"""Email + OTP access gate for the desktop app.

Every time the app launches, the operator must type an email address and get
a one-time code before the OCR tool becomes usable. By design the code is
emailed to the ADMINISTRATOR's inbox (``smtp_config.ADMIN_EMAIL``), never to
the operator - the administrator reads it and relays it out of band (phone
call, WhatsApp, ...). That turns "anyone with a copy of the .exe" into
"anyone the administrator has actually agreed to let in today", without
needing a backend server to run this whole thing through.
"""
from __future__ import annotations

import getpass
import platform
import re
import secrets
import smtplib
import time
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from typing import Tuple

from .smtp_config import ADMIN_EMAIL, load_smtp_credentials

# Deliberately simple RFC-5322-ish check: this only needs to catch typos
# before bothering the administrator, not to be a full validator.
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")

OTP_LENGTH = 6
OTP_TTL_SECONDS = 10 * 60
MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 45


def is_valid_email(email: str) -> bool:
    email = (email or "").strip()
    return bool(email) and len(email) <= 254 and bool(EMAIL_RE.match(email))


def generate_otp(length: int = OTP_LENGTH) -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


@dataclass
class OtpChallenge:
    """One outstanding code for one email, held only in memory."""
    email: str
    code: str
    created_at: float = field(default_factory=time.time)
    attempts: int = 0

    def expired(self) -> bool:
        return (time.time() - self.created_at) > OTP_TTL_SECONDS

    def seconds_left(self) -> int:
        return max(0, int(OTP_TTL_SECONDS - (time.time() - self.created_at)))

    def check(self, candidate: str) -> Tuple[bool, str]:
        if self.expired():
            return False, "This code has expired. Request a new one."
        if self.attempts >= MAX_ATTEMPTS:
            return False, "Too many wrong attempts. Request a new code."
        self.attempts += 1
        if secrets.compare_digest((candidate or "").strip(), self.code):
            return True, "OK"
        left = MAX_ATTEMPTS - self.attempts
        if left <= 0:
            return False, "Too many wrong attempts. Request a new code."
        return False, f"Wrong code. {left} attempt(s) left."


class OtpMailError(RuntimeError):
    """Message is safe to show directly in the UI."""


def send_otp_email(user_email: str, otp: str, timeout: int = 20) -> None:
    """Email the OTP to the administrator. Raises OtpMailError on failure."""
    user, password, host, port = load_smtp_credentials()
    if not user or not password:
        raise OtpMailError(
            "This copy of the app has no email sender configured. "
            "Contact the administrator.")

    body = (
        "Access requested for Share Certificate OCR.\n\n"
        f"Operator email entered : {user_email}\n"
        f"One-time code          : {otp}\n"
        f"Expires in             : {OTP_TTL_SECONDS // 60} minutes\n"
        f"Machine                : {platform.node()} / {getpass.getuser()}\n"
        f"Requested at           : {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "Relay this code to the operator to let them in."
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"Share OCR access code for {user_email}"
    msg["From"] = user
    msg["To"] = ADMIN_EMAIL

    try:
        with smtplib.SMTP_SSL(host, port, timeout=timeout) as server:
            server.login(user, password)
            server.sendmail(user, [ADMIN_EMAIL], msg.as_string())
    except smtplib.SMTPAuthenticationError as e:
        raise OtpMailError(
            "Could not sign in to send the code. The administrator's "
            "email credentials may have expired.") from e
    except OSError as e:
        raise OtpMailError(
            f"Could not reach the mail server - check the internet "
            f"connection. ({e})") from e
    except Exception as e:                                    # noqa: BLE001
        raise OtpMailError(f"Could not send the code: {e}") from e
