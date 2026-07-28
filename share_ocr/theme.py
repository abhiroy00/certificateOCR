"""Display quality + visual theme for the Tkinter UI.

Two separate problems are solved here.

1. BLURRY TEXT.  On Windows, a process that is not DPI-aware gets its window
   bitmap-stretched by the OS.  On a 125%/150%/175% display that means every
   glyph is scaled up from a 96-DPI raster - which is exactly the soft, fuzzy,
   slightly-too-fat text people see.  The fix has to happen BEFORE the first
   Tk() call, so `enable_hidpi()` is called from run_gui.py, not from App().
   After the window exists, `tk scaling` is set so point sizes map to real
   pixels instead of Tk's hard-coded 72 dpi assumption.

2. UGLY COLOURS.  clam's defaults are washed-out grey. The palette below is a
   single flat design system: one blue for primary actions, neutrals with
   actual contrast (the old muted grey #6b7280 on white is only 4.8:1 at 9pt,
   which reads as "blurry" even on a sharp screen), and tints used for table
   row states instead of saturated fills.
"""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# --------------------------------------------------------------- palette --

# Brand / primary
BLUE = "#2563eb"
BLUE_HOVER = "#1d4ed8"
BLUE_PRESSED = "#1e40af"
BLUE_DISABLED = "#a8c0f5"
BLUE_TINT = "#eff4ff"        # badge + add-on row background
BLUE_EDGE = "#c7d7fb"

# Secondary (neutral outline buttons - most buttons should be these, not green)
SLATE = "#e9edf4"
SLATE_HOVER = "#dde3ee"
SLATE_PRESSED = "#cfd7e6"

# Semantic
GREEN = "#059669"
GREEN_HOVER = "#047857"
RED = "#dc2626"
RED_HOVER = "#b91c1c"
AMBER_TINT = "#fff8e6"       # flagged row background
AMBER_EDGE = "#f0d9a0"

# Neutrals
BG = "#f4f6fa"               # app canvas
CARD = "#ffffff"
BORDER = "#e3e8f0"
ZEBRA = "#fafbfd"            # odd row
INK = "#111827"              # primary text
INK_SOFT = "#374151"         # secondary text
MUTED = "#5b6472"            # >=7:1 on white, unlike the old #6b7280
ON_ACCENT = "#ffffff"


# ------------------------------------------------------------ hidpi fix --

def enable_hidpi() -> float:
    """Make the process DPI-aware. MUST run before the first Tk() call.

    Returns the detected scale factor (1.0 when unknown / not needed).
    Safe to call on any OS; it is a no-op off Windows.
    """
    if not sys.platform.startswith("win"):
        # macOS Tk is retina-aware already. Linux/X11 honours Xft.dpi, which
        # _tk_scaling() below picks up from winfo_fpixels.
        return 1.0
    try:
        import ctypes

        # 2 = PROCESS_PER_MONITOR_DPI_AWARE (Win 8.1+). Best result: the window
        # is re-rendered, not stretched, when dragged between monitors.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:                                  # noqa: BLE001
            # Vista..Win8 fallback: system-wide awareness.
            ctypes.windll.user32.SetProcessDPIAware()

        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)    # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return (dpi or 96) / 96.0
    except Exception:                                      # noqa: BLE001
        return 1.0


def _tk_scaling(root: tk.Misc) -> float:
    """Points-per-pixel for this display, clamped to sane values."""
    try:
        # winfo_fpixels('1i') = pixels per inch as Tk sees it.
        dpi = float(root.winfo_fpixels("1i"))
    except Exception:                                      # noqa: BLE001
        dpi = 96.0
    if dpi < 72 or dpi > 400:
        dpi = 96.0
    return dpi / 72.0


# -------------------------------------------------------------- fonts ----

# First family that actually exists wins. Hard-coding "Segoe UI" was the second
# blur source: off Windows it silently falls back to a bitmap Helvetica that
# has no hinting at non-integer scales.
_UI_STACK = [
    "Segoe UI Variable Text", "Segoe UI",          # Windows
    "SF Pro Text", "Helvetica Neue",                # macOS
    "Inter", "Ubuntu", "Cantarell", "Noto Sans",    # Linux
    "DejaVu Sans", "TkDefaultFont",
]
_MONO_STACK = [
    "Cascadia Mono", "Consolas", "SF Mono", "Menlo",
    "JetBrains Mono", "DejaVu Sans Mono", "TkFixedFont",
]


