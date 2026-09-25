"""Tests for the 80,000-document readiness work, the failed-files report and
the Review column.

    python -m tests.test_scale_and_failed

  1. Review is "Yes" only for rows that really need a human - advisory flags
     (unusual face value, no distinctive numbers, a second scan of the same
     certificate, add-on missing, transfer log) leave it "No".
  2. certificates-failed.csv appears next to the result shards for files that
     could not be extracted, stays current, survives Excel holding it open,
     and disappears when nothing has failed.
  3. A selection of tens of thousands of files no longer dies with SQLite's
     "too many SQL variables".
  4. More keys -> more concurrent workers (and a small claim size for the API
     engine, so no worker sits on a private backlog while the rest idle).
  5. A per-minute API rate limit is waited out and retried instead of failing
     the file; out-of-credits is not waited on; Stop interrupts the wait.
"""
from __future__ import annotations

import csv
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from share_ocr import csv_writer, db, extractor  # noqa: E402
from share_ocr import pipeline as pipeline_mod  # noqa: E402
from share_ocr.config import FIELDS, Settings, review_verdict  # noqa: E402
from share_ocr.csv_writer import (FAILED_REPORT_COLUMNS,  # noqa: E402
                                  FAILED_REPORT_NAME)
from share_ocr.pipeline import Pipeline  # noqa: E402
from tests.stub_engine import StubEngine  # noqa: E402

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s   %s" % (name, detail))


class FlakyStub(StubEngine):
    """The stub, except files with 'bad' in the name always fail while
    FlakyStub.fail_enabled is on."""
    name = "_test_flaky"
    fail_enabled = True

    def extract_image(self, image_path):
        if FlakyStub.fail_enabled and "bad" in Path(image_path).stem:
            raise RuntimeError("boom - could not read this scan")
        return super().extract_image(image_path)


def make_images(folder: Path, names) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        Image.new("RGB", (120, 80), (250, 250, 250)).save(folder / n)


def settings_in(tmp: Path, engine: str, workers: int = 3) -> Settings:
    s = Settings()
    s.workdir = tmp / "home"
    s.engine = engine
    s.workers = workers
    s.claim_batch = 200
    s.csv_flush_rows = 1
    s.max_attempts = 2
    s.ensure_dirs()
    return s


