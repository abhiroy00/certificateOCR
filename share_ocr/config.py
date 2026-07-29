"""Central configuration for the Share Certificate OCR pipeline.

Everything can be overridden with environment variables or the settings.json
file that lives next to the executable, so the GUI and the headless CLI always
agree on the same values.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List

APP_NAME = "Share Certificate OCR"
APP_VERSION = "1.0.0"

# Directory that holds the queue database, CSV shards, logs and thumbnails.
DEFAULT_WORKDIR = Path(os.environ.get("SHARE_OCR_HOME", Path.home() / ".share_ocr"))

SUPPORTED_IMG = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
SUPPORTED_DOC = (".pdf",)
SUPPORTED_EXT = SUPPORTED_IMG + SUPPORTED_DOC

# The exact fields extracted from every certificate, in CSV column order.
FIELDS: List[str] = [
    "company_name",
    "folio_no",
    "certificate_no",
    "share_holder_name",
    "registered_folio_no",   # add-on #12 in the quotation
    "no_of_shares",
    "no_of_shares_words",
    "distinctive_from",
    "distinctive_to",
    "date_of_issue",
    "latest_share_holder_name",
    "face_value_per_share",  # add-on #10 in the quotation
    "share_type",            # add-on #11 in the quotation
    "remarks",               # add-on #11 in the quotation
]

# Billed as optional add-ons in the quotation. They are NOT hard-required for a
# row to be considered clean, but when `flag_missing_addons` is on we raise a
# soft flag so nothing silently ships un-extracted.
ADDON_FIELDS: List[str] = [
    "face_value_per_share",
    "share_type",
    "registered_folio_no",
]

# Indian share certificates are routinely scanned front AND back: the reverse
# is a ruled "Memorandum of Transfers" table, not a second certificate. The
# model reports which side it is looking at in this control field, and
# BaseEngine.extract_file uses it to fold the pair into ONE record. It is
# deliberately NOT in CSV_COLUMNS - it never reaches the deliverable.
PAGE_KIND = "page_kind"
PAGE_CERTIFICATE = "certificate"
PAGE_TRANSFER = "transfer_memo"

# What the model is asked to return: the CSV fields plus the control field.
MODEL_KEYS: List[str] = FIELDS + [PAGE_KIND]

CSV_COLUMNS: List[str] = [
    "row_id",
    "source_file",
    "source_path",
    "page_no",
    "company_name",
    "share_type",
    "folio_no",
    "registered_folio_no",
    "certificate_no",
    "share_holder_name",
    "latest_share_holder_name",
    "no_of_shares",
    "no_of_shares_words",
    "face_value_per_share",
    "remarks",
    "distinctive_from",
    "distinctive_to",
    "date_of_issue",
    "validation_flags",
    "engine",
    "model",
    "latency_ms",
    "extracted_at",
]

PROMPT = """You are extracting data from a scanned SHARE CERTIFICATE image.
Return ONLY a valid JSON object with EXACTLY these keys:
{keys}

Rules:
- Keep folio_no, registered_folio_no, certificate_no, distinctive_from and
  distinctive_to as STRINGS (preserve leading zeros exactly as printed).
- no_of_shares must be an INTEGER (convert words like "ONE HUNDRED" to 100).
- no_of_shares_words = the amount exactly as written in words on the document
  (keep trailing words like "ONLY" if printed, e.g. "FIVE ONLY").
- date_of_issue in ISO format YYYY-MM-DD. The date line often ends with a
  place name ("1ST DAY OF DECEMBER 1994 AT INDORE") - ignore the place.
- share_holder_name: if several JOINT holders are listed one under another,
  return ALL of them separated by " / ". Keep titles like MRS. Do NOT append
  the postal address that often sits directly below the names.
- latest_share_holder_name = the transferee/endorsed holder if the certificate
  shows a transfer/endorsement, else the registered holder.

These three are contractual add-ons - extract them carefully, do not skip them:

