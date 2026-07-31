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
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .config import ADDON_FIELDS, FIELDS, PROMPT, PROMPT_MULTI_PAGE, Settings
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


def _page_jpeg_path(pdf_path: str, index: int) -> Path:
    tmpdir = Path(tempfile.gettempdir()) / "share_ocr_pdf"
    tmpdir.mkdir(parents=True, exist_ok=True)
    # Thread id as well as pid: workers are threads, so two of them rendering
    # different PDFs that happen to share a stem would otherwise write to the
    # same filename and read each other's pages.
    return tmpdir / (f"{Path(pdf_path).stem}_{os.getpid()}"
                     f"_{threading.get_ident()}_p{index}.jpg")


def pdf_to_images(pdf_path: str, dpi: int,
                  max_pages: Optional[int] = None) -> List[str]:
    """Render PDF pages to JPEGs on disk, one per page.

    Two renderers are supported:
      * pdf2image + poppler - what the README tells people to install, used
        when the poppler binaries (pdftoppm/pdftocairo) are actually on PATH.
      * PyMuPDF (pip install pymupdf) - a pure pip wheel with no external
        binary to install, so PDFs still work out of the box on a fresh
        Windows machine where nobody has set up poppler yet.

    `max_pages` stops after that many pages, which is what makes a cheap
    first-page preview possible without rendering a whole document.
    """
    if shutil.which("pdftoppm") or shutil.which("pdftocairo"):
        from pdf2image import convert_from_path
        kwargs = {"dpi": dpi}
        if max_pages:
            kwargs["first_page"] = 1
            kwargs["last_page"] = max_pages
        out: List[str] = []
        for i, page in enumerate(convert_from_path(pdf_path, **kwargs)):
            p = _page_jpeg_path(pdf_path, i)
            page.save(p, "JPEG", quality=85)
            out.append(str(p))
        return out

    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise RuntimeError(
            "Cannot read PDFs: poppler is not on PATH and PyMuPDF is not "
            "installed. Run 'pip install pymupdf' (no extra setup needed), "
            "or install poppler - see README for platform instructions."
        ) from e

    out = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    doc = fitz.open(pdf_path)
    try:
        for i, page in enumerate(doc):
            if max_pages and i >= max_pages:
                break
            pix = page.get_pixmap(matrix=matrix)
            p = _page_jpeg_path(pdf_path, i)
            pix.save(str(p))
            out.append(str(p))
    finally:
        doc.close()
    return out


# Enough to read a certificate layout at thumbnail size, cheap enough that a
# strip of a dozen previews renders in well under a second.
THUMB_PDF_DPI = 60


def make_thumbnail(path: str, dest: Path, size: int = 160) -> Optional[str]:
    """A small JPEG preview of a scan. Handles PDFs as well as images.

    PDFs used to be skipped outright, so a folder of PDF certificates showed
    an empty preview strip - exactly the files an operator most wants to
    eyeball before extracting.
    """
    scratch = None
    try:
        from PIL import Image, ImageOps

        src = path
        if path.lower().endswith(".pdf"):
            pages = pdf_to_images(path, THUMB_PDF_DPI, max_pages=1)
            if not pages:
                return None
            src = scratch = pages[0]

        dest.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((size, size))
            im.save(dest, "JPEG", quality=75)
        return str(dest)
    except Exception:
        return None
    finally:
        if scratch:
            try:
                os.remove(scratch)
            except OSError:
                pass


