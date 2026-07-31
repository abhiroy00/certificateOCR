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
    "latest_folio_no",
    "folio_no_history",
    "share_holder_history",
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
    "latest_folio_no",     # appended at the end, not inserted mid-list, so
                           # existing CSV shards from before this field
                           # existed don't get every later column shifted
    "folio_no_history",    # same reason - always append, never insert
    "share_holder_history",
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
- latest_folio_no = when a transfer/endorsement moved the shares to a new
  "Register Folio" / "Regd. Folio" number (printed next to the transferee
  in the endorsement, transfer stamp, or Memorandum of Transfers), return
  that new folio number here. Else latest_folio_no = folio_no.
- folio_no_history = every distinct folio number this certificate has ever
  been registered under, oldest first, as a single string separated by
  " -> " (e.g. "000003 -> 0015145 -> 00017369"). Start with folio_no (the
  original), then add one more entry per transfer/endorsement that shows a
  new folio number, in date order. If there are no transfers, or none of
  them print a folio number, folio_no_history = folio_no (just the one
  value, no arrows).
- share_holder_history = every holder this certificate has ever been
  registered to, oldest first, as a single string separated by " -> "
  (e.g. "ABHAY AJMERA / AJAY AJMERA -> SHRI KIRTILAL M SHAH -> SHRI PRAKASH
  CHASKAR -> BHUPENDRA DANGARWALA / RUPA DANGARWALA"). Start with
  share_holder_name (the original registered holder(s)), then add one more
  entry per transfer/endorsement naming a transferee, in the SAME date
  order as folio_no_history - entry N here corresponds to entry N there.
  If there are no transfers, share_holder_history = share_holder_name (just
  the one value, no arrows). latest_share_holder_name is always the LAST
  value in share_holder_history.

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
""".format(keys=json.dumps(FIELDS, indent=2))

# Used instead of PROMPT when a scanned PDF has more than one page. Indian
# share certificates routinely print a "MEMORANDUM OF TRANSFERS" ledger on
# the physical reverse of the SAME certificate, which a PDF scan captures as
# page 2. Sending that page to the model on its own (with no page-1 context)
# made it force-fit the transfer ledger into the certificate schema and
# invent a bogus second "certificate" - wrong folio, wrong certificate
# number, company name misread off a watermark. Sending every page together
# and telling the model what page 2 actually is fixes that at the source.
MULTI_PAGE_NOTE = """

You were given MULTIPLE page images of the SAME physical share certificate,
in order (page 1 = the front; later pages = the reverse side of that same
sheet, or a continuation). Return ONE JSON record for the whole certificate
- never one record per page.

- folio_no, registered_folio_no, certificate_no, share_holder_name,
  no_of_shares, no_of_shares_words, distinctive_from, distinctive_to,
  date_of_issue, face_value_per_share and share_type all come from the
  FRONT page only.
- A later page titled "MEMORANDUM OF TRANSFERS", "TRANSFER OF SHARES" or
  similar is NOT a separate certificate and NOT a second row. It is a log
  of later ownership changes for this same certificate. Use the LAST
  (most recent / bottom-most dated) entry in that log to:
    * set latest_share_holder_name to that transferee's name (if the log
      is empty, latest_share_holder_name = share_holder_name)
    * set latest_folio_no to the "Register Folio" / "Regd. Folio" number
      printed on that same last entry, if one is printed there (if the log
      is empty or prints no folio, latest_folio_no = folio_no)
      WARNING: a transfer-log entry commonly prints THREE similar-looking
      reference numbers side by side in the same row, e.g.
      "TRF. No.: 003025   IW. No.: 002641   FOLIO NO. 00017369". Only the
      one actually labelled "Folio No." / "Regd. Folio" / "Register Folio"
      is latest_folio_no. "TRF. No." / "Transfer No." is which transfer
      this is, and "IW. No." is an unrelated internal instrument/warrant
      number - never use either of those as latest_folio_no even though
      they sit right next to it and are the same length.
    * append a short note to remarks, e.g. "Transferred to R MEENAKSHI on
      30/08/96"
  Never copy a transfer-log entry's folio or transfer number into folio_no,
  registered_folio_no or certificate_no - those always describe the
  ORIGINAL certificate from the front page. latest_folio_no is the one
  exception: it is deliberately read from the transfer log, not the front.
- The log commonly has MORE THAN ONE filled-in row (one per historical
  transfer, oldest at the top). Read every filled row, top to bottom, and
  build folio_no_history as: folio_no, then each row's own Folio No. in
  order, joined with " -> " (e.g. a certificate originally on folio 000003,
  transferred once onto folio 0015145, then again onto folio 00017369,
  gives folio_no_history = "000003 -> 0015145 -> 00017369"). Apply the same
  IW.No./Transfer No. vs Folio No. label check to every row, not just the
  last one. latest_folio_no is always the LAST value in folio_no_history.
- Build share_holder_history the same way, in lock-step with folio_no_history:
  start with share_holder_name, then each filled row's transferee name, top
  to bottom, joined with " -> ". Every entry in folio_no_history must have
  a matching entry in share_holder_history at the same position - if a row
  has a name but no readable folio, still add the name and repeat the
  previous folio in folio_no_history at that position (do not simply drop
  a row from one history but not the other).
- If a later page is blank or unrelated, ignore it.
"""
PROMPT_MULTI_PAGE = PROMPT + MULTI_PAGE_NOTE


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
