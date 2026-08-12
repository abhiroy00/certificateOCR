"""Streaming, shardedary CSV writer.

Never holds more than `flush_rows` records in memory, so it can write
30 lakh rows without touching pandas. Rows are appended to part files
(part-00001.csv, part-00002.csv ...) so no single CSV becomes unusable,
and a `_needs_review.csv` sidecar collects every flagged row.
"""
from __future__ import annotations

import csv
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .config import (CERT_NO_HEADER, CSV_COLUMNS, CSV_SPEC,
                     SOURCE_FILE_HEADER, review_verdict)

# ------------------------------------------------------ Source File links --
# The "Source File" column is written as an Excel HYPERLINK() formula rather
# than plain text, so clicking the file name in the exported CSV opens the
# actual scan (Excel evaluates a cell as a formula whenever its content,
# after CSV parsing, starts with "="). This is a second, independent
# escaping layer from CSV's own quoting: csv.writer already doubles quote
# characters and wraps the field when IT serializes this string, so the only
# thing done by hand here is doubling a literal quote so it survives as part
# of the formula's own string literal. Windows forbids '"' in file/folder
# names outright, so in practice this never triggers on the paths this app
# actually writes - it exists for robustness on other platforms.
_HYPERLINK_RE = re.compile(
    r'^=HYPERLINK\(\s*"((?:[^"]|"")*)"\s*,\s*"((?:[^"]|"")*)"\s*\)$',
    re.IGNORECASE | re.DOTALL)


def hyperlink_cell(path: Optional[str], display: str) -> str:
    """A Source File cell that opens `path` when clicked in Excel, showing
    `display` as the visible text. Falls back to plain `display` text when
    there is no path to link to (older shards never recorded one)."""
    if not path:
        return display
    esc = lambda s: (s or "").replace('"', '""')           # noqa: E731
    return '=HYPERLINK("%s","%s")' % (esc(path), esc(display))


def display_name(value: str) -> str:
    """The plain file name behind a Source File cell, whether it holds a
    HYPERLINK() formula (current shards) or plain text (shards written
    before this feature, or a value that isn't a recognised formula at
    all). Used anywhere a row needs to be matched by file name rather than
    by its raw cell text - see remove_rows()/_rewrite_without()."""
    if not value:
        return value
    m = _HYPERLINK_RE.match(value.strip())
    if not m:
        return value
    return m.group(2).replace('""', '"')


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
            # rows are keyed by CSV header; "Review" is "Yes" exactly when the
            # The needs-review sidecar collects every row that carries ANY
            # validation flag (soft advisories included), even though the
            # Review column only says "Yes" for the hard ones.
            if row.get("Validation Flags"):
                self._review_buf.append(row)
            if len(self._buf) >= self.flush_rows:
                self._flush_locked()

    def write_many(self, rows: List[Dict]) -> None:
        for r in rows:
            self.write(r)

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def pending(self) -> int:
        """Rows accepted by write() but not yet on disk - non-zero here
        means the last flush attempt failed (e.g. the CSV shard was open
        in Excel) and the rows are only in memory."""
        with self._lock:
            return len(self._buf)

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
        if not new:
            ShardedCsvWriter._repair_header_if_stale(path)
        with path.open("a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            if new:
                w.writeheader()
            for r in rows:
                w.writerow(r)
            f.flush()

    @staticmethod
    def _migrate_row(old: Dict) -> Dict:
        """Map one row read from an older-format shard onto the current
        CSV_SPEC. `old` may be keyed by the previous internal snake_case
        names (e.g. company_name) OR by an earlier set of pretty headers
        (e.g. Script Name) - we pull from whichever is present, so this is
        both a snake->pretty migration and an idempotent no-op once a file
        is already current. New columns (Review, Present Transfer Date) that
        old data never had are derived or left blank."""
        def pick(*keys):
            for k in keys:
                v = old.get(k)
                if v not in (None, ""):
                    return v
            return ""
        row = {}
        for header, src in CSV_SPEC:
            if src == "@review":
                row[header] = review_verdict(
                    pick("validation_flags", "Validation Flags"))
            elif src == "@source_file":
                row[header] = pick("source_file", header)
            elif src == "@extracted_at":
                row[header] = pick("extracted_at", header)
            else:
                row[header] = pick(src, header)   # internal snake OR pretty
        return row

    @staticmethod
    def _repair_header_if_stale(path: Path) -> None:
        """A shard written under an older CSV layout still has the old
        header - columns may since have been added, dropped, or renamed.
        Re-read every row and remap it onto the current CSV_SPEC so the
        file stays a clean, aligned CSV with the current headers."""
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            header_line = f.readline()
        current = next(csv.reader([header_line]), [])
        if not current or current == CSV_COLUMNS:
            return
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore",
                               restval="")
            w.writeheader()
            for r in rows:
                w.writerow(ShardedCsvWriter._migrate_row(r))

    def close(self) -> None:
        self.flush()

    # ------------------------------------------------------------------
    def remove_rows(self, signatures) -> int:
        """Physically drop rows matching these (source_file, certificate_no)
        signatures from every shard and the needs-review sidecar.

        This is the GUI's interactive "Delete selected" action. row_id is no
        longer exported (the client deliverable doesn't carry it), so rows
        are matched on Source File + CertificateNo instead - unique per
        certificate in practice. It rewrites whichever shard actually
        contains a match (shards are capped at `shard_rows`, 200k by default,
        specifically so a rewrite like this stays cheap).
        """
        sigset = {(str(sf), str(cn)) for sf, cn in signatures}
        if not sigset:
            return 0
        with self._lock:
            self._flush_locked()
            removed = 0
            for path in [*sorted(self.out_dir.glob(f"{self.prefix}-part-*.csv")),
                         self._review_path()]:
                removed += self._rewrite_without(path, sigset)
            self._rows_in_shard = self._count_rows(self._shard_path())
            return removed

    @staticmethod
    def _rewrite_without(path: Path, sigset: set) -> int:
        if not path.exists():
            return 0
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = [ShardedCsvWriter._migrate_row(r) for r in csv.DictReader(f)]

        def sig(r):
            return (display_name(str(r.get(SOURCE_FILE_HEADER, ""))),
                    str(r.get(CERT_NO_HEADER, "")))

        kept = [r for r in rows if sig(r) not in sigset]
        removed = len(rows) - len(kept)
        if removed == 0:
            return 0
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for r in kept:
                w.writerow(r)
        return removed

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


def record_to_row(rec: Dict, *, name: str, source_path: Optional[str] = None) -> Dict:
    """Flatten an extraction record into the deliverable CSV row, keyed by
    the pretty headers in CSV_SPEC. `name` is the scanned file's name;
    `source_path` (when known) is its absolute path on disk, which turns
    the Source File cell into a clickable Excel link that opens that exact
    scan - see hyperlink_cell()."""
    flags = rec.get("validation_flags") or ""
    extracted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row: Dict = {}
    for header, src in CSV_SPEC:
        if src == "@review":
            row[header] = review_verdict(flags)
        elif src == "@source_file":
            row[header] = hyperlink_cell(source_path, name)
        elif src == "@extracted_at":
            row[header] = extracted_at
        else:
            v = rec.get(src)
            row[header] = "" if v is None else v
    return row
