"""End-to-end smoke test using the test-only stub engine.

    python -m tests.test_pipeline
"""
from __future__ import annotations

import csv
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from share_ocr.config import Settings  # noqa: E402
from share_ocr.extractor import validate  # noqa: E402
from share_ocr.pipeline import Pipeline, scan_paths  # noqa: E402
from tests.stub_engine import install as install_stub  # noqa: E402


def make_fixtures(root: Path, n: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        sub = root / f"box{i % 5}"
        sub.mkdir(exist_ok=True)
        Image.new("RGB", (600, 400), (240, 240, 250)).save(sub / f"cert_{i:05d}.jpg")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="share_ocr_test_"))
    scans = tmp / "scans"
    n = 120
    make_fixtures(scans, n)

    s = Settings()
    s.workdir = tmp / "home"
    s.engine = install_stub()
    s.workers = 6
    s.claim_batch = 10
    s.csv_flush_rows = 25
    s.csv_shard_rows = 50           # force multiple shards
    s.ensure_dirs()

    # 1) scanner finds everything, recursively
    found = list(scan_paths(str(scans)))
    assert len(found) == n, f"scan found {len(found)} of {n}"

    # 2) run the pipeline
    p = Pipeline(s, on_log=lambda m: None)
    added = p.ingest([str(scans)])
    assert added == n, f"ingest queued {added} of {n}"
    p.start()
    p.join()

    c = p.counts()
    assert c["done"] == n, c
    assert c["rows"] == n, c

    # 3) idempotent re-ingest (no duplicates)
    assert p.ingest([str(scans)]) == 0, "re-ingest created duplicates"

    # 4) CSV shards written and mergeable
    shards = sorted(s.csv_dir.glob("certificates-part-*.csv"))
    assert len(shards) >= 2, f"expected shards, got {shards}"
    merged = p.export_single_csv(str(tmp / "certificates.csv"))
    # The CSV uses pretty headers now; normalise back to internal keys.
    from share_ocr.config import HEADER_TO_KEY
    with open(merged, encoding="utf-8-sig", newline="") as f:
        rows = [{HEADER_TO_KEY.get(k, k): v for k, v in r.items()}
                for r in csv.DictReader(f)]
    assert len(rows) == n, f"merged csv has {len(rows)} rows"
    assert rows[0]["company_name"].startswith("TEST COMPANY"), rows[0]["company_name"]
    assert rows[0]["distinctive_from"].isdigit(), rows[0]["distinctive_from"]
    assert rows[0]["validation_flags"] == ""

    # 4b) the three billed add-ons must be present as CSV columns AND populated
    for col in ("face_value_per_share", "share_type", "registered_folio_no",
                "remarks"):
        assert col in rows[0], f"add-on column {col} missing from CSV"
    assert rows[0]["face_value_per_share"] == "10", rows[0]["face_value_per_share"]
    assert rows[0]["share_type"] == "Equity", rows[0]["share_type"]
    assert rows[0]["registered_folio_no"] == rows[0]["folio_no"], rows[0]

    # 5) validation logic
    bad = {"company_name": "X", "certificate_no": "1", "share_holder_name": "Y",
           "no_of_shares": 100, "distinctive_from": "1", "distinctive_to": "50",
           "date_of_issue": "15-05-1993"}
    flags = validate(bad)
    assert "distinctive span" in flags and "Bad date format" in flags, flags

    clean_core = {"distinctive_from": "1", "distinctive_to": "100",
                  "no_of_shares": 100, "company_name": "A",
                  "certificate_no": "B", "share_holder_name": "C",
                  "date_of_issue": "1993-05-15"}

    # add-ons missing -> soft flag naming exactly which ones
    soft = validate(clean_core)
    assert soft.startswith("Add-on not captured"), soft
    for f in ("face_value_per_share", "share_type", "registered_folio_no"):
        assert f in soft, soft

    # add-ons switched off -> clean
    assert validate(clean_core, flag_addons=False) == ""

    # add-ons present -> clean
    full = dict(clean_core, face_value_per_share=10, share_type="Equity",
                registered_folio_no="8866")
    assert validate(full) == "", validate(full)

    # face value must never be the share count
    swapped = dict(full, no_of_shares=5000, face_value_per_share=5000,
                   distinctive_from="1", distinctive_to="5000")
    assert "Face value looks like the share count" in validate(swapped)

    # FABWORTH is a real certificate for 50 preference shares of Rs 50 each,
    # so face value == share count must NOT be flagged at this size
    small = dict(clean_core, no_of_shares=50, face_value_per_share=50,
                 distinctive_from="1", distinctive_to="50",
                 share_type="Preference", registered_folio_no="054062")
    assert validate(small) == "", validate(small)

    # a distinctive range that runs backwards is reported as a misread digit,
    # not as a negative span
    backwards = dict(clean_core, no_of_shares=50, face_value_per_share=10,
                     share_type="Preference", registered_folio_no="054062",
                     distinctive_from="9210801", distinctive_to="9210650")
    assert "runs backwards" in validate(backwards), validate(backwards)

    # 6) resume behaviour: kill half, requeue, finish
    p.q.conn.execute("UPDATE files SET status='running', claimed_at=0"
                     " WHERE id % 3 = 0")
    requeued = p.q.requeue_stale(older_than_s=1)
    assert requeued > 0
    assert p.q.counts()["pending"] == requeued

    # 7) start() must not let a worker's cached engine (built off whatever
    # API keys/settings existed the first time that thread id ever called
    # _engine()) outlive the run it was built for. Python/the OS can and
    # does recycle a terminated thread's id for a brand new thread, so
    # without clearing this cache at the top of every start(), a key added
    # (or removed, or a model changed) after the first Extract of the
    # session could silently never take effect for the rest of the app's
    # life - see Pipeline.start().
    p._engines[999999] = "stale-engine-sentinel"
    p.start()
    assert 999999 not in p._engines, (
        "start() left a stale cached engine in place - "
        "newly added/removed API keys would never be picked up")
    p.stop()
    p.join()

    # 8) claim_batch (200 by default) is sized for the 30-lakh job. On a
    # normal day-to-day run - tens to a few hundred certificates, everything
    # this app is actually run on most of the time - 200 is bigger than the
    # whole job, so the FIRST worker to call claim() used to take every file
    # in one shot and every other configured worker (and every extra API key
    # in the pool) sat idle for the entire run. start() must size THIS run's
    # claim to a fair per-worker share instead, capped by claim_batch.
    small_tmp = Path(tempfile.mkdtemp(prefix="share_ocr_fairshare_"))
    small_scans = small_tmp / "scans"
    make_fixtures(small_scans, 24)
    s2 = Settings()
    s2.workdir = small_tmp / "home"
    s2.engine = install_stub()
    s2.workers = 4
    s2.claim_batch = 200          # the large, mega-batch default
    s2.csv_flush_rows = 1
    s2.ensure_dirs()
    p2 = Pipeline(s2, on_log=lambda m: None)
    p2.ingest([str(small_scans)])
    p2.start()
    assert p2._claim_batch == 6, (          # ceil(24 files / 4 workers)
        f"expected a fair 6-file share per worker, got {p2._claim_batch} - "
        "a worker would grab everything and the others would sit idle")
    p2.join()
    assert p2.counts()["rows"] == 24, p2.counts()
    shutil.rmtree(small_tmp, ignore_errors=True)

    print(f"OK  {n} files -> {c['rows']} rows -> {len(shards)} CSV shard(s)")
    print(f"    merged: {merged}")
    print(f"    resume: {requeued} stale rows re-queued")
    print(f"    fair-share claim_batch: 24 files / 4 workers -> {p2._claim_batch}/claim")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