- registered_folio_no: the folio printed in the "Regd. Folio No." / "Register
  Folio" box, which on many Indian certificates sits separately from the
  "Folio No." on the counterfoil. If the certificate prints only ONE folio
  number anywhere, put that same value in BOTH folio_no and
  registered_folio_no. Only use null if no folio number is printed at all.
  WARNING: many certificates print an extra UNLABELLED number between the
  "Reg. Folio No." box and the "Certificate No." box (e.g. "Reg. Folio No.
  8866    7866    Certificate No. 31013"). That middle number is an internal
  ledger/transfer number. Ignore it - it is neither a folio nor the
  certificate number. Folios may be alphanumeric (e.g. H3K12500); return them
  as printed, never as a number.
- face_value_per_share: the NOMINAL value of one share, normally printed as
  "Rs. 10/- each", "of Rs. 100 each fully paid up", "FV Rs. 2" or inside the
  capital clause. Return the per-share number only (10, not "Rs.10/-").
  If only a total paid-up amount is printed, divide it by no_of_shares and
  return that. Never copy no_of_shares into this field.
  Do NOT confuse face value with the amount PAID UP. "EQUITY SHARES EACH OF
  Rs. 10 ... AMOUNT PAID UP PER SHARE ON APPLICATION Rs. 5" means the face
  value is 10, not 5. Likewise a preference share "EACH OF RUPEES 50/-" whose
  paid-up value "stands reduced to RUPEES 30/-" has a face value of 50; the
  reduction belongs in remarks. It is legitimate for the face value to happen
  to equal the number of shares - do not second-guess a correct reading.
- share_type: one of "Equity", "Preference", "Ordinary", "Redeemable
  Preference", "Bonus" - read it from the certificate heading
  (e.g. "EQUITY SHARE CERTIFICATE", "7% CUMULATIVE PREFERENCE SHARES").
  If the heading only says "SHARE CERTIFICATE" with no qualifier, return
  "Equity" only when the body confirms equity, otherwise null.
- remarks: any endorsement, transfer stamp, duplicate/lien note, split or
  consolidation note, or hand-written annotation on the face or reverse.
  A large "ENDORSED" stamp across the holder block counts. So does a
  conversion/redemption condition such as "converts into 2 equity shares of
  Rs. 10 on redemption". Empty string if the certificate is clean.

- Read digits with extra care in distinctive_from / distinctive_to. The count
  of numbers in the range must equal no_of_shares, and the "to" number is
  always GREATER than the "from" number. If your reading breaks either rule,
  look at the digits again before answering. Preserve leading zeros exactly
  (0001863752, not 1863752).
- Certificate scans are often cropped at the edge. If the company name is cut
  off, return the part you can actually read - do not invent the missing
  words.
- If any field is not readable, use null. DO NOT guess or hallucinate.
  In particular, if the "Distinctive No(s)" box is BLANK, return null for
  distinctive_from and distinctive_to. Do NOT fill in a range that merely
  matches no_of_shares (1 to 50 for 50 shares) - an invented range is
  self-consistent, so nothing downstream can catch it.
- date_of_issue is the date the certificate was ISSUED - the one next to
  "Given under the Common Seal of the Company this ...". Certificates also
  carry other dates: allotment/call-payment stamps ("Amount paid up on
  Allotment 13 JUN 1990"), transfer dates, and revenue-stamp cancellations.
  Do not return those.

- page_kind: "certificate" if this page is the FACE of the certificate (it
  names the company and carries a certificate number, holder and share
  count). Use "transfer_memo" if it is the REVERSE - a ruled table headed
  "MEMORANDUM OF TRANSFERS OF SHARE(S) MENTIONED OVERLEAF".
  On a transfer_memo page:
    * every certificate field must be null. Do NOT invent a company name
      from a rubber stamp or letterhead abbreviation, and do NOT copy
      transfer numbers, IW numbers or register-folio numbers into folio_no,
      registered_folio_no, certificate_no, no_of_shares or distinctive_*.
    * latest_share_holder_name = the transferee on the LAST (most recent)
      filled row of the table.
    * remarks = the whole chain, oldest first, joined with "; ", each entry
      as "Transferred DD-MM-YY to NAME" (omit the date if it is not legible).
""".format(keys=json.dumps(MODEL_KEYS, indent=2))


# The only engines that exist. There is deliberately no demo/mock engine:
# fake data must never be able to reach a customer CSV.
REAL_ENGINES = ("openai", "tesseract")


@dataclass
class Settings:
    # --- engine -----------------------------------------------------------
    engine: str = os.environ.get("SHARE_OCR_ENGINE", "openai")  # openai | tesseract
    model: str = os.environ.get("SHARE_OCR_MODEL", "gpt-4o-mini")
    api_key_env: str = "OPENAI_API_KEY"
    base_url: str = os.environ.get("OPENAI_BASE_URL", "")
    # Full path to tesseract.exe / tesseract. Blank means "find it on PATH or
    # in the usual install folders".
    tesseract_cmd: str = os.environ.get("TESSERACT_CMD", "")

    # --- scale knobs ------------------------------------------------------
    workers: int = int(os.environ.get("SHARE_OCR_WORKERS", "8"))
    claim_batch: int = 200          # rows a worker pulls from the queue at once
    csv_flush_rows: int = 500       # rows buffered before fsync of the CSV shard
    csv_shard_rows: int = 200_000   # new CSV part file after this many rows
    max_attempts: int = 3
    flag_missing_addons: bool = True   # soft-flag the billed add-on fields
    retry_base_delay: float = 2.0
    request_timeout: int = 90
    max_image_px: int = 1600        # downscale before upload -> cheaper + faster
    jpeg_quality: int = 80
    pdf_dpi: int = 200

    # --- window state -----------------------------------------------------
    # Remembered between sessions so the app reopens where you left it, the
    # way a real desktop app does. Never contains anything sensitive.
    window_geometry: str = ""       # "WxH+X+Y" from Tk
    window_maximized: bool = False

    # --- paths ------------------------------------------------------------
    workdir: Path = field(default_factory=lambda: DEFAULT_WORKDIR)

    @property
    def db_path(self) -> Path:
        return self.workdir / "queue.db"

    @property
    def csv_dir(self) -> Path:
        return self.workdir / "csv"

    @property
    def log_path(self) -> Path:
        return self.workdir / "share_ocr.log"

    @property
    def thumb_dir(self) -> Path:
        return self.workdir / "thumbs"

    def ensure_dirs(self) -> None:
        for p in (self.workdir, self.csv_dir, self.thumb_dir):
            p.mkdir(parents=True, exist_ok=True)

    # --- persistence ------------------------------------------------------
    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        cfg = s.workdir / "settings.json"
        if cfg.exists():
            try:
                data = json.loads(cfg.read_text("utf-8"))
                for k, v in data.items():
                    if k == "workdir":
                        v = Path(v)
                    if hasattr(s, k):
                        setattr(s, k, v)
            except Exception:
                pass
        # Older builds had a demo "mock" engine that returned the same fake
        # certificate for every image, and it could stay saved in
        # settings.json. There is no demo engine now - fall back to real OCR.
        if s.engine not in REAL_ENGINES:
            s.engine = "openai"
        return s

    def save(self) -> None:
        self.ensure_dirs()
        data = asdict(self)
        data["workdir"] = str(self.workdir)
        (self.workdir / "settings.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )
