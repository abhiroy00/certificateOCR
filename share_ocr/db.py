"""SQLite-backed work queue.

Why a queue instead of a python list?
30 lakh (3,000,000) images cannot live in RAM as a pandas DataFrame, and the
job will certainly be interrupted at some point. The queue gives us:

  * O(1) memory ingestion (files are streamed in, 5k at a time)
  * exactly-once processing with atomic claims
  * automatic resume after a crash / power cut
  * retry with attempt counters and a dead-letter status
  * live counters for the Tkinter progress bar
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, TypeVar

log = logging.getLogger("share_ocr")
_T = TypeVar("_T")

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
DEAD = "dead"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;
PRAGMA mmap_size=268435456;

CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY,
    path       TEXT    NOT NULL UNIQUE,
    name       TEXT    NOT NULL,
    size       INTEGER,
    status     TEXT    NOT NULL DEFAULT 'pending',
    attempts   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    batch      TEXT,
    claimed_at REAL,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status, id);
CREATE INDEX IF NOT EXISTS idx_files_batch  ON files(batch);

-- The file ids the CURRENT run is allowed to claim (the GUI's Extract only
-- processes what the operator selected). Kept in a table, not passed as
-- "id IN (?,?,...)": SQLite rejects a statement with more than ~32k bound
-- parameters, so selecting 80,000 files made Extract fail outright with
-- "too many SQL variables" - and even below that limit, rebuilding a
-- many-thousand-placeholder query on every claim() is needlessly slow.
CREATE TABLE IF NOT EXISTS run_scope (id INTEGER PRIMARY KEY);

CREATE TABLE IF NOT EXISTS results (
    row_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id    INTEGER NOT NULL,
    page_no    INTEGER NOT NULL DEFAULT 1,
    payload    TEXT    NOT NULL,
    flags      TEXT    NOT NULL DEFAULT '',
    engine     TEXT,
    model      TEXT,
    latency_ms INTEGER,
    created_at REAL,
    exported   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(file_id, page_no)
);
CREATE INDEX IF NOT EXISTS idx_results_export ON results(exported, row_id);
-- Lets find_duplicate_source() look a match up instead of scanning every
-- row's JSON payload - without this the duplicate check gets slower with
-- every certificate ever extracted into this queue.db, which on a bulk run
-- (dozens-hundreds of files, each triggering the check) held the database
-- busy long enough to cause "database is locked" under real-world
-- contention (antivirus / cloud-sync briefly touching the file, several
-- worker threads writing at once).
CREATE INDEX IF NOT EXISTS idx_results_cert_dup ON results(
    json_extract(payload, '$.certificate_no'),
    json_extract(payload, '$.company_name')
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Queue:
    """Thread-safe wrapper around the SQLite job queue."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._claim_lock = threading.Lock()
        with self._conn_new() as c:
            c.executescript(SCHEMA)
            mode = c.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "wal":
                # journal_mode=WAL silently falls back instead of raising
                # when the filesystem can't support it (a network drive, a
                # OneDrive-synced folder, some external USB drives) - when
                # that happens every writer takes an exclusive lock instead
                # of coexisting with readers, which is the single most
                # common real-world cause of "database is locked" on a
                # client machine. Logged here so it shows up in
                # share_ocr.log instead of only as a vague crash later.
                log.warning(
                    "queue.db could not enable WAL mode (using '%s' instead) "
                    "at %s - if this machine keeps hitting 'database is "
                    "locked', move the app's data folder (Settings > "
                    "SHARE_OCR_HOME, default ~/.share_ocr) off any "
                    "OneDrive/Dropbox-synced or network path.",
                    mode, self.db_path)

    # ------------------------------------------------------------------
    def _conn_new(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=60, isolation_level=None)
        c.execute("PRAGMA busy_timeout=60000")
        return c

    @staticmethod
    def _retry_locked(conn: sqlite3.Connection, fn: Callable[[], _T],
                      attempts: int = 6, base_delay: float = 0.5) -> _T:
        """Run a DB write (fn does its own BEGIN...COMMIT on conn), retrying
        a few times on 'database is locked' / 'database is busy'.
        busy_timeout already makes SQLite itself wait out most contention,
        but a lock that outlasts even that (antivirus or a sync client
        holding the file, several workers writing at once on a slower disk)
        used to surface immediately as a fatal error and abort the whole
        run. This gives it a few more seconds, total, to clear - actual bugs
        (a bad query, a schema error) are never 'locked'/'busy' and still
        raise straight away. A failed attempt may have left a transaction
        open (BEGIN succeeded, the statement or COMMIT inside it didn't), so
        each retry rolls back first - harmless if there was nothing to roll
        back."""
        delay = base_delay
        for attempt in range(attempts):
            try:
                return fn()
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "locked" not in msg and "busy" not in msg:
                    raise
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                if attempt == attempts - 1:
                    raise
                log.warning("database busy (attempt %d/%d), retrying in %.1fs: %s",
                           attempt + 1, attempts, delay, e)
                # jitter: with dozens of workers, identical delays make them
                # all wake up and collide again at the same instant
                time.sleep(delay * (0.5 + random.random()))
                delay *= 2
        raise AssertionError("unreachable")   # pragma: no cover

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._conn_new()
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # ---------------------------------------------------------- ingest --
    def add_files(self, paths: Iterable[Tuple[str, str, int]], batch: str,
                  chunk: int = 5000) -> int:
        """Insert (path, name, size) tuples. Duplicates are ignored.

        Streams in chunks so a 3M-file scan never blows up memory.
        """
        added = 0
        buf: List[Tuple] = []
        now = time.time()
        conn = self.conn
        for p, n, s in paths:
            buf.append((p, n, s, batch, now))
            if len(buf) >= chunk:
                added += self._flush_insert(conn, buf)
                buf.clear()
        if buf:
            added += self._flush_insert(conn, buf)
        return added

    @classmethod
    def _flush_insert(cls, conn: sqlite3.Connection, buf: Sequence[Tuple]) -> int:
        def _txn() -> int:
            cur = conn.cursor()
            cur.execute("BEGIN")
            cur.executemany(
                "INSERT OR IGNORE INTO files(path, name, size, batch, status, updated_at)"
                " VALUES(?,?,?,?,'pending',?)",
                buf,
            )
            n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            cur.execute("COMMIT")
            return n
        return cls._retry_locked(conn, _txn)

    # ----------------------------------------------------------- claim --
    def set_scope(self, ids: Optional[Sequence[int]]) -> None:
        """Stage the file ids this run may claim (see the run_scope table).
        None / empty clears it. Replaces whatever the previous run staged."""
        conn = self.conn
        ids = list(ids) if ids else []

        def _txn() -> None:
            cur = conn.cursor()
            cur.execute("BEGIN")
            cur.execute("DELETE FROM run_scope")
            for i in range(0, len(ids), 20_000):
                cur.executemany("INSERT OR IGNORE INTO run_scope(id) VALUES(?)",
                                ((x,) for x in ids[i:i + 20_000]))
            cur.execute("COMMIT")
        self._retry_locked(conn, _txn)

    def claim(self, limit: int, worker: str,
              scope_ids: Optional[Sequence[int]] = None,
              use_scope: bool = False) -> List[sqlite3.Row]:
        """Atomically move up to `limit` pending rows to running.

        `scope_ids`, when given, restricts the claim to those file ids only
        (the GUI's "Extract" uses this so it processes just what the
        operator selected, not every pending/failed file ever queued). The
        headless CLI never passes it, so the full resumable-queue behaviour
        for the 30-lakh job is unchanged.
        """
        if use_scope:
            pass                     # ids come from the run_scope table
        elif scope_ids is not None and not scope_ids:
            return []
        elif scope_ids is not None and len(scope_ids) > 900:
            # Too many for bound parameters - stage them and use the table.
            self.set_scope(scope_ids)
            use_scope = True
        with self._claim_lock:
            conn = self.conn
            conn.row_factory = sqlite3.Row

            def _txn() -> List[sqlite3.Row]:
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                if use_scope:
                    rows = cur.execute(
                        "SELECT f.id, f.path, f.name FROM files f"
                        " JOIN run_scope s ON s.id = f.id"
                        " WHERE f.status IN ('pending','failed')"
                        " ORDER BY f.id LIMIT ?", (limit,)).fetchall()
                elif scope_ids is not None:
                    qs = ",".join("?" * len(scope_ids))
                    rows = cur.execute(
                        f"SELECT id, path, name FROM files"
                        f" WHERE status IN ('pending','failed') AND id IN ({qs})"
                        f" ORDER BY id LIMIT ?",
                        [*scope_ids, limit],
                    ).fetchall()
                else:
                    rows = cur.execute(
                        "SELECT id, path, name FROM files WHERE status IN ('pending','failed')"
                        " ORDER BY id LIMIT ?",
                        (limit,),
                    ).fetchall()
                if rows:
                    ids = [r["id"] for r in rows]
                    qs = ",".join("?" * len(ids))
                    cur.execute(
                        f"UPDATE files SET status='running', claimed_at=?, updated_at=?"
                        f" WHERE id IN ({qs})",
                        [time.time(), time.time(), *ids],
                    )
                cur.execute("COMMIT")
                return rows
            return self._retry_locked(conn, _txn)

    def resolve_ids(self, paths: Iterable[str]) -> List[int]:
        """File ids for exact paths — used to scope a GUI run to only the
        files under the operator's current selection."""
        ids: List[int] = []
        conn = self.conn
        buf = list(paths)
        chunk = 500
        for i in range(0, len(buf), chunk):
            part = buf[i:i + chunk]
            qs = ",".join("?" * len(part))
            ids += [r[0] for r in conn.execute(
                f"SELECT id FROM files WHERE path IN ({qs})", part).fetchall()]
        return ids

    def counts_for(self, ids: Sequence[int]) -> Dict[str, int]:
        """Same shape as counts(), but scoped to a specific set of file ids."""
        out = {PENDING: 0, RUNNING: 0, DONE: 0, FAILED: 0, DEAD: 0}
        if not ids:
            out["total"] = 0
            out["rows"] = 0
            return out
        conn = self.conn
        chunk = 500
        buf = list(ids)
        for i in range(0, len(buf), chunk):
            part = buf[i:i + chunk]
            qs = ",".join("?" * len(part))
            for status, n in conn.execute(
                    f"SELECT status, COUNT(*) FROM files WHERE id IN ({qs})"
                    f" GROUP BY status", part).fetchall():
                out[status] = out.get(status, 0) + n
        out["total"] = sum(v for k, v in out.items() if k != "total")
        rows = 0
        for i in range(0, len(buf), chunk):
            part = buf[i:i + chunk]
            qs = ",".join("?" * len(part))
            rows += conn.execute(
                f"SELECT COUNT(*) FROM results WHERE file_id IN ({qs})",
                part).fetchone()[0]
        out["rows"] = rows
        return out

    def requeue_stale(self, older_than_s: float = 900) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE files SET status='pending' WHERE status='running' AND claimed_at < ?",
            (time.time() - older_than_s,),
        )
        return cur.rowcount

    # ---------------------------------------------------------- report --
    def mark_done(self, file_id: int, records: List[dict], engine: str,
                  model: str, latency_ms: int) -> List[int]:
        conn = self.conn

        def _txn() -> List[int]:
            cur = conn.cursor()
            cur.execute("BEGIN")
            row_ids: List[int] = []
            for i, rec in enumerate(records, start=1):
                cur.execute(
                    "INSERT OR REPLACE INTO results"
                    "(file_id, page_no, payload, flags, engine, model, latency_ms, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (file_id, rec.get("page_no", i), json.dumps(rec, default=str),
                     rec.get("validation_flags", ""), engine, model, latency_ms,
                     time.time()),
                )
                row_ids.append(cur.lastrowid)
            cur.execute(
                "UPDATE files SET status='done', error=NULL, updated_at=? WHERE id=?",
                (time.time(), file_id),
            )
            cur.execute("COMMIT")
            return row_ids
        return self._retry_locked(conn, _txn)

    def find_duplicate_source(self, certificate_no: str, company_name: str,
                              exclude_file_id: int) -> Optional[str]:
        """The name of ANOTHER already-extracted file whose certificate_no
        and company_name both match, if one exists.

        Used to catch the common operator mistake of a folder holding two
        copies of the same certificate under different names (e.g. a plain
        scan AND a "..._merged.pdf" of the same document) - both get
        extracted correctly and independently, so this doesn't stop that,
        it just flags the second one so it doesn't get mistaken for a real
        duplicate-processing bug. Matches on certificate_no + company_name
        together (not certificate_no alone) since certificate numbers are
        only unique within one issuing company. Searches the whole database,
        not just the current run, so a duplicate re-added in a later session
        is still caught."""
        certificate_no = (certificate_no or "").strip()
        company_name = (company_name or "").strip()
        if not certificate_no or not company_name:
            return None
        conn = self.conn

        def _query() -> Optional[str]:
            row = conn.execute(
                "SELECT f.name FROM results r JOIN files f ON f.id = r.file_id"
                " WHERE r.file_id != ?"
                "   AND json_extract(r.payload, '$.certificate_no') = ?"
                "   AND json_extract(r.payload, '$.company_name') = ?"
                " LIMIT 1",
                (exclude_file_id, certificate_no, company_name),
            ).fetchone()
            return row[0] if row else None
        return self._retry_locked(conn, _query)

    def mark_failed(self, file_id: int, error: str, max_attempts: int) -> str:
        conn = self.conn

        def _txn() -> str:
            cur = conn.cursor()
            cur.execute("BEGIN")
            cur.execute("UPDATE files SET attempts = attempts + 1 WHERE id=?", (file_id,))
            attempts = cur.execute(
                "SELECT attempts FROM files WHERE id=?", (file_id,)
            ).fetchone()[0]
            status = DEAD if attempts >= max_attempts else FAILED
            cur.execute(
                "UPDATE files SET status=?, error=?, updated_at=? WHERE id=?",
                (status, error[:1000], time.time(), file_id),
            )
            cur.execute("COMMIT")
            return status
        return self._retry_locked(conn, _txn)

    # ----------------------------------------------------------- stats --
    def counts(self) -> Dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM files GROUP BY status"
        ).fetchall()
        out = {PENDING: 0, RUNNING: 0, DONE: 0, FAILED: 0, DEAD: 0}
        for status, n in rows:
            out[status] = n
        out["total"] = sum(v for k, v in out.items() if k != "total")
        out["rows"] = self.conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
        return out

    def unexported(self, limit: int = 1000) -> List[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute(
            "SELECT r.row_id, r.file_id, r.page_no, r.payload, r.flags, r.engine,"
            "       r.model, r.latency_ms, r.created_at, f.path, f.name"
            "  FROM results r JOIN files f ON f.id = r.file_id"
            " WHERE r.exported = 0 ORDER BY r.row_id LIMIT ?",
            (limit,),
        ).fetchall()

    def mark_exported(self, row_ids: Sequence[int]) -> None:
        if not row_ids:
            return
        cur = self.conn.cursor()
        cur.execute("BEGIN")
        cur.executemany(
            "UPDATE results SET exported=1 WHERE row_id=?", [(i,) for i in row_ids]
        )
        cur.execute("COMMIT")

    def source_path(self, row_id: int) -> Optional[str]:
        """Absolute path of the scan a result row was extracted from.

        The table only carries the file NAME, so this is what lets the GUI
        reopen the original image/PDF for a row to eyeball the extraction
        against.
        """
        r = self.conn.execute(
            "SELECT f.path FROM results r JOIN files f ON f.id = r.file_id"
            " WHERE r.row_id = ?", (row_id,)).fetchone()
        return r[0] if r else None

    def result_signatures(self, row_ids: Sequence[int]) -> List[tuple]:
        """(source_file, certificate_no) for each row_id - how a row is
        identified in the exported CSV now that row_id itself is not a
        column (used by "Delete selected", see csv_writer.remove_rows)."""
        ids = [int(i) for i in row_ids]
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT r.payload, f.name FROM results r"
            f" JOIN files f ON f.id = r.file_id"
            f" WHERE r.row_id IN ({placeholders})", ids).fetchall()
        out = []
        for payload, name in rows:
            try:
                cert = json.loads(payload).get("certificate_no") or ""
            except Exception:                              # noqa: BLE001
                cert = ""
            out.append((name, cert))
        return out

    def recent_rows(self, limit: int = 200) -> List[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute(
            "SELECT r.row_id, r.payload, r.flags, f.name, f.path"
            "  FROM results r JOIN files f ON f.id = r.file_id"
            " ORDER BY r.row_id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def failures(self, limit: int = 500) -> List[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute(
            "SELECT id, name, path, attempts, error, status, updated_at FROM files"
            " WHERE status IN ('failed','dead') ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def failed_or_dead_ids(self) -> List[int]:
        return [r[0] for r in self.conn.execute(
            "SELECT id FROM files WHERE status IN ('failed','dead')").fetchall()]

    def retry_failed(self) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE files SET status='pending', attempts=0, error=NULL"
            " WHERE status IN ('failed','dead')"
        )
        return cur.rowcount

    def delete_results(self, row_ids: Sequence[int], requeue: bool = True) -> int:
        """Remove specific extracted rows (the GUI's "Delete selected").

        The source file is put back to 'pending' by default so a later
        Extract pass can redo it instead of leaving a 'done' file with no
        result row, which would otherwise be stuck forever (re-ingesting
        the same path is a no-op because of the UNIQUE(path) constraint).
        """
        row_ids = list(row_ids)
        if not row_ids:
            return 0
        conn = self.conn
        cur = conn.cursor()
        qs = ",".join("?" * len(row_ids))
        cur.execute("BEGIN")
        file_ids = [r[0] for r in cur.execute(
            f"SELECT DISTINCT file_id FROM results WHERE row_id IN ({qs})",
            row_ids).fetchall()]
        cur.execute(f"DELETE FROM results WHERE row_id IN ({qs})", row_ids)
        deleted = cur.rowcount
        if requeue and file_ids:
            fqs = ",".join("?" * len(file_ids))
            cur.execute(
                f"UPDATE files SET status='pending', attempts=0, error=NULL,"
                f" updated_at=? WHERE id IN ({fqs})",
                [time.time(), *file_ids],
            )
        cur.execute("COMMIT")
        return deleted

    def reset_all(self) -> None:
        cur = self.conn.cursor()
        cur.execute("BEGIN")
        cur.execute("DELETE FROM results")
        cur.execute("DELETE FROM files")
        cur.execute("COMMIT")
        cur.execute("VACUUM")

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, value)
        )

    def get_meta(self, key: str) -> Optional[str]:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None
