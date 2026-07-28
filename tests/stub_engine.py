"""A test-only OCR engine.

The shipped application has exactly two engines, openai and tesseract, and
no demo engine - fake certificate data must never be able to reach a
customer CSV. The tests still need to run the whole pipeline on a machine
with no API key and no tesseract binary, so they register this stub at
runtime. It lives under tests/ and is never imported by the app.

Unlike the old demo engine it derives its values from the actual file name,
so a test that accidentally processes the same file twice produces the same
row twice and duplicate bugs stay visible.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict

from share_ocr import extractor
from share_ocr.extractor import BaseEngine

ENGINE_ID = "_test_stub"


class StubEngine(BaseEngine):
    name = ENGINE_ID

    def extract_image(self, image_path: str) -> Dict:
        from PIL import Image

        with Image.open(image_path) as im:            # prove the file is real
            im.size

        stem = Path(image_path).stem
        digits = re.findall(r"\d+", stem)
        n = int(digits[-1]) if digits else 0
        shares = 100 + n
        start = 4193501 + n * 1000
        return {
            "company_name": "TEST COMPANY %d LIMITED" % n,
            "folio_no": "F%05d" % n,
            "certificate_no": "C%05d" % n,
            "share_holder_name": "HOLDER %d" % n,
            "registered_folio_no": "F%05d" % n,
            "no_of_shares": shares,
            "no_of_shares_words": "ONE HUNDRED",
            "distinctive_from": str(start),
            "distinctive_to": str(start + shares - 1),
            "date_of_issue": "1993-05-15",
            "latest_share_holder_name": "HOLDER %d" % n,
            "face_value_per_share": 10,
            "share_type": "Equity",
            "remarks": "",
            "_stem": stem,
        }


def install() -> str:
    """Register the stub and return the engine id to put in Settings."""
    extractor.ENGINES[ENGINE_ID] = StubEngine
    return ENGINE_ID
