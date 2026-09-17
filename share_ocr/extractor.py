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
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .config import ADDON_FIELDS, FIELDS, PROMPT, PROMPT_MULTI_PAGE, Settings
from .secrets import list_api_keys


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


@dataclass(frozen=True)
class ProviderKey:
    """One API key, tagged with which provider it belongs to and which
    model/endpoint a request on it should use. NVIDIA's NIM endpoint speaks
    the same OpenAI-compatible chat.completions API, just at a different
    base_url with different model names, so a ProviderKey is all
    OpenAIEngine needs to treat any key - OpenAI or NVIDIA - the same way."""
    provider: str
    key: str
    base_url: str      # "" = the openai SDK's own default (api.openai.com)
    model: str

    @property
    def identity(self) -> tuple:
        """What cooldown bookkeeping keys on - provider+key, not model/url,
        so the identity of one physical key never changes even if settings
        (e.g. which NVIDIA model to use) do."""
        return (self.provider, self.key)


class KeyPool:
    """Round-robins requests across every configured API key, from BOTH
    providers (OpenAI and NVIDIA) at once.

    The GUI's "API keys" dialog lets an operator add as many keys as they
    want, for either provider (mixing both is the point - NVIDIA keys are
    typically much cheaper per image, so a pool of "some OpenAI, some
    NVIDIA" lowers the average cost of a bulk run without a second manual
    pass over the ones that "should" have gone to the cheaper provider).
    Spreading requests across all of them and moving a request to the next
    key the moment one is rate-limited or out of quota is what lets a bulk
    run avoid ever needing a manual "retry failed" step - the pool self-heals
    instead of the file dead-lettering.

    One file is still only ever sent to ONE key on any given attempt -
    OpenAIEngine._complete() returns as soon as any key succeeds - so mixing
    providers here never means a document gets extracted twice; it only
    changes which single provider ends up doing the work.

    Shared across every worker thread (see `_pool_for`), because the whole
    point is a global rotation: if each thread kept its own round-robin
    pointer and cooldown state, two threads could hammer the same key at
    once while another sat idle.
    """

    def __init__(self, entries: List[ProviderKey]):
        self.entries = list(entries)
        self._lock = threading.Lock()
        self._idx = 0
        self._cooldown_until: Dict[tuple, float] = {}

    def __bool__(self) -> bool:
        return bool(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def cooldown(self, identity: tuple, seconds: float) -> None:
        with self._lock:
            self._cooldown_until[identity] = time.time() + seconds

    def order(self) -> List[ProviderKey]:
        """Keys to try for one request, starting from the next round-robin
        slot, healthy keys before ones still cooling down from a recent
        rate-limit/quota error. OpenAI and NVIDIA entries are interleaved in
        whatever order they were added, rotating together - there is no
        separate "prefer this provider" step, so cost savings come purely
        from however many of each kind of key are actually in the pool.

        Side-effecting: every call advances the round-robin pointer, so this
        is meant to be called exactly once per request (as _complete() does).
        Calling it an extra time - e.g. to log or inspect the order - skews
        which key the next real request starts from."""
        with self._lock:
            if not self.entries:
                return []
            n = len(self.entries)
            seq = [self.entries[(self._idx + i) % n] for i in range(n)]
            self._idx = (self._idx + 1) % n
            now = time.time()
            healthy = [e for e in seq
                      if self._cooldown_until.get(e.identity, 0) <= now]
            cooling = [e for e in seq if e not in healthy]
        return healthy + cooling


_pool_cache: Dict[tuple, KeyPool] = {}
_pool_cache_lock = threading.Lock()


def _pool_entries(settings: Settings) -> List[ProviderKey]:
    entries = [ProviderKey("openai", k, settings.base_url, settings.model)
              for k in list_api_keys(settings, "openai")]
    entries += [ProviderKey("nvidia", k, settings.nvidia_base_url,
                            settings.nvidia_model)
               for k in list_api_keys(settings, "nvidia")]
    return entries


def _pool_for(settings: Settings) -> KeyPool:
    """One KeyPool per distinct combination of keys/models/base_urls from
    BOTH providers, shared across threads."""
    entries = _pool_entries(settings)
    cache_key = tuple((e.provider, e.key, e.base_url, e.model) for e in entries)
    with _pool_cache_lock:
        pool = _pool_cache.get(cache_key)
        if pool is None:
            pool = KeyPool(entries)
            _pool_cache[cache_key] = pool
        return pool


# OpenAI reports both of these as HTTP 429, but they are not the same kind
# of problem. A per-minute rate limit ("tokens"/"requests" rate_limit_exceeded)
# is self-resolving in seconds. "insufficient_quota" (credit_balance_exhausted,
# or a monthly quota used up) means the account has no money behind it right
# now and will keep failing every single time, forever, until billing is
# fixed - cooling that key down for only 30s (see RATE_LIMIT_COOLDOWN_S) made
# it look "healthy" again almost immediately, so the pool kept re-trying a
# permanently broken key, burning through a file's max_attempts on nothing
# but repeats of the same billing error instead of ever getting a real shot.
_QUOTA_EXHAUSTED_MARKERS = ("insufficient_quota", "credit_balance_exhausted",
                           "no credits remaining", "exceeded your current quota")


def is_quota_exhausted(msg: str) -> bool:
    """True for a billing/credits problem, as opposed to a transient
    per-minute rate limit - see the note on _QUOTA_EXHAUSTED_MARKERS."""
    m = (msg or "").lower()
    return any(s in m for s in _QUOTA_EXHAUSTED_MARKERS)


# response_format={"type": "json_object"} reliably constrains OpenAI's
# models, but was confirmed live NOT to be reliably honoured by NVIDIA's
# smaller vision models: meta/llama-3.2-11b-vision-instruct read a
# certificate correctly but answered in prose bullet points instead of JSON
# until these were added. Applied to every request, OpenAI included -  it is
# a harmless no-op there and cheap insurance if NVIDIA's catalog changes
# again to some other model with the same quirk.
_JSON_ONLY_SYSTEM = ("You output ONLY a single valid JSON object - no "
                    "explanation, no markdown formatting, no text before or "
                    "after it.")
_JSON_ONLY_SUFFIX = ("\n\nCRITICAL: Respond with ONLY the raw JSON object "
                     "requested above - no explanation, no markdown code "
                     "fences, no bullet points, nothing else. Your entire "
                     "response must start with { and end with }.")
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_response(content: Optional[str]) -> dict:
    """A direct parse covers every model that honours response_format, which
    is the common, correct case. The fallback - pulling out the largest
    {...} block - recovers a model's answer even when it wraps the JSON in
    prose or markdown despite being told not to (see _JSON_ONLY_SUFFIX)."""
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        if content:
            m = _JSON_BLOCK_RE.search(content)
            if m:
                return json.loads(m.group(0))
        raise


class OpenAIEngine(BaseEngine):
    name = "openai"

    # How long a key sits out after hitting a rate limit / quota error
    # before being tried again. Plain per-minute rate limits reset in
    # seconds; an out-of-credits key or a bad/revoked key will not fix
    # itself that fast, so both get parked for much longer instead of being
    # retried every 30s for no benefit.
    RATE_LIMIT_COOLDOWN_S = 30.0
    QUOTA_EXHAUSTED_COOLDOWN_S = 3600.0
    AUTH_ERROR_COOLDOWN_S = 3600.0
    # A request that timed out (request_timeout, currently 90s) already cost
    # a full timeout's worth of wall-clock time on this one call. Without a
    # cooldown here, a key having a slow moment (observed in practice on
    # NVIDIA's free/hosted vision models, whose latency varies far more than
    # OpenAI's) got retried at full priority on every single following file,
    # each paying the same 90s before falling through - a handful of slow
    # keys could make a whole bulk run crawl. Parking it for a bit lets
    # healthy keys carry the load while this one gets a chance to recover.
    TIMEOUT_COOLDOWN_S = 45.0

    def __init__(self, settings: Settings):
        super().__init__(settings)
        from openai import OpenAI

        self._OpenAI = OpenAI
        self.pool = _pool_for(settings)
        if not self.pool:
            raise RuntimeError(
                "No API key configured for either provider. Add at least "
                "one OpenAI or NVIDIA key in the GUI (the API keys button), "
                "or export "
                f"{settings.api_key_env} / OPENAI_API_KEYS / NVIDIA_API_KEYS.")
        self._client_kwargs = {"timeout": settings.request_timeout,
                               "max_retries": 0}  # we do our own backoff
        self._clients: Dict[tuple, object] = {}
        self._clients_lock = threading.Lock()

    def _client_for(self, pk: ProviderKey):
        ident = pk.identity
        client = self._clients.get(ident)
        if client is None:
            with self._clients_lock:
                client = self._clients.get(ident)
                if client is None:
                    kwargs = dict(self._client_kwargs, api_key=pk.key)
                    if pk.base_url:
                        kwargs["base_url"] = pk.base_url
                    client = self._OpenAI(**kwargs)
                    self._clients[ident] = client
        return client

    def _complete(self, messages: List[Dict]) -> Dict:
        """Call chat.completions, trying every key in the pool (healthiest
        first, OpenAI and NVIDIA keys mixed together) before giving up. Only
        ONE of them ever actually answers - the loop returns on the first
        success - so a file is never billed or extracted twice no matter how
        many keys are configured.

        A rate-limit/quota error on one key just moves to the next. A
        non-key-related error (a malformed request, a model that rejects one
        of the parameters used here) is assumed to be a property of that
        PROVIDER's model/endpoint, not the individual key - retrying it on
        every other key of the SAME provider would just repeat it, so those
        are skipped, but a different provider is a genuinely different
        system and still gets tried. Only raises once nothing in the pool
        was able to answer."""
        order = self.pool.order()
        last_exc: Optional[Exception] = None
        skip_providers: set = set()
        for pk in order:
            if pk.provider in skip_providers:
                continue
            try:
                resp = self._client_for(pk).chat.completions.create(
                    model=pk.model,
                    temperature=0,
                    response_format={"type": "json_object"},
                    messages=messages,
                )
                data = _parse_json_response(resp.choices[0].message.content)
                return {k: data.get(k) for k in FIELDS}
            except Exception as e:                          # noqa: BLE001
                last_exc = e
                msg = str(e).lower()
                # Checked before the generic rate-limit case below: OpenAI
                # reports insufficient_quota as HTTP 429 too, so this must
                # be matched first or it is misread as a transient limit.
                if is_quota_exhausted(msg):
                    self.pool.cooldown(pk.identity, self.QUOTA_EXHAUSTED_COOLDOWN_S)
                    continue
                if any(s in msg for s in ("rate limit", "429", "overloaded",
                                          "503", "502")):
                    self.pool.cooldown(pk.identity, self.RATE_LIMIT_COOLDOWN_S)
                    continue
                if any(s in msg for s in ("401", "invalid_api_key",
                                          "incorrect api key", "account_deactivated")):
                    self.pool.cooldown(pk.identity, self.AUTH_ERROR_COOLDOWN_S)
                    continue
                if any(s in msg for s in ("timeout", "timed out", "connection")):
                    # Transient (network blip, or this key/endpoint having a
                    # slow moment) - still worth trying again later, unlike
                    # the "not key-related, this model will always reject
                    # this request" case below.
                    self.pool.cooldown(pk.identity, self.TIMEOUT_COOLDOWN_S)
                    continue
                skip_providers.add(pk.provider)
                continue
        raise last_exc or RuntimeError("No API key available")

    def extract_image(self, image_path: str) -> Dict:
        b64 = downscale_to_jpeg_b64(image_path, self.s.max_image_px, self.s.jpeg_quality)
        return self._complete([
            {"role": "system", "content": _JSON_ONLY_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": PROMPT + _JSON_ONLY_SUFFIX},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}",
                               "detail": "high"}},
            ]},
        ])

    def extract_document(self, image_paths: List[str]) -> Dict:
        """Read every page of one certificate in a single request, so a
        reverse-side "Memorandum of Transfers" page is read as part of the
        same certificate instead of being force-fit into its own row."""
        if len(image_paths) == 1:
            return self.extract_image(image_paths[0])
        content: List[Dict] = [{"type": "text", "text": PROMPT_MULTI_PAGE + _JSON_ONLY_SUFFIX}]
        for p in image_paths:
            b64 = downscale_to_jpeg_b64(p, self.MULTI_PAGE_MAX_PX, self.s.jpeg_quality)
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}",
                                          "detail": "high"}})
        return self._complete([
            {"role": "system", "content": _JSON_ONLY_SYSTEM},
            {"role": "user", "content": content},
        ])


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
