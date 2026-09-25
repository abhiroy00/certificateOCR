"""Streaming, shardedary CSV writer.

Never holds more than `flush_rows` records in memory, so it can write
30 lakh rows without touching pandas. Rows are appended to part files
(part-00001.csv, part-00002.csv ...) so no single CSV becomes unusable.

There used to be a second `certificates-needs-review.csv` sidecar collecting
every flagged row. It is gone: the Review column already says "Yes"/"No" on
every row of the main CSV, so filtering on that column in Excel does the same
job without a second file that can drift out of sync with the first. Files
that failed extraction entirely now show up here too (see failed_file_row) -
one file, one CSV, everything in it.
"""
from __future__ import annotations

import csv
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .config import (CERT_NO_HEADER, CSV_COLUMNS, CSV_HEADER_ALIASES,
                     CSV_SPEC, EXTRACTED_AT_HEADER, FLAGS_HEADER,
                     HEADER_TO_KEY, REVIEW_HEADER, SOURCE_FILE_HEADER,
                     review_verdict)

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


def parse_hyperlink_cell(value: str):
    """(path, display) out of a Source File cell. `path` is None when the
    cell is plain text - shards written before this feature, or a value
    that isn't a recognised HYPERLINK() formula at all - in which case
    `display` is just the value itself."""
    if not value:
        return None, value
    m = _HYPERLINK_RE.match(value.strip())
    if not m:
        return None, value
    return m.group(1).replace('""', '"'), m.group(2).replace('""', '"')


