"""Build a standalone Windows .exe (no Python needed on the client's PC).

    pip install pyinstaller
    python build_exe.py

Result:  dist/ShareCertificateOCR.exe   (single file, double-clickable)

Notes
-----
* Run this ON Windows to get a Windows .exe. PyInstaller does not
  cross-compile.
* The icon comes from assets/icon.ico. Drop the client's artwork in there
  (256x256 .ico) and rebuild - nothing else to change.
* Tesseract is a separate native program. If the customer wants the offline
  engine, they still install Tesseract themselves; the app finds it
  automatically in the default folder.
* PDF input needs nothing extra: pypdfium2 ships its renderer inside the exe.
  Poppler is only used as a fallback if it happens to be installed.
* The OpenAI key is NOT baked into the exe. The user pastes it into the app
  once and it goes to the Windows Credential Manager.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ICON = ROOT / "assets" / "icon.ico"
NAME = "ShareCertificateOCR"


def main() -> int:
    if shutil.which("pyinstaller") is None:
        print("PyInstaller is not installed. Run:  pip install pyinstaller")
        return 1

    cmd = [
        "pyinstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",                 # no console window behind the GUI
        "--name", NAME,
        "--collect-all", "tkinterdnd2",
        # Bundles the PDFium binary, so PDFs work on a customer PC that has
        # never heard of poppler.
        "--collect-all", "pypdfium2",
        "--hidden-import", "PIL._tkinter_finder",
    ]
    if ICON.exists():
        cmd += ["--icon", str(ICON),
                "--add-data", "%s%s%s" % (ICON, ";" if sys.platform == "win32" else ":", "assets")]
    cmd.append(str(ROOT / "run_gui.py"))

    print(" ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc == 0:
        print("\nBuilt:  %s" % (ROOT / "dist" / (NAME + ".exe")))
        print("Ship that single file. The customer needs no Python.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
