"""Vision extraction engines + validation.

Engines
-------
openai     : vision LLM (gpt-4o / gpt-4o-mini / any OpenAI-compatible endpoint)
tesseract  : fully offline fallback (pytesseract + regex heuristics)

There is NO demo/mock engine. Both engines read the actual image; if one
cannot run it raises a clear error instead of inventing data.

Every engine returns a list of records (one per page).
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .config import ADDON_FIELDS, FIELDS, PROMPT, Settings
from .secrets import get_api_key


# ---------------------------------------------------------------- helpers --
def downscale_to_jpeg_b64(path: str, max_px: int, quality: int) -> str:
    """Shrink a big scan before upload. On 3M images this is the single
    biggest cost/latency saver (a 4000px scan -> 1600px is ~85% fewer bytes)."""
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        scale = min(1.0, max_px / float(max(w, h)))
        if scale < 1.0:
            im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def pdf_to_images(pdf_path: str, dpi: int) -> List[str]:
    from pdf2image import convert_from_path

    out: List[str] = []
    tmpdir = Path(tempfile.gettempdir()) / "share_ocr_pdf"
    tmpdir.mkdir(parents=True, exist_ok=True)
    for i, page in enumerate(convert_from_path(pdf_path, dpi=dpi)):
        p = tmpdir / f"{Path(pdf_path).stem}_{os.getpid()}_p{i}.jpg"
        page.save(p, "JPEG", quality=85)
        out.append(str(p))
    return out


def make_thumbnail(path: str, dest: Path, size: int = 160) -> Optional[str]:
    try:
        from PIL import Image, ImageOps

        dest.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((size, size))
            im.save(dest, "JPEG", quality=75)
        return str(dest)
    except Exception:
        return None


# ---------------------------------------------------------------- engines --
class BaseEngine:
    name = "base"

    def __init__(self, settings: Settings):
        self.s = settings

    def extract_image(self, image_path: str) -> Dict:  # pragma: no cover
        raise NotImplementedError

    def extract_file(self, path: str) -> List[Dict]:
        """Handle images and multi-page PDFs uniformly."""
        records: List[Dict] = []
        if path.lower().endswith(".pdf"):
            for i, img in enumerate(pdf_to_images(path, self.s.pdf_dpi), start=1):
                rec = self.extract_image(img)
                rec["page_no"] = i
                records.append(rec)
                try:
                    os.remove(img)
                except OSError:
                    pass
        else:
            rec = self.extract_image(path)
            rec["page_no"] = 1
            records.append(rec)
        return records


class OpenAIEngine(BaseEngine):
    name = "openai"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        from openai import OpenAI

        kwargs = {"timeout": settings.request_timeout,
                  "max_retries": 0}  # we do our own backoff in the worker
        if settings.base_url:
            kwargs["base_url"] = settings.base_url
        # Resolved from env -> OS keychain -> obfuscated local file.
        # Never read straight out of settings.json; the key is not stored there.
        key = get_api_key(settings)
        if not key:
            raise RuntimeError(
                "No OpenAI API key configured. Set it in the GUI "
                "(Settings -> API key), run `python -m share_ocr.cli key --set`, "
                f"or export {settings.api_key_env}.")
        kwargs["api_key"] = key
        self.client = OpenAI(**kwargs)

    def extract_image(self, image_path: str) -> Dict:
        b64 = downscale_to_jpeg_b64(image_path, self.s.max_image_px, self.s.jpeg_quality)
        resp = self.client.chat.completions.create(
            model=self.s.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}",
                                   "detail": "high"}},
                ],
            }],
        )
        data = json.loads(resp.choices[0].message.content)
        return {k: data.get(k) for k in FIELDS}


class TesseractEngine(BaseEngine):
    """Offline fallback. Much cheaper, less accurate on handwriting.
    Useful as a pre-filter: run tesseract on everything, send only the
    low-confidence ones to the LLM."""

    name = "tesseract"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._configure_binary(settings)

    @staticmethod
    def _candidates(settings: Settings) -> List[str]:
        found = []
        if getattr(settings, "tesseract_cmd", ""):
            found.append(settings.tesseract_cmd)
        on_path = shutil.which("tesseract")
        if on_path:
            found.append(on_path)
        found += [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            os.path.expandvars(
                r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
            "/usr/bin/tesseract", "/usr/local/bin/tesseract",
            "/opt/homebrew/bin/tesseract",
        ]
        return found

    def _configure_binary(self, settings: Settings) -> None:
        """Find tesseract.exe up front and fail with a fixable message.

        Previously a missing binary only blew up per image, deep inside a
        worker thread, which read as 'Tesseract does not work'.
        """
        try:
            import pytesseract
        except ImportError as exc:                    # pragma: no cover
            raise RuntimeError(
                "The pytesseract package is not installed. Run:\n"
                "    pip install pytesseract") from exc

        for cand in self._candidates(settings):
            if cand and Path(cand).exists():
                pytesseract.pytesseract.tesseract_cmd = cand
                break
        try:
            self.version = str(pytesseract.get_tesseract_version())
        except Exception as exc:                      # noqa: BLE001
            raise RuntimeError(
                "Tesseract OCR is not installed on this computer.\n\n"
                "Windows: install it from\n"
                "  https://github.com/UB-Mannheim/tesseract/wiki\n"
                "and keep the default folder "
                r"(C:\Program Files\Tesseract-OCR)." "\n"
                "macOS:   brew install tesseract\n"
                "Linux:   sudo apt install tesseract-ocr\n\n"
                "If it is installed somewhere unusual, set the full path to "
                "tesseract.exe in the TESSERACT_CMD environment variable."
            ) from exc

    PATTERNS = {
        "certificate_no": r"certificate\s*(?:no|number)[.:\s]*([A-Z0-9\-/]+)",
        "folio_no": r"folio\s*(?:no|number)[.:\s]*([A-Z0-9\-/]+)",
        # add-on: registered folio sits in its own "Regd. Folio" box
        "registered_folio_no":
            r"(?:regd?\.?|register(?:ed)?)\s*folio\s*(?:no|number)?[.:\s]*([A-Z0-9\-/]+)",
        "no_of_shares": r"(?:no\.?\s*of\s*shares|number\s*of\s*shares)[.:\s]*([0-9,]+)",
    }

    # add-on: face value printed in several different house styles
    FACE_VALUE_PATTERNS = [
        r"(?:rs\.?|inr|\u20b9)\s*([0-9]+(?:\.[0-9]+)?)\s*/?-?\s*(?:each|per\s*share)",
        r"of\s*(?:rs\.?|inr|\u20b9)\s*([0-9]+(?:\.[0-9]+)?)\s*/?-?\s*each",
        r"f\.?\s*v\.?\s*(?:rs\.?)?\s*([0-9]+(?:\.[0-9]+)?)",
        r"face\s*value[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)",
        r"nominal\s*value[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)",
    ]

    # add-on: share type / class
    SHARE_TYPE_PATTERNS = [
        (r"redeemable\s+preference", "Redeemable Preference"),
        (r"cumulative\s+preference", "Preference"),
        (r"preference\s+shares?", "Preference"),
        (r"equity\s+shares?", "Equity"),
        (r"ordinary\s+shares?", "Ordinary"),
        (r"bonus\s+shares?", "Bonus"),
    ]

    # add-on: remarks / endorsements worth surfacing to the reviewer
    REMARK_PATTERNS = [
        (r"\bduplicate\b", "DUPLICATE"),
        (r"\btransferr?ed\b|\btransferee\b", "TRANSFER ENDORSED"),
        (r"\blien\b", "LIEN NOTED"),
        (r"\bsplit\b", "SPLIT"),
        (r"\bconsolidat", "CONSOLIDATED"),
        (r"\bcancell?ed\b", "CANCELLED"),
    ]

    def extract_image(self, image_path: str) -> Dict:
        import pytesseract
        from PIL import Image, ImageOps

        with Image.open(image_path) as im:
            im = ImageOps.exif_transpose(im).convert("L")
            text = pytesseract.image_to_string(im)
        low = text.lower()
        rec: Dict = {k: None for k in FIELDS}
        for field, pat in self.PATTERNS.items():
            m = re.search(pat, low, re.I)
            if m:
                rec[field] = m.group(1).strip().upper()
        m = re.search(r"distinctive\s*(?:nos?|numbers?)[.:\s]*([0-9]+)\s*(?:to|-|–)\s*([0-9]+)",
                      low, re.I)
        if m:
            rec["distinctive_from"], rec["distinctive_to"] = m.group(1), m.group(2)
        m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?"
                      r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*,?\s*(\d{4})",
                      low, re.I)
        if m:
            months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                      "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
            rec["date_of_issue"] = "%04d-%02d-%02d" % (
                int(m.group(3)), months[m.group(2)[:3].lower()], int(m.group(1)))
        if rec.get("no_of_shares"):
            try:
                rec["no_of_shares"] = int(str(rec["no_of_shares"]).replace(",", ""))
            except ValueError:
                rec["no_of_shares"] = None

        # ---- add-on #10: face value per share --------------------------
        for pat in self.FACE_VALUE_PATTERNS:
            m = re.search(pat, low, re.I)
            if m:
                try:
                    v = float(m.group(1))
                    rec["face_value_per_share"] = int(v) if v.is_integer() else v
                    break
                except ValueError:
                    pass
        # fall back to total paid-up / no_of_shares
        if rec.get("face_value_per_share") is None and rec.get("no_of_shares"):
            m = re.search(r"(?:aggregating|total)[^0-9]{0,20}(?:rs\.?|\u20b9)?\s*([0-9,]+)",
                          low, re.I)
            if m:
                try:
                    total = float(m.group(1).replace(",", ""))
                    fv = total / float(rec["no_of_shares"])
                    rec["face_value_per_share"] = int(fv) if fv.is_integer() else round(fv, 2)
                except (ValueError, ZeroDivisionError):
                    pass

        # ---- add-on #11: share type ------------------------------------
        for pat, label in self.SHARE_TYPE_PATTERNS:
            if re.search(pat, low, re.I):
                rec["share_type"] = label
                break

        # ---- add-on #11: remarks ---------------------------------------
        notes = [label for pat, label in self.REMARK_PATTERNS
                 if re.search(pat, low, re.I)]
        rec["remarks"] = "; ".join(notes)

        # ---- add-on #12: registered folio fallback ---------------------
        # If only one folio number is printed, it serves as both.
        if not rec.get("registered_folio_no") and rec.get("folio_no"):
            rec["registered_folio_no"] = rec["folio_no"]

        rec["_raw_text"] = text[:2000]
        return rec


# Only real OCR engines. A demo engine that invents plausible-looking
# certificate data is dangerous in this product: fake rows are
# indistinguishable from real ones once they are in the CSV.
ENGINES = {"openai": OpenAIEngine, "tesseract": TesseractEngine}


def build_engine(settings: Settings) -> BaseEngine:
    cls = ENGINES.get(settings.engine)
    if cls is None:
        raise ValueError(
            "Unknown engine %r. Choose 'openai' or 'tesseract'." % settings.engine)
    return cls(settings)


# Denominations Indian companies actually issued shares in. Anything outside
# this set is usually the model picking up a paid-up amount, a total, or a
# stray number from elsewhere on the certificate.
COMMON_FACE_VALUES = {1.0, 2.0, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0, 500.0,
                      1000.0}


# ------------------------------------------------------------- validation --
def validate(rec: Dict, flag_addons: bool = True) -> str:
    """Return a semicolon-joined string of validation flags (empty = clean).

    `flag_addons` raises a soft flag when one of the billed add-on fields
    (face value, share type, registered folio) came back empty, so those rows
    land in needs-review instead of silently shipping blank.
    """
    flags: List[str] = []

    # 1) share count must equal the distinctive-number span
    try:
        d_from = int(str(rec["distinctive_from"]).strip())
        d_to = int(str(rec["distinctive_to"]).strip())
        span = d_to - d_from + 1
        if span <= 0:
            # Impossible range: the 'to' number came out lower than the
            # 'from'. In practice this is a single misread digit, so say so
            # plainly instead of reporting a negative span.
            flags.append(
                f"Distinctive range runs backwards ({d_from} to {d_to}) "
                "- likely a misread digit")
        elif rec.get("no_of_shares") is not None and span != int(rec["no_of_shares"]):
            flags.append(
                f"Share count {rec['no_of_shares']} != distinctive span {span}")
    except (TypeError, ValueError, KeyError):
        flags.append("Distinctive numbers missing/unreadable")

    # 2) required fields present
    for key in ("company_name", "certificate_no", "share_holder_name", "no_of_shares"):
        if not rec.get(key):
            flags.append(f"Missing {key}")

    # 3) date sanity
    if rec.get("date_of_issue"):
        try:
            d = datetime.strptime(str(rec["date_of_issue"]), "%Y-%m-%d")
            if not (1850 <= d.year <= datetime.now().year):
                flags.append("Date out of plausible range")
        except ValueError:
            flags.append("Bad date format")

    # 4) billed add-ons: soft flags, never block a row
    if flag_addons:
        missing = [f for f in ADDON_FIELDS if rec.get(f) in (None, "")]
        if missing:
            flags.append("Add-on not captured: " + ", ".join(missing))

    # 5) sanity on face value.
    #
    # Careful here. The FABWORTH sample is a real certificate for 50
    # preference shares of Rs 50 each - face value legitimately EQUALS the
    # share count. So plain equality is not evidence of an error and flagging
    # it produces false positives on exactly the kind of certificate a
    # reviewer would waste time on.
    #
    # What IS suspicious:
    #   a) equality on a large count (nobody issues 5,000 shares of Rs 5,000)
    #   b) a face value that is not one of the denominations Indian companies
    #      actually used
    fv, n = rec.get("face_value_per_share"), rec.get("no_of_shares")
    if fv is not None:
        try:
            fvf = float(fv)
            if n is not None and fvf == float(n) and float(n) > 1000:
                flags.append("Face value looks like the share count")
            elif fvf not in COMMON_FACE_VALUES:
                flags.append(f"Unusual face value ({fv}) - check it")
        except (TypeError, ValueError):
            pass

    return "; ".join(flags)