def display_name(value: str) -> str:
    """The plain file name behind a Source File cell, whether it holds a
    HYPERLINK() formula (current shards) or plain text (shards written
    before this feature, or a value that isn't a recognised formula at
    all). Used anywhere a row needs to be matched by file name rather than
    by its raw cell text - see remove_rows()/_rewrite_without()."""
    return parse_hyperlink_cell(value)[1]


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
        self._shard_index = self._detect_shard_index()
        self._rows_in_shard = self._count_rows(self._shard_path())
        self.total_written = 0

    # ------------------------------------------------------------------
    def _detect_shard_index(self) -> int:
        existing = sorted(self.out_dir.glob(f"{self.prefix}-part-*.csv"))
        return int(existing[-1].stem.split("-")[-1]) if existing else 1

    def _shard_path(self) -> Path:
        return self.out_dir / f"{self.prefix}-part-{self._shard_index:05d}.csv"

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
        CSV_SPEC. `old` may be keyed by internal snake_case names
        (company_name), the current pretty headers, or a PREVIOUS set of
        pretty headers from before a column was renamed/reordered (e.g.
        "Source File" now "File name"). Every key is first resolved to its
        internal meaning, so this is a rename/reorder/snake->pretty migration
        and an idempotent no-op once a file is already current. New columns
        (Review, Present Transfer Date) old data never had are derived or
        left blank."""
        by_key: Dict[str, str] = {}
        for k, v in old.items():
            internal = HEADER_TO_KEY.get(k) or CSV_HEADER_ALIASES.get(k) or k
            if v not in (None, "") and not by_key.get(internal):
                by_key[internal] = v
        row = {}
        for header, src in CSV_SPEC:
            if src == "@review":
                row[header] = review_verdict(by_key.get("validation_flags", ""))
            else:
                row[header] = by_key.get(HEADER_TO_KEY[header], "")
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
        signatures from every shard.

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
            for path in sorted(self.out_dir.glob(f"{self.prefix}-part-*.csv")):
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

    # ------------------------------------------------------------------
    def merge_into_excel(self, dest: Path) -> Path:
        """Merge every shard into one real .xlsx workbook, with Source File
        as a genuine Excel hyperlink - blue and underlined - instead of a
        =HYPERLINK() formula.

        Why this exists: plain CSV cannot carry cell styling at all. A
        =HYPERLINK() formula in a .csv still opens the scan when clicked -
        Excel evaluates it as a formula the moment the cell's content
        starts with "=" - but Excel does not auto-apply the blue/underlined
        "Hyperlink" look to a formula result, only to a real hyperlink
        object (or a URL typed directly into a cell). There is no way
        around that while the file stays a .csv.

        The sharded .csv files remain the primary, scalable output - this
        is a one-shot bulk write, only sane up to roughly the same ~1M-row
        ceiling as merge_into(), for when a client wants one polished file
        to open and click through rather than raw shards.
        """
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font
        except ImportError as e:
            raise RuntimeError(
                "Excel export needs the openpyxl package. Run:\n"
                "    pip install openpyxl") from e

        self.flush()
        dest = Path(dest)
        wb = Workbook()
        ws = wb.active
        ws.title = "certificates"
        ws.append(CSV_COLUMNS)
        for cell in ws[1]:
            cell.font = Font(bold=True)

        link_col = CSV_COLUMNS.index(SOURCE_FILE_HEADER) + 1  # openpyxl is 1-based
        for p in sorted(self.out_dir.glob(f"{self.prefix}-part-*.csv")):
            with p.open("r", encoding="utf-8-sig", newline="") as f:
                for old in csv.DictReader(f):
                    row = self._migrate_row(old)
                    ws.append([row.get(h, "") for h in CSV_COLUMNS])
                    cell = ws.cell(row=ws.max_row, column=link_col)
                    link_path, display = parse_hyperlink_cell(cell.value)
                    cell.value = display
                    if link_path:
                        cell.hyperlink = link_path
                        cell.style = "Hyperlink"   # the real blue+underline look

        wb.save(dest)
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


def failed_file_row(name: str, source_path: Optional[str], error: str) -> Dict:
    """A CSV row for a file that never produced an extraction at all (queue
    status 'dead' - every retry, across every configured API key, was
    exhausted - see pipeline._process_one). Every data column is left blank
    on purpose: there is nothing to show for a file that was never actually
    read, only that it needs a human look. Source File still links to the
    scan so that look is one click away.

    This is what replaced the separate needs-review sidecar file: flagged
    rows (successful-but-uncertain, and now fully-failed ones too) all live
    in the one CSV, distinguished by the Review column."""
    row = {header: "" for header in CSV_COLUMNS}
    row[SOURCE_FILE_HEADER] = hyperlink_cell(source_path, name)
    row[REVIEW_HEADER] = "Yes"
    row[FLAGS_HEADER] = "Extraction failed: " + " ".join(str(error).split())
    row[EXTRACTED_AT_HEADER] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return row


# ------------------------------------------------- failed-files report ------
# A separate, always-current list of every file that could not be extracted,
# written next to the result shards so it is right there when the operator
# clicks "Output folder". The main CSV is untouched - a file that finally gave
# up still gets its blank Review=Yes placeholder row there (failed_file_row);
# this is the same information in one place, with the reason, attempt count
# and time, ready to hand to whoever re-scans or re-runs those files.
FAILED_REPORT_NAME = "certificates-failed.csv"
FAILED_REPORT_COLUMNS = ["File name", "Status", "Reason", "Attempts",
                         "Failed at", "File path"]


def write_failed_report(dest: Path, failures, max_attempts: int) -> int:
    """(Re)write the failed-files report from the queue's failed/dead rows
    and return how many files it lists. Written to a temp file and swapped
    in, so a reader never sees a half-written report. With nothing failed
    there is nothing to report - an old report is removed instead of being
    left behind listing files that have since succeeded.

    Raises PermissionError/OSError if the report is open in Excel (Windows
    locks it) - the caller retries later rather than losing the update."""
    rows = sorted(failures, key=lambda r: str(r["name"]).lower())
    dest = Path(dest)
    if not rows:
        try:
            dest.unlink()
        except FileNotFoundError:
            pass
        return 0
    tmp = dest.with_name(dest.name + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(FAILED_REPORT_COLUMNS)
        for r in rows:
            gave_up = r["status"] == "dead"
            status = (f"Failed - gave up after {r['attempts']} attempt(s)"
                      if gave_up else "Failed - will retry on next Extract")
            when = ""
            if r["updated_at"]:
                when = datetime.fromtimestamp(
                    r["updated_at"], timezone.utc).isoformat(timespec="seconds")
            w.writerow([
                hyperlink_cell(r["path"], r["name"]),
                status,
                " ".join(str(r["error"] or "").split()),
                f"{r['attempts']}/{max_attempts}",
                when,
                r["path"],
            ])
    os.replace(tmp, dest)
    return len(rows)
