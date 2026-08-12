"""Score a real extraction CSV against hand-verified ground truth.

Why this exists
---------------
Before you quote a client an accuracy figure, measure it. This compares the
CSV the app produced against tests/golden/ground_truth.json (values read off
the scans by eye) and prints a per-field and per-certificate score.

Usage
-----
    # 1. put the sample scans in one folder, run them through the app
    python -m share_ocr.cli run C:\\Users\\vikal\\Downloads\\ocr
    python -m share_ocr.cli export C:\\temp\\out.csv

    # 2. score it
    python tools/check_accuracy.py C:\\temp\\out.csv

Only rows whose source_file appears in the ground truth are scored; the rest
are ignored, so you can point it at a big CSV that happens to contain the
sample files.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TRUTH = ROOT / "tests" / "golden" / "ground_truth.json"

# Fields worth scoring. Everything else is bookkeeping.
SCORED = [
    "company_name",
    "share_type",
    "folio_no",
    "registered_folio_no",
    "certificate_no",
    "share_holder_name",
    "no_of_shares",
    "face_value_per_share",
    "distinctive_from",
    "distinctive_to",
    "date_of_issue",
]

# Identifiers where a single wrong character matters and leading zeros count.
STRICT = {"folio_no", "registered_folio_no", "certificate_no",
          "distinctive_from", "distinctive_to", "date_of_issue"}


def norm(value, field: str) -> str:
    """Normalise for comparison without hiding real errors."""
    if value is None:
        return ""
    s = str(value).strip()
    if field in STRICT:
        # ignore only leading zeros, never other characters
        return s.upper().lstrip("0") or "0"
    if field == "no_of_shares" or field == "face_value_per_share":
        try:
            return str(int(float(s)))
        except ValueError:
            return s.upper()
    s = s.upper()
    s = s.replace("&", " AND ")
    s = re.sub(r"[.,()]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # LIMITED / LTD are the same company
    s = re.sub(r"\bLTD\b", "LIMITED", s)
    return s


def main(argv) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    csv_path = Path(argv[1])
    if not csv_path.exists():
        print("No such CSV: %s" % csv_path)
        return 2

    truth = json.loads(TRUTH.read_text("utf-8"))["certificates"]
    by_file = {c["source_file"]: c for c in truth}

    # The exported CSV uses pretty headers ("Script Name", ...); normalise
    # each row back to the internal snake_case keys this tool scores on.
    from share_ocr.config import HEADER_TO_KEY

    def _internal(r):
        return {HEADER_TO_KEY.get(k, k): v for k, v in r.items()}

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = [ir for r in csv.DictReader(f)
                for ir in [_internal(r)]
                if ir.get("source_file") in by_file]

    if not rows:
        print("None of the ground-truth files appear in that CSV.")
        print("Expected one of: %s" % ", ".join(sorted(by_file)))
        return 1

    field_hits = {f: [0, 0] for f in SCORED}      # [correct, total]
    print("=" * 74)
    for row in sorted(rows, key=lambda r: r["source_file"]):
        exp = by_file[row["source_file"]]
        wrong = []
        for f in SCORED:
            got, want = norm(row.get(f), f), norm(exp.get(f), f)
            field_hits[f][1] += 1
            if got == want:
                field_hits[f][0] += 1
            else:
                wrong.append((f, row.get(f), exp.get(f)))

        ok = len(SCORED) - len(wrong)
        print("%-28s %2d/%d fields" % (row["source_file"], ok, len(SCORED)))
        for f, got, want in wrong:
            print("    %-24s got %-28r want %r" % (f, got, want))
        must = exp.get("remarks_must_contain")
        if must and must.upper() not in str(row.get("remarks", "")).upper():
            print("    %-24s remarks should mention %r" % ("remarks", must))
        if row.get("validation_flags"):
            print("    flags: %s" % row["validation_flags"])

    print("=" * 74)
    print("Per-field accuracy across %d certificate(s):" % len(rows))
    total_c = total_n = 0
    for f in SCORED:
        c, n = field_hits[f]
        total_c += c
        total_n += n
        bar = "#" * int(round(20 * c / n)) if n else ""
        print("  %-24s %3d%%  %-20s %d/%d"
              % (f, round(100 * c / n) if n else 0, bar, c, n))
    print("-" * 74)
    print("  %-24s %3d%%        %d/%d fields"
          % ("OVERALL", round(100 * total_c / total_n) if total_n else 0,
             total_c, total_n))

    missing = sorted(set(by_file) - {r["source_file"] for r in rows})
    if missing:
        print("\nNot in the CSV (not scored): %s" % ", ".join(missing))
    return 0 if total_c == total_n else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
