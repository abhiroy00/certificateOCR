"""Custom Tk widgets that ttk cannot draw.

ttk on Windows renders buttons through the native theme engine, which gives
square corners no matter what style you configure. The only reliable way to
get rounded corners in Tk is to draw them ourselves on a Canvas, so that is
what RoundedButton does: one rounded rectangle plus a centred label, with
hover / pressed / disabled / focus states wired up by hand.

Everything is sized in scaled pixels so the buttons stay crisp at 125%, 150%
and 200% Windows scaling.
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional

from .theme import (BLUE, BLUE_DISABLED, BLUE_HOVER, BLUE_PRESSED, BORDER,
                    CARD, GREEN, GREEN_HOVER, INK_SOFT, MUTED, ON_ACCENT, RED,
                    RED_HOVER, SLATE, SLATE_HOVER, SLATE_PRESSED)

# variant -> (fill, hover, pressed, text, border, disabled_fill, disabled_text)
VARIANTS = {
    "primary": (BLUE, BLUE_HOVER, BLUE_PRESSED, ON_ACCENT, None,
                BLUE_DISABLED, "#eef2fb"),
    "secondary": (SLATE, SLATE_HOVER, SLATE_PRESSED, INK_SOFT, BORDER,
                  "#f2f4f8", "#aab2c0"),
    "success": (GREEN, GREEN_HOVER, GREEN_HOVER, ON_ACCENT, None,
                "#a7d9c8", "#eef7f3"),
    "danger": (RED, RED_HOVER, RED_HOVER, ON_ACCENT, None,
               "#eab3b3", "#fbeeee"),
    "ghost": (CARD, SLATE, SLATE_HOVER, INK_SOFT, BORDER,
              CARD, "#aab2c0"),
}


def _measure(widget: tk.Misc, text: str, font) -> int:
    """Pixel width of `text` in `font`, with a safe fallback."""
    try:
        import tkinter.font as tkfont
        return int(tkfont.Font(root=widget, font=font).measure(text))
    except Exception:                                 # noqa: BLE001
        return int(len(text) * 8)


class RoundedButton(tk.Canvas):
    """A flat, rounded, themed button.

    Supports the small slice of the ttk.Button API the app actually uses:
    ``configure(text=..., state=...)``, ``cget("text")``, ``cget("state")``
    and the usual geometry managers.
    """

    def __init__(self, parent, text: str = "", command: Optional[Callable] = None,
                 variant: str = "secondary", theme=None, bg: str = CARD,
                 min_width: int = 0, state: str = "normal",
                 radius: Optional[int] = None, font=None, pad: int = 0):
        self._theme = theme
        px = theme.px if theme is not None else (lambda v: v)
        self._px = px
        self._font = font or (theme.f(-1, "bold") if theme is not None
                              else ("Segoe UI", 10, "bold"))
        self._variant = variant if variant in VARIANTS else "secondary"
        self._text = text
        self._command = command
        self._state = state
        self._bg = bg
        self._hover = False
        self._pressed = False
        self._focus = False
        self._pad_x = px(18) + pad
        self._pad_y = px(10)
        self._min_width = min_width

        h = px(34)
        w = self._wanted_width(parent)
        super().__init__(parent, width=w, height=h, bg=bg, bd=0,
                         highlightthickness=0, takefocus=1,
                         cursor="hand2" if state != "disabled" else "arrow")
        self._bw, self._bh = w, h
        self._radius = radius if radius is not None else min(px(9), h // 2)

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Return>", self._on_key)
        self.bind("<space>", self._on_key)
        self._draw()

    # ------------------------------------------------------------ size --
    def _wanted_width(self, parent) -> int:
        text_w = _measure(parent, self._text or " ", self._font)
        return max(self._min_width, text_w + self._pad_x * 2)

    # ---------------------------------------------------------- paint --
    def _palette(self):
        fill, hover, pressed, fg, border, dis_fill, dis_fg = VARIANTS[self._variant]
        if self._state == "disabled":
            return dis_fill, dis_fg, border
        if self._pressed:
            return pressed, fg, border
        if self._hover:
            return hover, fg, border
        return fill, fg, border

    def _round_points(self, x1, y1, x2, y2, r):
        return [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]

    def _draw(self) -> None:
        try:
            self.delete("all")
        except Exception:                             # noqa: BLE001
            return
        fill, fg, border = self._palette()
        w, h, r = self._bw, self._bh, self._radius
        outline = border or fill
        self.create_polygon(self._round_points(1, 1, w - 1, h - 1, r),
                            fill=fill, outline=outline, smooth=True,
                            splinesteps=24, width=1)
        if self._focus and self._state != "disabled":
            # subtle keyboard-focus ring, drawn just inside the edge
            self.create_polygon(self._round_points(3, 3, w - 3, h - 3, max(2, r - 2)),
                                fill="", outline=ON_ACCENT if self._variant
                                not in ("secondary", "ghost") else BLUE,
                                smooth=True, splinesteps=24, width=1)
        self.create_text(w // 2, h // 2, text=self._text, fill=fg,
                         font=self._font)

    # ---------------------------------------------------------- events --
    def _on_enter(self, _e=None):
        if self._state == "disabled":
            return
        self._hover = True
        self._draw()

    def _on_leave(self, _e=None):
        self._hover = False
        self._pressed = False
        self._draw()

    def _on_press(self, _e=None):
        if self._state == "disabled":
            return
        self._pressed = True
        self._draw()
        try:
            self.focus_set()
        except Exception:                             # noqa: BLE001
            pass

    def _on_release(self, _e=None):
        if self._state == "disabled":
            return
        was = self._pressed
        self._pressed = False
        self._draw()
        if was and self._command:
            self._command()

    def _on_key(self, _e=None):
        if self._state != "disabled" and self._command:
            self._command()
        return "break"

    def _on_focus_in(self, _e=None):
        self._focus = True
        self._draw()

    def _on_focus_out(self, _e=None):
        self._focus = False
        self._draw()

    # ------------------------------------------------- ttk-ish API ----
    def invoke(self):
        """Call the command, the way ttk.Button.invoke() does (used by tests)."""
        if self._state != "disabled" and self._command:
            return self._command()
        return None

    def configure(self, **kw):                        # type: ignore[override]
        redraw = False
        if "text" in kw:
            self._text = kw.pop("text")
            self._bw = self._wanted_width(self)
            try:
                super().configure(width=self._bw)
            except Exception:                         # noqa: BLE001
                pass
            redraw = True
        if "state" in kw:
            self._state = kw.pop("state")
            self._hover = False
            self._pressed = False
            try:
                super().configure(
                    cursor="arrow" if self._state == "disabled" else "hand2")
            except Exception:                         # noqa: BLE001
                pass
            redraw = True
        if "command" in kw:
            self._command = kw.pop("command")
        if "variant" in kw:
            v = kw.pop("variant")
            self._variant = v if v in VARIANTS else self._variant
            redraw = True
        if kw:
            super().configure(**kw)
        if redraw:
            self._draw()
        return None

    config = configure

    def cget(self, key):                              # type: ignore[override]
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        if key == "variant":
            return self._variant
        return super().cget(key)

    def __getitem__(self, key):
        return self.cget(key)


def rbutton(parent, text, command=None, variant="secondary", theme=None,
            bg=CARD, **kw) -> RoundedButton:
    """Convenience factory so call sites stay short."""
    return RoundedButton(parent, text=text, command=command, variant=variant,
                         theme=theme, bg=bg, **kw)
