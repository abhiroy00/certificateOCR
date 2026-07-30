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
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

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

    # ------------------------------------------------------------------
    def _conn_new(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=60, isolation_level=None)
        c.execute("PRAGMA busy_timeout=60000")
        return c

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

    @staticmethod
    def _flush_insert(conn: sqlite3.Connection, buf: Sequence[Tuple]) -> int:
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

    # ----------------------------------------------------------- claim --
    def claim(self, limit: int, worker: str,
              scope_ids: Optional[Sequence[int]] = None) -> List[sqlite3.Row]:
        """Atomically move up to `limit` pending rows to running.

        `scope_ids`, when given, restricts the claim to those file ids only
        (the GUI's "Extract" uses this so it processes just what the
        operator selected, not every pending/failed file ever queued). The
        headless CLI never passes it, so the full resumable-queue behaviour
        for the 30-lakh job is unchanged.
        """
        if scope_ids is not None and not scope_ids:
            return []
        with self._claim_lock:
            conn = self.conn
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            if scope_ids is not None:
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

    def mark_failed(self, file_id: int, error: str, max_attempts: int) -> str:
        conn = self.conn
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
            "SELECT id, name, path, attempts, error FROM files"
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
