#!/usr/bin/env python3
"""Pre-flight check: tells you exactly what is missing before you run the GUI.

    python check_env.py
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys

OK = "  OK  "
WARN = " WARN "
FAIL = " FAIL "


def line(status: str, name: str, detail: str = "") -> None:
    print(f"[{status}] {name:<26} {detail}")


def check_module(name: str, required: bool, hint: str) -> bool:
    try:
        m = importlib.import_module(name)
        ver = getattr(m, "__version__", "")
        line(OK, name, ver)
        return True
    except Exception as e:                            # noqa: BLE001
        line(FAIL if required else WARN, name, f"{e.__class__.__name__} — {hint}")
        return False


def main() -> int:
    print(f"Python {sys.version.split()[0]}  ({sys.executable})\n")
    hard_fail = False

    # --- Tkinter: the actual GUI toolkit --------------------------------
    try:
        import tkinter
        line(OK, "tkinter", f"Tk {tkinter.TkVersion}")
        try:
            root = tkinter.Tk()
            root.withdraw()
            root.destroy()
            line(OK, "display", "a window can be opened")
        except Exception as e:                        # noqa: BLE001
            hard_fail = True
            line(FAIL, "display", f"{e} — no screen/X server. Run on a desktop, "
                                  "or use: python -m share_ocr.cli")
    except Exception:                                 # noqa: BLE001
        hard_fail = True
        line(FAIL, "tkinter", "missing — Ubuntu: sudo apt install python3-tk | "
                              "Mac: brew install python-tk | Windows: reinstall "
                              "Python with 'tcl/tk' ticked")

    # --- libraries -------------------------------------------------------
    if not check_module("PIL", True, "pip install pillow"):
        hard_fail = True
    check_module("openai", False, "pip install openai — only needed for the openai engine")
    have_pymupdf = check_module("fitz", False, "pip install pymupdf — reads PDFs, "
                                                "no extra install needed")
    have_pdf2image = check_module("pdf2image", False, "pip install pdf2image — reads "
                                                       "PDFs, but also needs poppler on PATH")
    check_module("pytesseract", False, "pip install pytesseract — only for the offline engine")
    check_module("tkinterdnd2", False, "pip install tkinterdnd2 — enables drag & drop "
                                       "(click-to-select still works without it)")
    check_module("keyring", False, "pip install keyring — stores the API key in the "
                                   "OS credential store instead of a local file")

    # --- external binaries ----------------------------------------------
    have_poppler = bool(shutil.which("pdftoppm") or shutil.which("pdftocairo"))
    line(OK if have_poppler else WARN, "poppler",
        shutil.which("pdftoppm") or "not found — only needed if you rely on pdf2image "
                                    "instead of pymupdf for PDF input")
    line(OK if shutil.which("tesseract") else WARN, "tesseract",
        shutil.which("tesseract") or "not found — needed only for the offline engine")
    line(OK if (have_pymupdf or (have_pdf2image and have_poppler)) else WARN,
        "PDF input", "ready via " + (
            "pymupdf" if have_pymupdf else "pdf2image + poppler"
        ) if (have_pymupdf or (have_pdf2image and have_poppler))
        else "not available — run 'pip install pymupdf' for the simplest fix")

    # --- API key ----------------------------------------------------------
    try:
        from share_ocr.config import Settings
        from share_ocr.secrets import describe

        info = describe(Settings.load())
        line(OK if info["configured"] else WARN, "OpenAI API key",
             f"{info['masked']} via {info['source_label']}"
             if info["configured"]
             else "not set — add it in the GUI toolbar, run "
                  "`python -m share_ocr.cli key --set`, or use engine "
                  "add it in the app: click the API key chip in the toolbar")
        line(OK if info["keyring"] else WARN, "credential store",
             info["keyring_backend"] if info["keyring"]
             else "unavailable — the key would fall back to an obfuscated "
                  "local file (pip install keyring for OS-level storage)")
    except Exception as e:                                    # noqa: BLE001
        line(WARN, "OpenAI API key", f"could not check ({e})")

    print()
    if hard_fail:
        print("X GUI cannot start yet — fix the FAIL lines above.")
        return 1
    print("OK GUI can start.  Next:  python run_gui.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
