#!/usr/bin/env python3
"""Headless bulk runner — this is what you use for the 30-lakh job.

Examples
--------
  # index + process a whole tree with 32 workers
  python -m share_ocr.cli run /mnt/scans --workers 32

  # only index now, process later (or from another machine on the same share)
  python -m share_ocr.cli ingest /mnt/scans
  python -m share_ocr.cli run --workers 48

  # progress / failures / export
  python -m share_ocr.cli status
  python -m share_ocr.cli retry
  python -m share_ocr.cli export /out/certificates.csv

Run the same `run` command on N machines pointed at the same queue.db
(on shared storage) and they will co-operate without duplicating work.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from .config import Settings
from .pipeline import Pipeline


def human(n: float) -> str:
    n = int(n)
    h, rem = divmod(n, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}h{m:02d}m{s:02d}s"


def build_settings(args) -> Settings:
    s = Settings.load()
    for key in ("engine", "model", "workers", "max_image_px", "claim_batch"):
        v = getattr(args, key, None)
        if v:
            setattr(s, key, v)
    if getattr(args, "workdir", None):
        from pathlib import Path
        s.workdir = Path(args.workdir)
    s.ensure_dirs()
    return s


def cmd_ingest(args) -> int:
    s = build_settings(args)
    p = Pipeline(s, on_log=print)
    t0 = time.time()
    n = p.ingest(args.paths)
    print(f"Queued {n:,} new file(s) in {time.time() - t0:.1f}s")
    print(p.counts())
    return 0


def cmd_run(args) -> int:
    s = build_settings(args)
    p = Pipeline(s, on_log=lambda m: logging.info(m))
    if args.paths:
        p.ingest(args.paths)

    signal.signal(signal.SIGINT, lambda *_: (print("\nStopping… progress is saved"),
                                             p.stop()))
    p.start()
    last = 0
    while p.running:
        time.sleep(2)
        st = p.stats
        done = st.done + st.failed
        if done != last:
            last = done
            pct = 100.0 * done / max(1, st.total)
            print(f"\r{done:,}/{st.total:,} ({pct:5.1f}%)  "
                  f"{st.rate:6.1f} files/s  rows={st.rows:,}  "
                  f"failed={st.failed:,}  ETA {human(st.eta_seconds or 0)}",
                  end="", flush=True)
    p.join()
    print("\nDone.", p.counts())
    print(f"CSV shards: {s.csv_dir}")
    return 0


def cmd_status(args) -> int:
    s = build_settings(args)
    p = Pipeline(s)
    c = p.counts()
    print(f"total    : {c['total']:,}")
    for k in ("pending", "running", "done", "failed", "dead"):
        print(f"{k:9s}: {c.get(k, 0):,}")
    print(f"rows     : {c['rows']:,}")
    print(f"csv dir  : {s.csv_dir}")
    return 0


def cmd_retry(args) -> int:
    s = build_settings(args)
    p = Pipeline(s)
    print(f"{p.q.retry_failed():,} file(s) re-queued")
    return 0


def cmd_failures(args) -> int:
    s = build_settings(args)
    p = Pipeline(s)
    for r in p.q.failures(args.limit):
        print(f"{r['attempts']}x  {r['name']}  ::  {r['error']}")
    return 0


def cmd_export(args) -> int:
    s = build_settings(args)
    p = Pipeline(s)
    print("Merged ->", p.export_single_csv(args.dest))
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="share_ocr",
                                 description="Share certificate OCR at scale")
    ap.add_argument("--workdir")
    ap.add_argument("--engine", choices=["openai", "tesseract"])
    ap.add_argument("--model")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--max-image-px", dest="max_image_px", type=int)
    ap.add_argument("--claim-batch", dest="claim_batch", type=int)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ingest", help="index files into the queue")
    sp.add_argument("paths", nargs="+")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("run", help="process the queue")
    sp.add_argument("paths", nargs="*")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("status"); sp.set_defaults(func=cmd_status)
    sp = sub.add_parser("retry"); sp.set_defaults(func=cmd_retry)

    sp = sub.add_parser("failures")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_failures)

    sp = sub.add_parser("export", help="merge CSV shards into one file")
    sp.add_argument("dest")
    sp.set_defaults(func=cmd_export)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