def _first_available(root: tk.Misc, stack) -> str:
    try:
        have = {f.lower() for f in tkfont.families(root)}
    except Exception:                                      # noqa: BLE001
        return stack[-1]
    for name in stack:
        if name.lower() in have:
            return name
    return stack[-1]


class Theme:
    """Resolved fonts + metrics for one root window."""

    def __init__(self, root: tk.Misc):
        self.root = root
        self.scale = _tk_scaling(root)
        self.ui = _first_available(root, _UI_STACK)
        self.mono = _first_available(root, _MONO_STACK)
        # Base size in points. Tk scales points by `tk scaling`, so we keep the
        # numbers small and let the scaling factor do the work - that is what
        # produces crisp glyphs instead of an upscaled bitmap.
        self.base = 10

    def f(self, delta: int = 0, weight: str = "normal"):
        return (self.ui, self.base + delta, weight)

    def fm(self, delta: int = 0):
        return (self.mono, self.base + delta)

    def px(self, n: int) -> int:
        """Scale a pixel measurement (paddings, row heights, thumbnails)."""
        return max(1, int(round(n * self.scale)))


def apply_theme(root: tk.Misc) -> Theme:
    """Install ttk styles + crisp fonts. Returns the resolved Theme."""
    t = Theme(root)

    try:
        root.tk.call("tk", "scaling", t.scale)
    except Exception:                                      # noqa: BLE001
        pass

    # Re-point Tk's named fonts so menus, dialogs, comboboxes and messageboxes
    # inherit the same family instead of the 1990s default.
    for name, delta, weight in (
        ("TkDefaultFont", 0, "normal"),
        ("TkTextFont", 0, "normal"),
        ("TkMenuFont", 0, "normal"),
        ("TkHeadingFont", 0, "bold"),
        ("TkTooltipFont", -1, "normal"),
        ("TkIconFont", 0, "normal"),
        ("TkSmallCaptionFont", -1, "normal"),
    ):
        try:
            nf = tkfont.nametofont(name, root=root)
            nf.configure(family=t.ui, size=t.base + delta, weight=weight)
        except Exception:                                  # noqa: BLE001
            pass
    try:
        tkfont.nametofont("TkFixedFont", root=root).configure(
            family=t.mono, size=t.base)
    except Exception:                                      # noqa: BLE001
        pass

    st = ttk.Style(root)
    try:
        st.theme_use("clam")        # only theme that honours custom colours
    except tk.TclError:
        pass

    pad = t.px

    # -- surfaces ---------------------------------------------------------
    st.configure(".", background=BG, foreground=INK, font=t.f())
    st.configure("BG.TFrame", background=BG)
    st.configure("Card.TFrame", background=CARD)
    st.configure("Divider.TFrame", background=BORDER)

    # -- text -------------------------------------------------------------
    st.configure("Card.TLabel", background=CARD, foreground=INK, font=t.f())
    st.configure("Title.TLabel", background=CARD, foreground=INK,
                 font=t.f(4, "bold"))
    st.configure("Section.TLabel", background=CARD, foreground=INK,
                 font=t.f(1, "bold"))
    st.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=t.f(-1))
    st.configure("Field.TLabel", background=CARD, foreground=INK_SOFT,
                 font=t.f(-1, "bold"))
    st.configure("Status.TLabel", background=CARD, foreground=INK_SOFT, font=t.f(-1))

    # -- buttons ----------------------------------------------------------
    def button(name, bg, hover, pressed, fg=ON_ACCENT, disabled_bg="#e5e9f0",
               size=0, weight="bold", padx=16, pady=8):
        st.configure(name, background=bg, foreground=fg, font=t.f(size, weight),
                     padding=(pad(padx), pad(pady)), borderwidth=0,
                     relief="flat", focuscolor=bg, anchor="center")
        st.map(name,
               background=[("disabled", disabled_bg), ("pressed", pressed),
                           ("active", hover)],
               foreground=[("disabled", "#9aa3b2")],
               relief=[("pressed", "flat"), ("!pressed", "flat")])

    # Primary - exactly one on screen at a time.
    button("Primary.TButton", BLUE, BLUE_HOVER, BLUE_PRESSED,
           disabled_bg=BLUE_DISABLED, size=1, padx=22, pady=9)
    # Secondary - the default for everything else. Dark ink on light grey
    # reads far better than the wall of green buttons we had before.
    button("Secondary.TButton", SLATE, SLATE_HOVER, SLATE_PRESSED,
           fg=INK_SOFT, size=-1, weight="normal", padx=14, pady=7)
    button("Success.TButton", GREEN, GREEN_HOVER, GREEN_HOVER, size=-1, padx=16)
    button("Danger.TButton", RED, RED_HOVER, RED_HOVER, fg=ON_ACCENT,
           size=-1, padx=14, pady=7)

    # -- inputs -----------------------------------------------------------
    for cls in ("TEntry", "TCombobox", "TSpinbox"):
        st.configure(cls, fieldbackground=CARD, background=CARD,
                     foreground=INK, bordercolor=BORDER, lightcolor=BORDER,
                     darkcolor=BORDER, insertcolor=INK, arrowcolor=MUTED,
                     padding=(pad(7), pad(5)), font=t.f(-1))
        st.map(cls,
               bordercolor=[("focus", BLUE)],
               lightcolor=[("focus", BLUE)],
               darkcolor=[("focus", BLUE)],
               fieldbackground=[("readonly", CARD), ("disabled", "#f1f3f7")],
               foreground=[("disabled", "#9aa3b2")])
    st.map("TCombobox", selectbackground=[("readonly", CARD)],
           selectforeground=[("readonly", INK)])

    st.configure("TRadiobutton", background=CARD, foreground=INK_SOFT,
                 font=t.f(-1), focuscolor=CARD)
    st.map("TRadiobutton", foreground=[("active", INK)],
           background=[("active", CARD)])
    st.configure("TCheckbutton", background=CARD, foreground=INK_SOFT,
                 font=t.f(-1), focuscolor=CARD)

    # -- table ------------------------------------------------------------
    st.configure("Treeview",
                 background=CARD, fieldbackground=CARD, foreground=INK,
                 font=t.f(-1), rowheight=pad(30), borderwidth=0,
                 relief="flat")
    st.map("Treeview",
           background=[("selected", BLUE)],
           foreground=[("selected", ON_ACCENT)])
    st.configure("Treeview.Heading",
                 background="#f7f9fc", foreground=MUTED,
                 font=t.f(-2, "bold"), relief="flat",
                 padding=(pad(8), pad(9)), borderwidth=0)
    st.map("Treeview.Heading",
           background=[("active", "#eef2f8")],
           foreground=[("active", INK)])

    # -- misc chrome ------------------------------------------------------
    st.configure("TProgressbar", troughcolor="#e6eaf2", background=BLUE,
                 borderwidth=0, thickness=pad(8), lightcolor=BLUE,
                 darkcolor=BLUE, bordercolor="#e6eaf2")
    st.configure("Vertical.TScrollbar", background="#cfd6e2",
                 troughcolor=CARD, borderwidth=0, arrowcolor=MUTED,
                 bordercolor=CARD, lightcolor="#cfd6e2", darkcolor="#cfd6e2")
    st.configure("Horizontal.TScrollbar", background="#cfd6e2",
                 troughcolor=CARD, borderwidth=0, arrowcolor=MUTED,
                 bordercolor=CARD, lightcolor="#cfd6e2", darkcolor="#cfd6e2")
    for cls in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        st.map(cls, background=[("active", "#b6c0d0")])

    st.configure("TSeparator", background=BORDER)

    return t


def card(parent, **kw) -> tk.Frame:
    """A white panel with a hairline border (ttk cannot do 1px borders well)."""
    return tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                    highlightcolor=BORDER, highlightthickness=1, bd=0, **kw)