# ---------------------------------------------------------------- engines --
class BaseEngine:
    name = "base"

    # A physical certificate scanned to PDF is normally 1-2 pages (front,
    # optionally its own reverse). A PDF with more pages than this is
    # presumed to be a genuine multi-certificate batch scan instead, so it
    # falls back to one row per page rather than being forced into one
    # record.
    MAX_PAGES_PER_CERTIFICATE = 4

    # The reverse page's transfer log packs 3 similar reference numbers
    # (Transfer No. / IW. No. / Folio No.) into a few square millimetres,
    # often over a repeating watermark. At the bulk-run defaults (200 dpi,
    # 1600px upload cap) the model reliably confused IW. No. for the folio.
    # This only applies to the combined front+back read (extract_document),
    # a small fraction of total volume, so it doesn't raise cost on the
    # single-page bulk path the 30-lakh run actually depends on.
    MULTI_PAGE_DPI = 300
    MULTI_PAGE_MAX_PX = 2400

    def __init__(self, settings: Settings):
        self.s = settings

    def extract_image(self, image_path: str) -> Dict:  # pragma: no cover
        raise NotImplementedError

    def extract_document(self, image_paths: List[str]) -> Dict:
        """Extract one record from every page of a single certificate.

        Default: only the front page (the first image) is authoritative;
        used by engines that cannot reason across multiple images at once.
        Vision-LLM engines override this to actually read every page.
        """
        return self.extract_image(image_paths[0])

    def extract_file(self, path: str) -> List[Dict]:
        """Handle images and multi-page PDFs uniformly."""
        if not path.lower().endswith(".pdf"):
            rec = self.extract_image(path)
            rec["page_no"] = 1
            return [rec]

        # Render at the higher multi-page DPI first, capped just past the
        # certificate-page threshold. If the PDF turns out to have more
        # pages than that, it's the "genuine multi-certificate batch scan"
        # case, and those pages get processed at the normal bulk DPI instead
        # (no cost regression there - only the combined-read path is pricier).
        probe = pdf_to_images(path, self.MULTI_PAGE_DPI,
                              max_pages=self.MAX_PAGES_PER_CERTIFICATE + 1)
        try:
            if probe and len(probe) <= self.MAX_PAGES_PER_CERTIFICATE:
                rec = self.extract_document(probe)
                rec["page_no"] = 1
                return [rec]
        finally:
            for img in probe:
                try:
                    os.remove(img)
                except OSError:
                    pass

        imgs = pdf_to_images(path, self.s.pdf_dpi)
        try:
            records: List[Dict] = []
            for i, img in enumerate(imgs, start=1):
                rec = self.extract_image(img)
                rec["page_no"] = i
                records.append(rec)
            return records
        finally:
            for img in imgs:
                try:
                    os.remove(img)
                except OSError:
                    pass


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

    def extract_document(self, image_paths: List[str]) -> Dict:
        """Read every page of one certificate in a single request, so a
        reverse-side "Memorandum of Transfers" page is read as part of the
        same certificate instead of being force-fit into its own row."""
        if len(image_paths) == 1:
            return self.extract_image(image_paths[0])
        content: List[Dict] = [{"type": "text", "text": PROMPT_MULTI_PAGE}]
        for p in image_paths:
            b64 = downscale_to_jpeg_b64(p, self.MULTI_PAGE_MAX_PX, self.s.jpeg_quality)
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}",
                                          "detail": "high"}})
        resp = self.client.chat.completions.create(
            model=self.s.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": content}],
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

        # No transfer log visible on a single page -> nothing has changed
        # since issue, so "latest" is just what's on the front.
        rec["latest_share_holder_name"] = (
            rec.get("latest_share_holder_name") or rec.get("share_holder_name"))
        rec["latest_folio_no"] = rec.get("latest_folio_no") or rec.get("folio_no")
        rec["folio_no_history"] = rec.get("folio_no_history") or rec.get("folio_no")
        rec["share_holder_history"] = (
            rec.get("share_holder_history") or rec.get("share_holder_name"))

        rec["_raw_text"] = text[:2000]
        return rec

    def extract_document(self, image_paths: List[str]) -> Dict:
        """Front page is authoritative for every structured field. Later
        pages (typically the certificate's own reverse - a "Memorandum of
        Transfers" ledger) cannot be reasoned about the way a vision LLM
        can, but their OCR text is still scanned for endorsement keywords
        so that note isn't silently lost."""
        rec = self.extract_image(image_paths[0])
        extra_notes = []
        for img in image_paths[1:]:
            import pytesseract
            from PIL import Image, ImageOps

            with Image.open(img) as im:
                im = ImageOps.exif_transpose(im).convert("L")
                text = pytesseract.image_to_string(im)
            low = text.lower()
            extra_notes += [label for pat, label in self.REMARK_PATTERNS
                           if re.search(pat, low, re.I)]
            if re.search(r"memorandum\s+of\s+transfers?", low, re.I):
                extra_notes.append("Transfer(s) recorded on reverse")
        if extra_notes:
            existing = rec.get("remarks") or ""
            merged = "; ".join(dict.fromkeys(
                [n for n in existing.split("; ") if n] + extra_notes))
            rec["remarks"] = merged
        rec["latest_share_holder_name"] = (
            rec.get("latest_share_holder_name") or rec.get("share_holder_name"))
        rec["latest_folio_no"] = rec.get("latest_folio_no") or rec.get("folio_no")
        rec["folio_no_history"] = rec.get("folio_no_history") or rec.get("folio_no")
        rec["share_holder_history"] = (
            rec.get("share_holder_history") or rec.get("share_holder_name"))
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

    # 6) folio_no_history came from reading a back-page transfer log where
    # several similar-looking reference numbers (Transfer No. / IW. No. /
    # Folio No.) sit side by side. This has been observed to be genuinely
    # non-deterministic - the same certificate can read correctly or
    # incorrectly on different runs - so any row with more than one folio
    # in its history is soft-flagged for a human to confirm by eye rather
    # than trusted outright.
    history = rec.get("folio_no_history") or ""
    holder_history = rec.get("share_holder_history") or ""
    if " -> " in str(history) or " -> " in str(holder_history):
        flags.append("Transfer history read from transfer log - verify by eye "
                     f"(folios: {history}; holders: {holder_history})")
        # folio_no_history and share_holder_history are supposed to be built
        # in lock-step, one entry per transfer row. A count mismatch means a
        # row's folio or name was dropped on one side but not the other.
        n_folio = str(history).count(" -> ") + 1
        n_holder = str(holder_history).count(" -> ") + 1
        if n_folio != n_holder:
            flags.append(
                f"Folio history has {n_folio} entries but holder history has "
                f"{n_holder} - a transfer row's folio or name was dropped")

    return "; ".join(flags)
