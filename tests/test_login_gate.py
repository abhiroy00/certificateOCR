"""Headless tests for the email + OTP sign-in gate.

Run with:  python -m tests.test_login_gate

Exercises share_ocr.login_gui.LoginGate against the fake Tk in
tests/fake_tk.py, with share_ocr.login_gui.send_otp_email monkeypatched so
no real network call happens.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import fake_tk                            # noqa: E402

fake_tk.install()                                     # BEFORE importing the GUI

import tkinter as tk                                  # noqa: E402  (the fake)

from share_ocr import login_gui as L                   # noqa: E402
from share_ocr.otp_auth import OtpMailError            # noqa: E402

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
    print("\n[1] invalid email is rejected before anything is sent")
    root = tk.Tk()
    sent = []
    real_send = L.send_otp_email
    L.send_otp_email = lambda *a, **k: sent.append(a)
    try:
        done = []
        gate = L.LoginGate(root, on_success=lambda: done.append(1))
        gate.email_var.set("not-an-email")
        gate._on_send()
        check("no thread was started for a bad address", not sent)
        check("status explains the problem",
              "valid email" in gate.status.cget("text"), gate.status.cget("text"))
        check("OTP field stays hidden", not gate.otp_frame.packed)

        print("\n[2] a valid email sends and reveals the OTP field")
        gate.email_var.set("ops@client.com")
        gate._on_send()
        check("send button disabled while sending",
              gate.send_btn.cget("state") == "disabled")
        # the send happened synchronously (stubbed), just off-thread; drain
        # the queue the way the real polling loop would.
        for _ in range(100):
            if not gate._ui_queue.empty():
                break
            time.sleep(0.01)
        gate._poll_queue()
        check("exactly one send was attempted", len(sent) == 1, sent)
        check("OTP field appears after a successful send", gate.otp_frame.packed)
        check("send button re-enabled", gate.send_btn.cget("state") == "normal")
        check("status names the admin inbox",
              "chawla.mahinder@gmail.com" in gate.status.cget("text"),
              gate.status.cget("text"))

        print("\n[3] wrong code is rejected, right code finishes the gate")
        check("a challenge now exists", gate.challenge is not None)
        real_code = gate.challenge.code
        wrong = "000000" if real_code != "000000" else "111111"
        gate.otp_var.set(wrong)
        gate._on_verify()
        check("wrong code shows an error",
              "Wrong code" in gate.status.cget("text"), gate.status.cget("text"))
        check("on_success not called yet", not done)

        gate.otp_var.set(real_code)
        gate._on_verify()
        check("verify button disabled once correct", gate.verify_btn.cget("state") == "disabled")
        root.run_pending()
        check("on_success fires exactly once", done == [1], done)
        check("the gate's own widgets are gone",
              root.winfo_children() == [], root.winfo_children())

        print("\n[4] resend cooldown blocks an immediate second request")
        root2 = tk.Tk()
        sent.clear()
        gate2 = L.LoginGate(root2, on_success=lambda: None)
        gate2.email_var.set("ops2@client.com")
        gate2._on_send()
        for _ in range(100):
            if not gate2._ui_queue.empty():
                break
            time.sleep(0.01)
        gate2._poll_queue()
        check("first send went through", len(sent) == 1, sent)
        gate2._on_send()          # immediate resend, before cooldown elapses
        check("cooldown blocks the resend", len(sent) == 1, sent)
        check("status mentions waiting",
              "wait" in gate2.status.cget("text").lower(), gate2.status.cget("text"))

        print("\n[5] a mail error is surfaced, not swallowed")
        root3 = tk.Tk()

        def _boom(*a, **k):
            raise OtpMailError("no email sender configured")
        L.send_otp_email = _boom
        gate3 = L.LoginGate(root3, on_success=lambda: None)
        gate3.email_var.set("ops3@client.com")
        gate3._on_send()
        for _ in range(100):
            if not gate3._ui_queue.empty():
                break
            time.sleep(0.01)
        gate3._poll_queue()
        check("error message reaches the status label",
              "no email sender configured" in gate3.status.cget("text"),
              gate3.status.cget("text"))
        check("OTP field stays hidden after a failed send", not gate3.otp_frame.packed)

        print("\n%d passed, %d failed" % (PASS, FAIL))
        return 1 if FAIL else 0
    finally:
        L.send_otp_email = real_send


if __name__ == "__main__":
    raise SystemExit(main())