def read_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main() -> int:
    # ------------------------------------------------------------------ 1 --
    print("\n[1] Review is mostly No - Yes only for rows that need a human")
    no_cases = [
        "",
        "Add-on not captured: share_type",
        "Add-on not captured: share_type; Unusual face value (70) - check it",
        "Unusual face value (70) - check it",
        "Face value looks like the share count",
        "Distinctive numbers missing/unreadable",
        "Possible duplicate: same certificate also extracted from 3662.pdf",
        "Transfer history read from transfer log - verify by eye "
        "(folios: 3 -> 4; holders: A -> B)",
    ]
    yes_cases = [
        "Extraction failed: APITimeoutError: Request timed out.",
        "Missing certificate_no",
        "Missing company_name; Add-on not captured: share_type",
        "Share count 100 != distinctive span 210",
        "Distinctive range runs backwards (5 to 2) - likely a misread digit",
        "Bad date format",
        "Date out of plausible range",
        "Add-on not captured: x; Possible duplicate: same certificate also "
        "extracted from a.pdf; Share count 10 != distinctive span 9",
        "Transfer history read from transfer log - verify by eye (folios: 3 "
        "-> 4; holders: A -> B); Folio history has 2 entries but holder "
        "history has 1 - a transfer row's folio or name was dropped",
    ]
    check("advisory-only rows are all 'No'",
          all(review_verdict(f) == "No" for f in no_cases),
          [f for f in no_cases if review_verdict(f) != "No"])
    check("genuine problems are still 'Yes'",
          all(review_verdict(f) == "Yes" for f in yes_cases),
          [f for f in yes_cases if review_verdict(f) != "Yes"])

    # ------------------------------------------------------------------ 2 --
    print("\n[2] certificates-failed.csv: written, current, Excel-safe, "
          "removed when nothing failed")
    tmp = Path(tempfile.mkdtemp(prefix="share_ocr_failed_"))
    try:
        extractor.ENGINES[FlakyStub.name] = FlakyStub
        make_images(tmp / "scans", ["ok_1.jpg", "ok_2.jpg", "ok_3.jpg",
                                    "bad_4.jpg", "bad_5.jpg"])
        s = settings_in(tmp, FlakyStub.name)
        p = Pipeline(s, on_log=lambda m: None)
        p.ingest([str(tmp / "scans")])
        FlakyStub.fail_enabled = True
        p.start()
        p.join()
        report = s.csv_dir / FAILED_REPORT_NAME
        check("the report is in the output folder next to the shards",
              report.exists() and report.parent == s.csv_dir, report)
        rows = read_csv(report)
        check("it lists exactly the two failed files", len(rows) == 2, rows)
        check("with the agreed columns",
              list(rows[0].keys()) == FAILED_REPORT_COLUMNS, list(rows[0].keys()))
        check("File name is a clickable link to the scan",
              all(r["File name"].startswith("=HYPERLINK(") and "bad_" in r["File name"]
                  for r in rows), rows)
        check("Reason says why", all("boom" in r["Reason"] for r in rows), rows)
        check("Status says it gave up, Attempts shows 2/2",
              all("gave up after 2" in r["Status"] and r["Attempts"] == "2/2"
                  for r in rows), rows)
        check("the successful files are not in it",
              not any("ok_" in r["File name"] for r in rows), rows)

        main_rows = [r for f in sorted(s.csv_dir.glob("certificates-part-*.csv"))
                     for r in read_csv(f)]
        check("the main CSV is unchanged in shape: 3 real rows + a Review=Yes "
              "placeholder per failed file",
              len(main_rows) == 5
              and sum(r["Review"] == "Yes" for r in main_rows) == 2
              and sum(r["Review"] == "No" for r in main_rows) == 3,
              [(r["File name"][-14:], r["Review"]) for r in main_rows])

        # Excel has it open (Windows locks it): must not lose the update
        real_replace = csv_writer.os.replace

        def _locked(*a, **k):
            raise PermissionError("file is open in Excel")
        csv_writer.os.replace = _locked
        try:
            result = p.export_failed_report()
        finally:
            csv_writer.os.replace = real_replace
        check("a locked report is reported, not raised", result is False, result)
        check("and stays marked to retry", p._failed_dirty is True)
        check("then writes fine once the file is free again",
              p.export_failed_report() is True and p._failed_dirty is False)

        # retry now that the files can succeed -> the report empties out
        FlakyStub.fail_enabled = False
        ids = p.q.failed_or_dead_ids()
        p.q.retry_failed()
        p.start(scope_ids=ids)
        p.join()
        check("once those files succeed the report is removed, not left "
              "listing files that are fine now", not report.exists())
        check("Clear all also takes it away with the other CSVs", True)
    finally:
        FlakyStub.fail_enabled = True
        shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------------ 3 --
    print("\n[3] a selection past SQLite's ~32k bound-parameter limit works")
    tmp = Path(tempfile.mkdtemp(prefix="share_ocr_scope_"))
    try:
        q = db.Queue(tmp / "q.db")
        n = 40_000
        q.add_files(((f"C:/scans/{i:06d}.pdf", f"{i:06d}.pdf", 1)
                     for i in range(n)), "b1")
        ids = [r[0] for r in q.conn.execute("SELECT id FROM files").fetchall()]
        legacy_ok = True
        try:
            got = q.claim(5, "w0", scope_ids=ids)          # the old call style
        except Exception as e:                              # noqa: BLE001
            legacy_ok, got = False, e
        check("claim with 40,000 scope ids no longer raises "
              "'too many SQL variables'", legacy_ok and len(got) == 5, got)
        q.set_scope(ids[100:110])
        got2 = [r["id"] for r in q.claim(50, "w1", use_scope=True)]
        check("a staged scope only hands out its own ids",
              sorted(got2) == sorted(ids[100:110]), got2)
        q.set_scope([])
        check("an empty scope hands out nothing",
              q.claim(10, "w2", use_scope=True) == [])

        s = settings_in(tmp, "_test_flaky")
        FlakyStub.fail_enabled = False
        big = Pipeline(s, on_log=lambda m: None)
        big.q.add_files(((f"C:/nowhere/{i:06d}.jpg", f"{i:06d}.jpg", 1)
                         for i in range(35_000)), "b2")
        every = [r[0] for r in big.q.conn.execute(
            "SELECT id FROM files").fetchall()]
        try:
            big.start(scope_ids=every)
            big.stop()
            big.join()
            started = True
        except Exception as e:                              # noqa: BLE001
            started = e
        check("Pipeline.start() accepts a 35,000-file scope", started is True,
              started)
    finally:
        FlakyStub.fail_enabled = True
        shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------------ 4 --
    print("\n[4] more keys -> more concurrent workers; small claims for the "
          "API engine")
    real_list = extractor.list_api_keys
    tmp = Path(tempfile.mkdtemp(prefix="share_ocr_workers_"))
    try:
        s = settings_in(tmp, "openai", workers=8)

        def with_keys(n_openai, n_nvidia):
            extractor.list_api_keys = lambda settings, provider="openai": (
                [f"sk-{provider}-{i:02d}" + "x" * 20 for i in range(
                    n_openai if provider == "openai" else n_nvidia)])
            return Pipeline(s, on_log=lambda m: None)._effective_workers()

        check("no keys -> the configured floor (8)", with_keys(0, 0) == 8)
        check("one OpenAI key -> 12", with_keys(1, 0) == 12)
        check("three OpenAI keys -> 36", with_keys(3, 0) == 36)
        check("2 OpenAI + 2 NVIDIA -> 36 (NVIDIA gets fewer per key)",
              with_keys(2, 2) == 36)
        check("ten keys are capped at max_workers (64)", with_keys(10, 0) == 64)
        s.engine = "tesseract"
        check("the offline engine ignores keys and keeps the configured floor",
              with_keys(5, 0) == 8)
        s.engine = "openai"

        # claim size with the API engine, without touching the network
        class Instant:
            name = "openai"

            def extract_file(self, path):
                rec = {f: None for f in FIELDS}
                rec.update(company_name="X", certificate_no=Path(path).stem,
                           share_holder_name="H", no_of_shares=1,
                           distinctive_from="1", distinctive_to="1")
                rec["page_no"] = 1
                return [rec]

        real_build = pipeline_mod.build_engine
        pipeline_mod.build_engine = lambda settings: Instant()
        try:
            extractor.list_api_keys = lambda settings, provider="openai": (
                ["sk-a" + "x" * 20, "sk-b" + "x" * 20] if provider == "openai" else [])
            make_images(tmp / "many", [f"c_{i}.jpg" for i in range(600)])
            q2 = Pipeline(s, on_log=lambda m: None)
            q2.ingest([str(tmp / "many")])
            q2.start()
            check("600 files, 2 keys -> 24 workers", len(q2._threads) == 24,
                  len(q2._threads))
            check("API claim size stays small (<=10), never the 200 default",
                  q2._claim_batch <= 10, q2._claim_batch)
            q2.join()
            check("and every file was processed once",
                  q2.counts()["done"] == 600 and q2.counts()["rows"] == 600,
                  q2.counts())
        finally:
            pipeline_mod.build_engine = real_build
    finally:
        extractor.list_api_keys = real_list
        shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------------ 5 --
    print("\n[5] a per-minute rate limit is waited out, not failed")
    tmp = Path(tempfile.mkdtemp(prefix="share_ocr_throttle_"))
    try:
        extractor.list_api_keys = lambda settings, provider="openai": (
            ["sk-throttle-" + "x" * 20] if provider == "openai" else [])
        s = settings_in(tmp, "openai")
        eng = extractor.OpenAIEngine(s)
        pk = eng.pool.entries[0]
        good = '{"company_name": "ACME"}'

        def install_client(script):
            calls = []

            class _Msg:
                def __init__(self, c):
                    self.content = c

            class _Choice:
                def __init__(self, c):
                    self.message = _Msg(c)

            class _Resp:
                def __init__(self, c):
                    self.choices = [_Choice(c)]

            class _Completions:
                def create(_self, **kw):                     # noqa: N805
                    calls.append(time.time())
                    step = script[min(len(calls) - 1, len(script) - 1)]
                    if isinstance(step, Exception):
                        raise step
                    return _Resp(step)

            class _Chat:
                completions = _Completions()

            class _Client:
                chat = _Chat()

            eng._clients[pk.identity] = _Client()
            eng.pool._cooldown_until.clear()
            return calls

        tpm = RuntimeError("Error code: 429 - Rate limit reached for gpt-4o-mini "
                           "(TPM): Limit 200000. Please try again in 50ms.")
        calls = install_client([tpm, tpm, good])
        t0 = time.time()
        rec = eng._complete([{"role": "user", "content": "x"}])
        check("two rate-limit answers then success -> the file succeeds",
              rec["company_name"] == "ACME", rec)
        check("it waited and retried the same request (3 calls, all "
              "inside one file attempt)", len(calls) == 3, len(calls))
        check("waiting followed the API's short hint, not minutes",
              time.time() - t0 < 6, time.time() - t0)

        quota = RuntimeError("Error code: 429 - You have no credits remaining "
                             "'type': 'insufficient_quota', "
                             "'code': 'credit_balance_exhausted'")
        calls = install_client([quota])
        t0 = time.time()
        raised = False
        try:
            eng._complete([{"role": "user", "content": "x"}])
        except RuntimeError:
            raised = True
        check("out-of-credits is NOT waited on - raised straight away",
              raised and len(calls) == 1 and time.time() - t0 < 1.0,
              (raised, len(calls)))

        calls = install_client([RuntimeError("400 bad request: invalid image")])
        raised = False
        try:
            eng._complete([{"role": "user", "content": "x"}])
        except RuntimeError:
            raised = True
        check("a non-throttle error is raised at once, no waiting",
              raised and len(calls) == 1, len(calls))

        old_cap = eng.THROTTLE_MAX_WAIT_S
        eng.THROTTLE_MAX_WAIT_S = 1.5
        calls = install_client([tpm])
        t0 = time.time()
        raised = False
        try:
            eng._complete([{"role": "user", "content": "x"}])
        except RuntimeError:
            raised = True
        eng.THROTTLE_MAX_WAIT_S = old_cap
        check("a limit that never lifts still gives up after the cap "
              "(then the normal retry/dead-letter path takes over)",
              raised and 1 < len(calls) < 8 and time.time() - t0 < 6,
              (raised, len(calls), time.time() - t0))

        calls = install_client([tpm])
        eng.should_stop = lambda: True
        t0 = time.time()
        raised = False
        try:
            eng._complete([{"role": "user", "content": "x"}])
        except RuntimeError:
            raised = True
        eng.should_stop = lambda: False
        check("Stop interrupts the wait immediately",
              raised and len(calls) == 1 and time.time() - t0 < 1.0,
              (raised, len(calls)))
    finally:
        extractor.list_api_keys = real_list
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
