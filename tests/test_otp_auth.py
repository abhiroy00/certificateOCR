"""Pure-logic tests for the email + OTP access gate.

    python -m tests.test_otp_auth
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from share_ocr import otp_auth as O                    # noqa: E402
from share_ocr import smtp_config as SC                 # noqa: E402

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s   %s" % (name, detail))


def main() -> int:
    print("\n[1] email format validation")
    check("accepts a normal address", O.is_valid_email("ops@client.com"))
    check("accepts dots/plus tags", O.is_valid_email("first.last+tag@sub.client.co.in"))
    check("rejects missing @", not O.is_valid_email("opsclient.com"))
    check("rejects missing domain dot", not O.is_valid_email("ops@client"))
    check("rejects empty string", not O.is_valid_email(""))
    check("rejects whitespace only", not O.is_valid_email("   "))
    check("rejects a value with spaces", not O.is_valid_email("ops person@client.com"))
    check("trims surrounding whitespace before checking",
          O.is_valid_email("  ops@client.com  "))

    print("\n[2] OTP generation")
    otp = O.generate_otp()
    check("default length is 6 digits", len(otp) == O.OTP_LENGTH, otp)
    check("only digits", otp.isdigit(), otp)
    others = {O.generate_otp() for _ in range(20)}
    check("not obviously constant across calls", len(others) > 1, others)

    print("\n[3] OtpChallenge.check")
    ch = O.OtpChallenge(email="ops@client.com", code="123456")
    ok, msg = ch.check("000000")
    check("wrong code fails", not ok, msg)
    check("attempts counter increments", ch.attempts == 1, ch.attempts)
    ok, msg = ch.check("123456")
    check("right code succeeds", ok, msg)

    ch2 = O.OtpChallenge(email="ops@client.com", code="654321")
    for _ in range(O.MAX_ATTEMPTS):
        ch2.check("000000")
    ok, msg = ch2.check("654321")
    check("locked out after MAX_ATTEMPTS wrong tries even with the right code",
          not ok, msg)

    ch3 = O.OtpChallenge(email="ops@client.com", code="111111",
                         created_at=time.time() - O.OTP_TTL_SECONDS - 5)
    check("an old challenge reports expired", ch3.expired())
    ok, msg = ch3.check("111111")
    check("expired challenge rejects even the right code", not ok, msg)

    print("\n[4] SMTP credential loading")
    check("admin address is the fixed inbox",
          SC.ADMIN_EMAIL == "chawlamahinder65@gmail.com", SC.ADMIN_EMAIL)

    print("\n[5] send_otp_email fails loudly with no credentials configured")
    import os
    for k in ("SHARE_OCR_SMTP_USER", "SHARE_OCR_SMTP_PASSWORD"):
        os.environ.pop(k, None)
    real_load = SC.load_smtp_credentials
    SC.load_smtp_credentials = lambda: (None, None, SC.DEFAULT_SMTP_HOST,
                                        SC.DEFAULT_SMTP_PORT)
    O.load_smtp_credentials = SC.load_smtp_credentials
    try:
        raised = False
        try:
            O.send_otp_email("ops@client.com", "123456")
        except O.OtpMailError:
            raised = True
        check("raises OtpMailError instead of crashing", raised)
    finally:
        SC.load_smtp_credentials = real_load
        O.load_smtp_credentials = real_load

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
