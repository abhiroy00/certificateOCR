#!/usr/bin/env python3
"""Entry point for the desktop app: python run_gui.py"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from share_ocr.config import Settings          # noqa: E402
from share_ocr.gui import main                 # noqa: E402

if __name__ == "__main__":
    s = Settings.load()
    s.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
        handlers=[logging.FileHandler(s.log_path, encoding="utf-8"),
                  logging.StreamHandler()],
    )
    try:
        main()
    except KeyboardInterrupt:
        # Ctrl+C in the launching console is a normal way to quit; don't
        # frighten the operator with a traceback.
        print("\nClosed.")
