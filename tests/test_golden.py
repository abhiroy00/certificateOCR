"""Validator checks driven by the four real sample certificates.

These do NOT run OCR (no API key, no scans in the repo). They feed the
hand-verified ground truth through validate() and assert that a perfectly
read certificate comes out CLEAN. Any rule that flags one of these is a rule
that would send correct work to a human reviewer at 30 lakh scale.

Run: python -m tests.test_golden
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from share_ocr.extractor import validate  # noqa: E402

TRUTH = ROOT / "tests" / "golden" / "ground_truth.json"

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok    %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s   %s" % (name, detail))


def main() -> int:
    certs = json.loads(TRUTH.read_text("utf-8"))["certificates"]
    check("ground truth file has all four samples", len(certs) == 4, len(certs))

    print("\n[1] a perfectly read certificate must produce zero flags")
    for c in certs:
        rec = {k: v for k, v in c.items() if not k.startswith("_")
               and k not in ("source_file", "remarks_must_contain")}
        rec.setdefault("remarks", c.get("remarks_must_contain", ""))
        flags = validate(rec)
        check(c["source_file"], flags == "", flags)

    print("\n[2] FABWORTH: face value 50 on 50 shares is REAL, not an error")
    fab = next(c for c in certs if "FABWORTH" in c["company_name"])
    rec = {k: v for k, v in fab.items() if not k.startswith("_")
           and k != "source_file"}
    check("no face-value flag on the FABWORTH row",
          "ace value" not in validate(rec), validate(rec))

    print("\n[3] TATA: alphanumeric folio and 4 leading zeros survive")
    tata = next(c for c in certs if "TATA" in c["company_name"])
    check("folio kept as printed", tata["folio_no"] == "H3K12500")
    check("leading zeros kept", tata["distinctive_from"] == "0001863752")
    check("5 shares match the distinctive span",
          int(tata["distinctive_to"]) - int(tata["distinctive_from"]) + 1
          == tata["no_of_shares"])
    check("all three joint holders captured",
          tata["share_holder_name"].count("/") == 2, tata["share_holder_name"])

    print("\n[4] the errors we actually saw in the live run are still caught")
    # the real gpt-4o-mini misread: 09210850 -> 09210650
    misread = dict(rec, distinctive_to="09210650")
    check("backwards range caught", "runs backwards" in validate(misread),
          validate(misread))
    # face value picked up from a paid-up amount that is not a denomination
    odd = dict(rec, face_value_per_share=30)
    check("non-denomination face value caught",
          "Unusual face value" in validate(odd), validate(odd))
    # share count that does not match the span
    short = dict(rec, no_of_shares=40)
    check("count vs span mismatch caught",
          "distinctive span" in validate(short), validate(short))

    print("\n[5] every scored field is present in the CSV layout")
    from share_ocr.config import HEADER_TO_KEY
    exported = set(HEADER_TO_KEY.values())      # internal keys the CSV carries
    sys.path.insert(0, str(ROOT / "tools"))
    import check_accuracy
    missing = [f for f in check_accuracy.SCORED if f not in exported]
    check("accuracy tool only scores real CSV columns", not missing, missing)

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
