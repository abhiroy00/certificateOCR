"""Email + OTP gate shown before the main OCR window.

See share_ocr/otp_auth.py for why the code is emailed to the administrator
rather than to the operator typing the email address.
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from .config import APP_NAME, APP_VERSION
from .otp_auth import (MAX_ATTEMPTS, OTP_TTL_SECONDS, RESEND_COOLDOWN_SECONDS,
                       OtpChallenge, OtpMailError, generate_otp, is_valid_email,
                       send_otp_email)
from .smtp_config import ADMIN_EMAIL
from .theme import BG, MUTED, RED, apply_theme, card
from .widgets import RoundedButton


class LoginGate:
    """Owns the root window until a valid OTP is verified, then hands off.

    Usage: ``LoginGate(root, on_success=launch_app)`` - builds its UI
    straight into ``root`` (no Toplevel), and on success destroys its own
    widgets and calls ``on_success()`` so the caller can build the real app
    in the same window.
    """

    def __init__(self, root: tk.Tk, on_success: Callable[[], None]):
        self.root = root
        self.on_success = on_success
        self.challenge: Optional[OtpChallenge] = None
        self._ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self._resend_ready_at = 0.0
        self._tick_id: Optional[str] = None
        self._closed = False

        self.t = apply_theme(root)
        root.title(f"{APP_NAME} v{APP_VERSION} - Sign in")
        root.configure(bg=BG)
        root.resizable(False, False)
        root.update_idletasks()
        self._center(440, 380)

        self._build()
        self._poll_queue()

    # ------------------------------------------------------------- layout --
    def _center(self, w: int, h: int) -> None:
        px = self.t.px
        w, h = px(w), px(h)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 3)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _build(self) -> None:
        t, px = self.t, self.t.px
        self.panel = card(self.root)
        self.panel.pack(expand=True, padx=px(30), pady=px(30))
        self.body = ttk.Frame(self.panel, style="Card.TFrame",
                              padding=(px(26), px(24)))
        self.body.pack()

        ttk.Label(self.body, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(self.body, text="Enter your email to request access.",
                  style="Muted.TLabel", wraplength=px(320), justify="left"
                  ).pack(anchor="w", pady=(px(4), px(16)))

        ttk.Label(self.body, text="Email", style="Field.TLabel").pack(anchor="w")
        self.email_var = tk.StringVar()
        self.email_entry = ttk.Entry(self.body, textvariable=self.email_var,
                                     width=34, font=t.f(-1))
        self.email_entry.pack(anchor="w", pady=(px(4), px(10)))
        self.email_entry.bind("<Return>", lambda e: self._on_send())
        self.email_entry.focus_set()

        self.send_btn = RoundedButton(self.body, text="Send access code",
                                      variant="primary", theme=t,
                                      command=self._on_send)
        self.send_btn.pack(anchor="w")

        # OTP section, hidden until a code has actually been sent.
        self.otp_frame = ttk.Frame(self.body, style="Card.TFrame")
        ttk.Separator(self.otp_frame).pack(fill="x", pady=(px(16), px(14)))
        ttk.Label(self.otp_frame, text="Access code", style="Field.TLabel"
                  ).pack(anchor="w")
        ttk.Label(self.otp_frame,
                  text="Sent to the administrator - ask them for the code.",
                  style="Muted.TLabel", wraplength=px(320), justify="left"
                  ).pack(anchor="w", pady=(px(2), px(8)))
        self.otp_var = tk.StringVar()
        self.otp_entry = ttk.Entry(self.otp_frame, textvariable=self.otp_var,
                                   width=16, font=t.fm(1))
        self.otp_entry.pack(anchor="w")
        self.otp_entry.bind("<Return>", lambda e: self._on_verify())

        row = ttk.Frame(self.otp_frame, style="Card.TFrame")
        row.pack(anchor="w", pady=(px(10), 0))
        self.verify_btn = RoundedButton(row, text="Verify", variant="primary",
                                        theme=t, command=self._on_verify)
        self.verify_btn.pack(side="left")
        self.resend_btn = RoundedButton(row, text="Resend code",
                                        variant="secondary", theme=t,
                                        command=self._on_send)
        self.resend_btn.pack(side="left", padx=(px(8), 0))

        self.status = ttk.Label(self.body, style="Muted.TLabel",
                                wraplength=px(320), justify="left", text="")
        self.status.pack(anchor="w", pady=(px(12), 0))

    # ------------------------------------------------------------- status --
    def _set_status(self, msg: str, error: bool = False) -> None:
        self.status.configure(text=msg, foreground=(RED if error else MUTED))

    # ------------------------------------------------------------- send ---
    def _on_send(self) -> None:
        email = self.email_var.get().strip()
        if not is_valid_email(email):
            self._set_status("That does not look like a valid email address.",
                             error=True)
            return
        now = time.time()
        if now < self._resend_ready_at:
            wait = int(self._resend_ready_at - now)
            self._set_status(f"Please wait {wait}s before requesting another code.")
            return

        self.send_btn.configure(state="disabled")
        self.resend_btn.configure(state="disabled")
        self.email_entry.configure(state="disabled")
        self._set_status("Sending request…")
        otp = generate_otp()
        self.challenge = OtpChallenge(email=email, code=otp)
        threading.Thread(target=self._send_bg, args=(email, otp),
                         daemon=True, name="otp-send").start()

    def _send_bg(self, email: str, otp: str) -> None:
        try:
            send_otp_email(email, otp)
            self._ui_queue.put(("sent", None))
        except OtpMailError as e:
            self._ui_queue.put(("send_error", str(e)))
        except Exception as e:                            # noqa: BLE001
            self._ui_queue.put(("send_error", str(e)))

    # ------------------------------------------------------------ verify --
    def _on_verify(self) -> None:
        if not self.challenge:
            return
        code = self.otp_var.get().strip()
        if not code:
            self._set_status("Enter the code you were given.", error=True)
            return
        ok, msg = self.challenge.check(code)
        if ok:
            self._set_status("Verified. Opening…")
            self.verify_btn.configure(state="disabled")
            self.otp_entry.configure(state="disabled")
            self.root.after(200, self._finish)
        else:
            self._set_status(msg, error=True)

    def _finish(self) -> None:
        self._closed = True
        if self._tick_id is not None:
            try:
                self.root.after_cancel(self._tick_id)
            except Exception:                             # noqa: BLE001
                pass
            self._tick_id = None
        self.root.resizable(True, True)
        for w in self.root.winfo_children():
            w.destroy()
        self.on_success()

    # -------------------------------------------------------------- poll --
    def _poll_queue(self) -> None:
        if self._closed:
            return
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "sent":
                    self.otp_frame.pack(fill="x")
                    self.otp_entry.focus_set()
                    self._resend_ready_at = time.time() + RESEND_COOLDOWN_SECONDS
                    self._set_status(
                        f"Code sent to the administrator ({ADMIN_EMAIL}). "
                        f"Ask them for it - valid {OTP_TTL_SECONDS // 60} "
                        f"minutes, up to {MAX_ATTEMPTS} tries.")
                elif kind == "send_error":
                    self._set_status(payload, error=True)
                self.send_btn.configure(state="normal")
                self.resend_btn.configure(state="normal")
                self.email_entry.configure(state="normal")
        except queue.Empty:
            pass
        if not self._closed:
            self._tick_id = self.root.after(200, self._poll_queue)
