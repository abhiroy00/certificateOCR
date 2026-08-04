"""Where the outgoing SMTP credentials used to email OTP codes come from.

These are the APP'S OWN sender credentials (a Gmail account with an App
Password) - not anything belonging to the operator who is trying to log in.
They are used only to email a one-time code to ``ADMIN_EMAIL`` (see
``share_ocr/otp_auth.py`` for why the code goes there and not to the
operator).

Read in this order, first hit wins:

  1. Environment variables ``SHARE_OCR_SMTP_USER`` / ``SHARE_OCR_SMTP_PASSWORD``
     - handy for running/testing from source.
  2. ``smtp_config.json`` next to this file (source checkout) or bundled into
     the .exe by build_exe.py the same way assets/icon.ico is.

Real credentials are never committed - ``smtp_config.json`` is gitignored.
Copy ``smtp_config.example.json`` to ``smtp_config.json`` and fill in a
Gmail App Password before running build_exe.py. See README.md
"Configuring the OTP sender".
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

# Where every OTP code is sent. Never the operator's own address - see
# otp_auth.py for the reasoning.
ADMIN_EMAIL = "chawlamahinder65@gmail.com"

DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465


def _config_path() -> Optional[Path]:
    """Find smtp_config.json, both running from source and from the .exe.

    PyInstaller unpacks --add-data files into sys._MEIPASS at runtime.
    """
    roots = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        roots.append(Path(meipass))
    here = Path(__file__).resolve().parent
    roots += [here.parent, here]      # project root, then share_ocr/
    for r in roots:
        p = r / "smtp_config.json"
        if p.exists():
            return p
    return None


def load_smtp_credentials() -> Tuple[Optional[str], Optional[str], str, int]:
    """(user, password, host, port). user/password are None if unconfigured."""
    user = os.environ.get("SHARE_OCR_SMTP_USER")
    password = os.environ.get("SHARE_OCR_SMTP_PASSWORD")
    host = os.environ.get("SHARE_OCR_SMTP_HOST", DEFAULT_SMTP_HOST)
    port = int(os.environ.get("SHARE_OCR_SMTP_PORT", str(DEFAULT_SMTP_PORT)))
    if user and password:
        return user.strip(), password.strip(), host, port

    p = _config_path()
    if p is not None:
        try:
            data = json.loads(p.read_text("utf-8"))
            user = data.get("smtp_user") or user
            password = data.get("smtp_password") or password
            host = data.get("smtp_host", host)
            port = int(data.get("smtp_port", port))
        except Exception:                                     # noqa: BLE001
            pass
    return (user.strip() if user else None,
            password.strip() if password else None, host, port)
