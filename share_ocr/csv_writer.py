"""Streaming, shardedary CSV writer.

Never holds more than `flush_rows` records in memory, so it can write
30 lakh rows without touching pandas. Rows are appended to part files
(part-00001.csv, part-00002.csv ...) so no single CSV becomes unusable,
and a `_needs_review.csv` sidecar collects every flagged row.
"""
from __future__ import annotations

import csv
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .config import CSV_COLUMNS


class ShardedCsvWriter:
    def __init__(self, out_dir: Path, shard_rows: int = 200_000,
                 flush_rows: int = 500, prefix: str = "certificates"):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.shard_rows = shard_rows
        self.flush_rows = flush_rows
        self.prefix = prefix
        self._lock = threading.Lock()
        self._buf: List[Dict] = []
        self._review_buf: List[Dict] = []
        self._shard_index = self._detect_shard_index()
        self._rows_in_shard = self._count_rows(self._shard_path())
        self.total_written = 0

    # ------------------------------------------------------------------
    def _detect_shard_index(self) -> int:
        existing = sorted(self.out_dir.glob(f"{self.prefix}-part-*.csv"))
        return int(existing[-1].stem.split("-")[-1]) if existing else 1

    def _shard_path(self) -> Path:
        return self.out_dir / f"{self.prefix}-part-{self._shard_index:05d}.csv"

    def _review_path(self) -> Path:
        return self.out_dir / f"{self.prefix}-needs-review.csv"

    @staticmethod
    def _count_rows(path: Path) -> int:
        if not path.exists():
            return 0
        with path.open("r", encoding="utf-8", newline="") as f:
            return max(0, sum(1 for _ in f) - 1)

    # ------------------------------------------------------------------
    def write(self, row: Dict) -> None:
        with self._lock:
            self._buf.append(row)
            if row.get("validation_flags"):
                self._review_buf.append(row)
            if len(self._buf) >= self.flush_rows:
                self._flush_locked()

    def write_many(self, rows: List[Dict]) -> None:
        for r in rows:
            self.write(r)

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._buf:
            self._append(self._shard_path(), self._buf)
            self._rows_in_shard += len(self._buf)
            self.total_written += len(self._buf)
            self._buf.clear()
            if self._rows_in_shard >= self.shard_rows:
                self._shard_index += 1
                self._rows_in_shard = 0
        if self._review_buf:
            self._append(self._review_path(), self._review_buf)
            self._review_buf.clear()

    @staticmethod
    def _append(path: Path, rows: List[Dict]) -> None:
        new = not path.exists()
        with path.open("a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            if new:
                w.writeheader()
            for r in rows:
                w.writerow(r)
            f.flush()

    def close(self) -> None:
        self.flush()

    # ------------------------------------------------------------------
    def merge_into(self, dest: Path) -> Path:
        """Concatenate all shards into one CSV (only sane below ~1M rows,
        but handy for a single-batch export from the GUI)."""
        self.flush()
        dest = Path(dest)
        parts = sorted(self.out_dir.glob(f"{self.prefix}-part-*.csv"))
        with dest.open("w", encoding="utf-8-sig", newline="") as out:
            wrote_header = False
            for p in parts:
                with p.open("r", encoding="utf-8-sig", newline="") as f:
                    header = f.readline()
                    if not wrote_header:
                        out.write(header)
                        wrote_header = True
                    for line in f:
                        out.write(line)
        return dest


def record_to_row(rec: Dict, *, row_id: int, name: str, path: str,
                  engine: str, model: str, latency_ms: int) -> Dict:
    """Flatten an extraction record into the fixed CSV column order."""
    row = {c: "" for c in CSV_COLUMNS}
    row.update({
        "row_id": row_id,
        "source_file": name,
        "source_path": path,
        "page_no": rec.get("page_no", 1),
        "engine": engine,
        "model": model,
        "latency_ms": latency_ms,
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    for k, v in rec.items():
        if k in row and v is not None:
            row[k] = v
    return row
