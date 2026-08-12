"""The scalable worker pipeline.

Flow
----
  scan(folder)  -> streams paths into the SQLite queue (no RAM blow-up)
  Pipeline.run  -> N worker threads claim batches, call the engine,
                   write results to SQLite AND stream rows into CSV shards

Designed for 30 lakh (3,000,000) files:
  * ingestion is O(1) memory, ~50k files/sec with os.scandir
  * work is claimed in batches so SQLite locking is never the bottleneck
  * every result is durable immediately -> kill the app anytime and resume
  * exponential backoff + rate-limit awareness for API errors
  * live throughput / ETA counters for the GUI
"""
from __future__ import annotations

import logging
import os
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from . import db
from .config import SUPPORTED_EXT, Settings
from .csv_writer import ShardedCsvWriter, record_to_row
from .extractor import build_engine, validate

log = logging.getLogger("share_ocr")


# ------------------------------------------------------------------ scan --
def scan_paths(root: str, exts: Tuple[str, ...] = SUPPORTED_EXT) -> Iterator[Tuple[str, str, int]]:
    """Recursively yield (path, name, size). Uses os.scandir -> constant memory.

    The root is resolved to an absolute, canonical path first. The queue's
    uniqueness check is a literal string match on path, so the same
    physical file reached via a relative root in one session and an
    absolute root in another (e.g. a script run from the project folder
    vs. the GUI's folder-picker, which always returns an absolute path)
    would otherwise be queued - and billed for - twice.
    """
    root_p = Path(root).resolve()
    if root_p.is_file():
        if root_p.suffix.lower() in exts:
            yield str(root_p), root_p.name, root_p.stat().st_size
        return

    stack = [str(root_p)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            if os.path.splitext(entry.name)[1].lower() in exts:
                                yield entry.path, entry.name, entry.stat().st_size
                    except OSError:
                        continue
        except OSError:
            continue


# ----------------------------------------------------------------- stats --
@dataclass
class Stats:
    total: int = 0
    done: int = 0
    failed: int = 0
    rows: int = 0
    started_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def bump(self, *, done: int = 0, failed: int = 0, rows: int = 0) -> None:
        with self._lock:
            self.done += done
            self.failed += failed
            self.rows += rows

    @property
    def elapsed(self) -> float:
        return max(1e-6, time.time() - self.started_at)

    @property
    def rate(self) -> float:
        """files per second"""
        return (self.done + self.failed) / self.elapsed

    @property
    def eta_seconds(self) -> Optional[float]:
        remaining = self.total - self.done - self.failed
        if remaining <= 0 or self.rate <= 0:
            return 0.0
        return remaining / self.rate


# -------------------------------------------------------------- pipeline --
class Pipeline:
    def __init__(self, settings: Settings,
                 on_row: Optional[Callable[[Dict], None]] = None,
                 on_progress: Optional[Callable[[Stats], None]] = None,
                 on_log: Optional[Callable[[str], None]] = None):
        self.s = settings
        self.s.ensure_dirs()
        self.q = db.Queue(self.s.db_path)
        self.csv = ShardedCsvWriter(self.s.csv_dir, self.s.csv_shard_rows,
                                    self.s.csv_flush_rows)
        self.stats = Stats()
        self.on_row = on_row
        self.on_progress = on_progress
        self.on_log = on_log or (lambda m: log.info(m))
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._threads: List[threading.Thread] = []
        self._engines: Dict[int, object] = {}
        self._engine_lock = threading.Lock()
        self._scope_ids: Optional[List[int]] = None

    # ---------------------------------------------------------- ingest --
    def ingest(self, roots: List[str], batch: Optional[str] = None) -> int:
        batch = batch or time.strftime("%Y%m%d-%H%M%S")
        added = 0
        for root in roots:
            added += self.q.add_files(scan_paths(root), batch)
            self.on_log(f"Indexed {root} -> {added:,} new file(s) queued")
        c = self.q.counts()
        self.stats.total = c["total"]
        return added

    def scope_ids_for(self, roots: List[str]) -> List[int]:
        """File ids under these roots/paths, whether just-ingested or left
        over pending from an earlier run of this same selection. Used to
        scope a GUI Extract to what the operator actually picked, instead
        of the entire historical backlog other selections may have left
        pending."""
        paths = [p for root in roots for p, _, _ in scan_paths(root)]
        return self.q.resolve_ids(paths)

    # ----------------------------------------------------------- engine --
    def _engine(self):
        tid = threading.get_ident()
        eng = self._engines.get(tid)
        if eng is None:
            with self._engine_lock:
                eng = build_engine(self.s)
                self._engines[tid] = eng
        return eng

    # ------------------------------------------------------------- run --
    def start(self, scope_ids: Optional[List[int]] = None) -> None:
        """Start the worker pool.

        `scope_ids`, when given, restricts processing to those file ids
        only (see scope_ids_for) — this is how the GUI's Extract avoids
        silently reprocessing unrelated backlog left pending by an earlier,
        different selection. Leave it None (the CLI's default) to drain
        the whole resumable queue, which is what the 30-lakh multi-machine
        job relies on.
        """
        self._stop.clear()
        self._scope_ids = list(scope_ids) if scope_ids is not None else None
        self.q.requeue_stale()
        c = self.q.counts_for(self._scope_ids) if self._scope_ids is not None else self.q.counts()
        self.stats = Stats(total=c["total"], done=c[db.DONE], failed=c[db.DEAD])
        self._threads = [
            threading.Thread(target=self._worker, args=(i,), daemon=True,
                             name=f"ocr-{i}")
            for i in range(self.s.workers)
        ]
        for t in self._threads:
            t.start()
        threading.Thread(target=self._reporter, daemon=True, name="reporter").start()

    def stop(self) -> None:
        self._stop.set()

    def pause(self, value: bool = True) -> None:
        self._pause.set() if value else self._pause.clear()

    def join(self) -> None:
        for t in self._threads:
            t.join()
        self._drain_flush()

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    # --------------------------------------------------------- workers --
    def _worker(self, idx: int) -> None:
        q = db.Queue(self.s.db_path)   # thread-local connection
        idle_rounds = 0
        while not self._stop.is_set():
            while self._pause.is_set() and not self._stop.is_set():
                time.sleep(0.25)
            rows = q.claim(self.s.claim_batch, worker=f"w{idx}",
                          scope_ids=self._scope_ids)
            if not rows:
                # DUPLICATE-ROW BUG (fixed): the old code probed for work with
                # `q.claim(1)` and threw the result away. That row was already
                # flipped to 'running' with nobody processing it, so
                # requeue_stale() later handed the same file to another worker
                # and it was extracted TWICE - two identical rows in the table
                # and in the CSV. Never claim what you are not going to run.
                idle_rounds += 1
                if idle_rounds >= 2:
                    break
                time.sleep(0.5)
                continue
            idle_rounds = 0
            for r in rows:
                if self._stop.is_set():
                    # give unfinished work back to the queue
                    q.conn.execute(
                        "UPDATE files SET status='pending' WHERE id=? AND status='running'",
                        (r["id"],))
                    q.conn.commit()
                    continue
                self._process_one(q, r["id"], r["path"], r["name"])
        q.close()

    def _process_one(self, q: db.Queue, file_id: int, path: str, name: str) -> None:
        t0 = time.time()
        try:
            engine = self._engine()
            records = engine.extract_file(path)
            for rec in records:
                rec["validation_flags"] = validate(
                    rec, flag_addons=self.s.flag_missing_addons)
            latency = int((time.time() - t0) * 1000)
            row_ids = q.mark_done(file_id, records, engine.name, self.s.model, latency)
        except Exception as e:                      # noqa: BLE001
            status = q.mark_failed(file_id, f"{type(e).__name__}: {e}",
                                   self.s.max_attempts)
            if status == db.DEAD:
                self.stats.bump(failed=1)
            self.on_log(f"ERROR {name}: {type(e).__name__}: {e}")
            # backoff on rate limits / transient network failures
            msg = str(e).lower()
            if any(k in msg for k in ("rate limit", "429", "timeout", "connection",
                                      "overloaded", "503", "502")):
                delay = self.s.retry_base_delay * (2 ** min(4, q.conn.execute(
                    "SELECT attempts FROM files WHERE id=?", (file_id,)
                ).fetchone()[0])) + random.random()
                time.sleep(min(60.0, delay))
            return

        # The extraction has already succeeded and is durably stored in the
        # results table above. A CSV write failure here (classically: the
        # shard file is open in Excel, which locks it on Windows) must NOT
        # undo that or mark the file failed - it would put the file back in
        # the pending/failed pool, get re-claimed on the next Extract click,
        # and pay for the same OpenAI call again for data we already have.
        csv_rows = [record_to_row(rec, name=name) for rec in records]
        try:
            self.csv.write_many(csv_rows)
            q.mark_exported(row_ids)
        except Exception as e:                      # noqa: BLE001
            self.on_log(f"WARN {name}: extracted OK but CSV write failed "
                       f"({type(e).__name__}: {e}) - close the CSV file if "
                       "it is open in Excel. It writes automatically once "
                       "the file is free again.")
        self.stats.bump(done=1, rows=len(csv_rows))
        if self.on_row:
            # The GUI table + delete path work in internal (snake_case) keys,
            # not the pretty CSV headers - feed them the raw record plus the
            # bits the table needs (source_file, row_id).
            for rid, rec in zip(row_ids, records):
                self.on_row({**rec, "source_file": name, "row_id": rid})

    # -------------------------------------------------------- reporter --
    def _reporter(self) -> None:
        while not self._stop.is_set() and self.running:
            self._safe_flush()
            if self.on_progress:
                self.on_progress(self.stats)
            time.sleep(1.0)
        self._drain_flush()
        if self.on_progress:
            self.on_progress(self.stats)

    def _safe_flush(self) -> None:
        try:
            self.csv.flush()
        except Exception as e:                      # noqa: BLE001
            self.on_log(f"WARN: CSV flush failed ({type(e).__name__}: {e}) - "
                       "close the CSV file if it is open in Excel.")

    def _drain_flush(self, attempts: int = 15, delay: float = 2.0) -> None:
        """Keep retrying after work stops, not just once.

        A single failed flush (e.g. the CSV shard is open in Excel at the
        exact moment extraction finishes) used to strand rows in memory
        until the app was closed and reopened - nothing kept retrying once
        the worker threads and the 1s reporter loop had both exited. This
        gives a locked file up to `attempts * delay` seconds to free up
        (closing Excel, say) before giving up and leaving the WARN log as
        the only trace.
        """
        for _ in range(attempts):
            self._safe_flush()
            if self.csv.pending() == 0:
                return
            time.sleep(delay)

    # ----------------------------------------------------------- misc --
    def counts(self) -> Dict[str, int]:
        return self.q.counts()

    def export_single_csv(self, dest: str) -> str:
        return str(self.csv.merge_into(Path(dest)))

    def delete_rows(self, row_ids: List[int], requeue: bool = True) -> int:
        """Remove specific extracted rows: DB, CSV shards, and (by default)
        put the source file back to pending so Extract can redo it."""
        if not row_ids:
            return 0
        # row_id is not exported any more, so look up how each row is
        # identified in the CSV (Source File + CertificateNo) BEFORE deleting
        # it from the DB, then use that to drop it from the shards.
        signatures = self.q.result_signatures(row_ids)
        deleted = self.q.delete_results(row_ids, requeue=requeue)
        self.csv.remove_rows(signatures)
        return deleted

    def clear(self) -> None:
        self.q.reset_all()
        for p in self.s.csv_dir.glob("*.csv"):
            p.unlink(missing_ok=True)
        self.csv = ShardedCsvWriter(self.s.csv_dir, self.s.csv_shard_rows,
                                    self.s.csv_flush_rows)
        self.stats = Stats()
