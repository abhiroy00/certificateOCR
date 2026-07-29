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
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from . import db
from .config import SUPPORTED_EXT, Settings
from .csv_writer import ShardedCsvWriter, record_to_row
from .extractor import build_engine, validate

log = logging.getLogger("share_ocr")

# "Please try again in 656ms" / "in 1.5s", as OpenAI words it.
_RETRY_HINT = re.compile(r"try again in\s*([0-9.]+)\s*(ms|s)\b", re.I)
_RATE_LIMIT_MARKERS = ("rate limit", "429", "rate_limit_exceeded")


def is_rate_limit(exc: BaseException) -> bool:
    return any(k in str(exc).lower() for k in _RATE_LIMIT_MARKERS)


def retry_after_seconds(exc: BaseException) -> Optional[float]:
    """How long the API asked us to wait, from the headers or the message."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) or {}
    for key in ("retry-after-ms", "retry-after"):
        try:
            raw = headers.get(key)
        except Exception:                               # noqa: BLE001
            raw = None
        if raw:
            try:
                v = float(raw)
                return v / 1000.0 if key.endswith("-ms") else v
            except (TypeError, ValueError):
                pass
    m = _RETRY_HINT.search(str(exc))
    if m:
        v = float(m.group(1))
        return v / 1000.0 if m.group(2).lower() == "ms" else v
    return None


class RateGate:
    """Process-wide brake shared by every worker.

    A 429 from one worker used to tell the other seven nothing: they kept
    firing into an exhausted quota, each burned an attempt, and files hit
    max_attempts and died while the *account* was merely busy - 5 of 10 went
    to 'dead' within seconds. Now the first worker to hit the wall parks the
    whole pool until the window the API actually asked for has passed.
    """

    def __init__(self) -> None:
        self._until = 0.0
        self._lock = threading.Lock()

    def wait(self, stop: threading.Event) -> None:
        while not stop.is_set():
            with self._lock:
                remaining = self._until - time.time()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.25))

    def penalise(self, seconds: float) -> float:
        seconds = max(0.5, min(60.0, seconds))
        with self._lock:
            self._until = max(self._until, time.time() + seconds)
            return self._until - time.time()


# ------------------------------------------------------------------ scan --
def scan_paths(root: str, exts: Tuple[str, ...] = SUPPORTED_EXT) -> Iterator[Tuple[str, str, int]]:
    """Recursively yield (path, name, size). Uses os.scandir -> constant memory."""
    root_p = Path(root)
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
        self._rate = RateGate()
        # Rate-limit requeues per file. A 429 does not count as an attempt,
        # so this is the only thing stopping a file bouncing forever if the
        # quota never recovers.
        self._rl_hits: Dict[int, int] = defaultdict(int)
        self._rl_lock = threading.Lock()

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
        self._safe_flush()

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    # --------------------------------------------------------- workers --
    def _claim_size(self, q: db.Queue) -> int:
        """How many rows this worker should take in one claim.

        A flat claim_batch (200) meant the first worker to reach the queue
        took the ENTIRE selection and the other N-1 exited idle a second
        later. A 10-file run from the GUI - the common case, since a
        selection is usually smaller than claim_batch x workers - therefore
        ran single-file-at-a-time no matter how many workers were
        configured.

        So: claim in bulk while there is bulk to claim, and fall back to a
        fair share when the queue is nearly drained. The count is capped
        (see pending_count) so the 30-lakh path pays almost nothing for it
        and still gets the full batch.
        """
        cap = max(1, self.s.claim_batch * max(1, self.s.workers))
        try:
            pending = q.pending_count(self._scope_ids, cap=cap)
        except Exception:                               # noqa: BLE001
            return self.s.claim_batch
        if pending >= cap:
            return self.s.claim_batch
        fair = -(-pending // max(1, self.s.workers))    # ceil division
        return max(1, min(self.s.claim_batch, fair))

    @staticmethod
    def _release(q: db.Queue, file_id: int) -> None:
        """Hand a claimed-but-unprocessed file back to the queue."""
        try:
            q.conn.execute(
                "UPDATE files SET status='pending' WHERE id=? AND status='running'",
                (file_id,))
            q.conn.commit()
        except Exception:                               # noqa: BLE001
            pass

    def _worker(self, idx: int) -> None:
        q = db.Queue(self.s.db_path)   # thread-local connection
        idle_rounds = 0
        while not self._stop.is_set():
            while self._pause.is_set() and not self._stop.is_set():
                time.sleep(0.25)
            rows = q.claim(self._claim_size(q), worker=f"w{idx}",
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
                    self._release(q, r["id"])   # give the work back
                    continue
                try:
                    self._process_one(q, r["id"], r["path"], r["name"])
                except BaseException:           # noqa: BLE001
                    # _process_one handles its own failures. Anything that
                    # escapes here means this thread is dying mid-file, and
                    # the row must not be left 'running' - that state is
                    # invisible to the next Extract and (before the db fix)
                    # to Retry as well, so the file silently produced
                    # nothing. Observed for real on a 429 backoff.
                    self._release(q, r["id"])
                    raise
        q.close()

    MAX_RATE_LIMIT_REQUEUES = 20

    def _handle_rate_limit(self, q: db.Queue, file_id: int, name: str,
                           exc: BaseException) -> bool:
        """Park the pool and give the file back uncounted. True if handled.

        A 429 means the account is out of quota for this minute, not that
        the scan is bad, so it must not push the file towards 'dead'.
        """
        with self._rl_lock:
            self._rl_hits[file_id] += 1
            hits = self._rl_hits[file_id]
        if hits > self.MAX_RATE_LIMIT_REQUEUES:
            return False        # quota never recovered - fail it for real
        wait = retry_after_seconds(exc) or self.s.retry_base_delay
        actual = self._rate.penalise(wait + random.random())
        q.mark_rate_limited(file_id, f"{type(exc).__name__}: {exc}")
        self.on_log(f"Rate limited — pausing all workers {actual:.1f}s "
                   f"(retry {hits}/{self.MAX_RATE_LIMIT_REQUEUES}, {name})")
        return True

    def _process_one(self, q: db.Queue, file_id: int, path: str, name: str) -> None:
        t0 = time.time()
        self._rate.wait(self._stop)      # respect a penalty another worker took
        if self._stop.is_set():
            self._release(q, file_id)
            return
        try:
            engine = self._engine()
            records = engine.extract_file(path)
            for rec in records:
                rec["validation_flags"] = validate(
                    rec, flag_addons=self.s.flag_missing_addons)
            latency = int((time.time() - t0) * 1000)
            row_ids = q.mark_done(file_id, records, engine.name, self.s.model, latency)
        except Exception as e:                      # noqa: BLE001
            if is_rate_limit(e) and self._handle_rate_limit(q, file_id, name, e):
                return
            status = q.mark_failed(file_id, f"{type(e).__name__}: {e}",
                                   self.s.max_attempts)
            if status == db.DEAD:
                self.stats.bump(failed=1)
            self.on_log(f"ERROR {name}: {type(e).__name__}: {e}")
            # backoff on transient network failures
            msg = str(e).lower()
            if any(k in msg for k in ("timeout", "connection", "overloaded",
                                      "503", "502")):
                row = q.conn.execute(
                    "SELECT attempts FROM files WHERE id=?", (file_id,)).fetchone()
                attempts = row[0] if row else 1
                delay = self.s.retry_base_delay * (2 ** min(4, attempts)) \
                    + random.random()
                time.sleep(min(60.0, delay))
            return

        # The extraction has already succeeded and is durably stored in the
        # results table above. A CSV write failure here (classically: the
        # shard file is open in Excel, which locks it on Windows) must NOT
        # undo that or mark the file failed - it would put the file back in
        # the pending/failed pool, get re-claimed on the next Extract click,
        # and pay for the same OpenAI call again for data we already have.
        out_rows = [
            record_to_row(rec, row_id=rid, name=name, path=path,
                          engine=engine.name, model=self.s.model,
                          latency_ms=latency)
            for rid, rec in zip(row_ids, records)
        ]
        try:
            self.csv.write_many(out_rows)
            q.mark_exported(row_ids)
        except Exception as e:                      # noqa: BLE001
            self.on_log(f"WARN {name}: extracted OK but CSV write failed "
                       f"({type(e).__name__}: {e}) - close the CSV file if "
                       "it is open in Excel. It writes automatically once "
                       "the file is free again.")
        self.stats.bump(done=1, rows=len(out_rows))
        if self.on_row:
            for row in out_rows:
                self.on_row(row)

    # -------------------------------------------------------- reporter --
    def _reporter(self) -> None:
        while not self._stop.is_set() and self.running:
            self._safe_flush()
            if self.on_progress:
                self.on_progress(self.stats)
            time.sleep(1.0)
        self._safe_flush()
        if self.on_progress:
            self.on_progress(self.stats)

    def _safe_flush(self) -> None:
        try:
            self.csv.flush()
        except Exception as e:                      # noqa: BLE001
            self.on_log(f"WARN: CSV flush failed ({type(e).__name__}: {e}) - "
                       "close the CSV file if it is open in Excel.")

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
        deleted = self.q.delete_results(row_ids, requeue=requeue)
        self.csv.remove_rows(row_ids)
        return deleted

    def clear(self) -> None:
        self.q.reset_all()
        for p in self.s.csv_dir.glob("*.csv"):
            p.unlink(missing_ok=True)
        self.csv = ShardedCsvWriter(self.s.csv_dir, self.s.csv_shard_rows,
                                    self.s.csv_flush_rows)
        self.stats = Stats()
