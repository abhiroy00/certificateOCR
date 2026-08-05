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
* The OpenAI key is NOT baked into the exe. The user pastes it into the app
  once and it goes to the Windows Credential Manager.
* smtp_config.json (if present) IS baked into the exe - it holds the Gmail
  App Password the app uses to email OTP access codes to the administrator.
  Copy smtp_config.example.json to smtp_config.json and fill it in before
  running this script; see README.md "Configuring the OTP sender". Without
  it the exe still builds, but the sign-in screen will tell every operator
  "no email sender configured" instead of sending a code.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ICON = ROOT / "assets" / "icon.ico"
SMTP_CONFIG = ROOT / "smtp_config.json"
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
        "--hidden-import", "PIL._tkinter_finder",
    ]
    # pytesseract does `try: import pandas` / `try: import numpy` purely to
    # offer an optional DataFrame return type this app never asks for (see
    # extractor.py - only image_to_string()/get_tesseract_version() are
    # used). If those packages merely happen to be installed on the machine
    # building the exe (e.g. from an unrelated project in the same global
    # Python), PyInstaller's static analysis has no way to know they are
    # optional and bundles them anyway - pulling in numpy, pandas, and
    # pandas' own optional backends (numba/llvmlite, sqlalchemy, psycopg2,
    # opentelemetry) despite this app going out of its way NOT to depend on
    # any of them (see the "no pandas" design notes in csv_writer.py/db.py).
    # Result: a much bigger exe, and native code compiled for the BUILD
    # machine's CPU shipped to a client machine that may not support the
    # same instruction set - a classic silent "has stopped working" crash
    # with no Python traceback. None of these are ever imported by this
    # app's own code, so excluding them is safe.
    for mod in ("numpy", "pandas", "numba", "llvmlite", "sqlalchemy",
                "psycopg2", "opentelemetry", "openpyxl"):
        cmd += ["--exclude-module", mod]
    if ICON.exists():
        cmd += ["--icon", str(ICON),
                "--add-data", "%s%s%s" % (ICON, ";" if sys.platform == "win32" else ":", "assets")]
    if SMTP_CONFIG.exists():
        cmd += ["--add-data", "%s%s%s" % (SMTP_CONFIG, ";" if sys.platform == "win32" else ":", ".")]
    else:
        print("WARNING: smtp_config.json not found - the built exe will not "
              "be able to send OTP codes. See README.md "
              "'Configuring the OTP sender'.")
    cmd.append(str(ROOT / "run_gui.py"))

    print(" ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc == 0:
        print("\nBuilt:  %s" % (ROOT / "dist" / (NAME + ".exe")))
        print("Ship that single file. The customer needs no Python.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
