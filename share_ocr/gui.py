"""Tkinter desktop UI for the Share Certificate OCR pipeline.

Layout mirrors the web mock-up:
  +----------------------------------------------------+
  |  drop zone: "Click to select or drag & drop"        |
  |  [ Extract ]                                        |
  +----------------------------------------------------+
  |  Results  (N records)                               |
  |  [thumb #1][thumb #2][thumb #3] ...                 |
  |  #  File  Name of Share  Folio No  Certificate No   |
  |     Name of Share Holder  No of Shares  ...         |
  |  [ Download CSV ]  [ Clear All ]                    |
  +----------------------------------------------------+

The table is virtualised (only the newest N rows are shown) so the UI stays
snappy even while millions of rows stream into the CSV shards in the background.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional, Tuple

from .config import (APP_NAME, APP_VERSION, SUPPORTED_DOC, SUPPORTED_EXT,
                     SUPPORTED_IMG, Settings)
from .extractor import make_thumbnail
from .pipeline import Pipeline, Stats, scan_paths
from .secrets import (PROVIDERS, add_api_key, describe, list_api_keys,
                      looks_like_key, mask, remove_api_key, resolve, test_key)
from .theme import (AMBER_TINT, BG, BLUE, BLUE_EDGE, BLUE_HOVER, BLUE_TINT,
                    BORDER, CARD, GREEN, INK, INK_SOFT, MUTED, RED, ZEBRA,
                    apply_theme, card, enable_hidpi)
from .widgets import RoundedButton

# Engines the user can pick, with plain-language labels. Internal id first.
ENGINE_CHOICES = [
    ("openai", "OpenAI vision  (best accuracy)"),
    ("tesseract", "Tesseract  (offline, free)"),
]

# Vision-capable OpenAI models, ordered the way we would actually recommend
# them for a 30-lakh certificate run: cost/throughput first, accuracy last.
OPENAI_MODELS = [
    ("gpt-4o-mini", "gpt-4o-mini  -  fast & cheap  (recommended for bulk)"),
    ("gpt-4.1-mini", "gpt-4.1-mini  -  balanced"),
    ("gpt-4.1-nano", "gpt-4.1-nano  -  cheapest, clean scans only"),
    ("gpt-4o", "gpt-4o  -  best on faded / handwritten scans"),
    ("gpt-4.1", "gpt-4.1  -  strongest reasoning"),
    ("o4-mini", "o4-mini  -  slow, good for re-checking flagged rows"),
]


def _label_for(pairs, value: str) -> str:
    for key, label in pairs:
        if key == value:
            return label
    return value


def _value_for(pairs, label: str) -> str:
    for key, lbl in pairs:
        if lbl == label:
            return key
    return label

# Optional drag & drop support (pip install tkinterdnd2)
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DND = True
except Exception:                                     # noqa: BLE001
    _DND = False

COLUMNS = [
    ("idx", "#", 44),
    ("file", "File", 200),
    ("company", "Name of Share", 170),
    ("folio", "Folio No", 80),
    ("regfolio", "Registered Folio No", 130),      # add-on #12
    ("cert", "Certificate No", 110),
    ("holder", "Name of Share Holder", 190),
    ("shares", "No of Shares", 95),
    ("facevalue", "Face Value / Share", 120),      # add-on #10
    ("sharetype", "Share Type", 110),              # add-on #11
    ("distinctive", "Distinctive No", 150),
    ("date", "Date of Issue", 115),
    ("latest", "Latest Share Holder", 180),
    ("latestfolio", "Latest Folio No", 120),
    ("foliohistory", "Folio No History", 200),
    ("holderhistory", "Share Holder History", 220),
    ("remarks", "Remarks", 160),                   # add-on #11
    ("flags", "Flags", 200),
]

MAX_TABLE_ROWS = 2000       # keep the Treeview light
# The strip scrolls horizontally, so this only protects against rendering
# thumbnails for an absurdly large folder (rasterising a PDF page per
# thumbnail isn't free) - it is not a limit on how many you can reach.
MAX_THUMBS = 60


def human_eta(seconds: Optional[float]) -> str:
    if seconds is None:
        return "--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


class App:
    def __init__(self, root: tk.Tk, settings: Settings):
        self.root = root
        self.s = settings
        self.s.ensure_dirs()
        self.pipeline = Pipeline(
            settings,
            on_row=self._enqueue_row,
            on_progress=self._enqueue_progress,
            on_log=self._enqueue_log,
        )
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.selected_paths: List[str] = []
        self._sel_token = 0          # discards stale background file counts
        self.row_count = 0
        self._thumb_imgs: List[tk.PhotoImage] = []
        # iid -> source path for rows in the "Failed" filter, which are not
        # backed by a results row_id the way extracted rows are - see
        # _reload_failed() / _row_source_path().
        self._failed_paths: Dict[str, str] = {}

        # Fonts + ttk styles first: every widget below reads self.t for sizes.
        self.t = apply_theme(root)
        px = self.t.px

        root.title(f"{APP_NAME} v{APP_VERSION}")
        root.configure(bg=BG)
        self._closing = False
        self._after_id = None

        self._build_toolbar()
        self._build_dropzone()
        # Status bar BEFORE the results card, anchored to the bottom edge.
        # Tk's packer gives each widget its requested size in packing order
        # and only shares out the surplus afterwards, so the results table -
        # which asks for a lot and expands - consumed the whole cavity and
        # left nothing for whatever was packed after it. That is how the
        # status bar ended up off the bottom of the window on a 1080p screen.
        self._build_statusbar()
        self._build_results()
        self._restore_counts()

        # Sizing last, so requested widget sizes are known.
        self._setup_window_size()

        self._after_id = self.root.after(120, self._drain_ui_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------ window size --
    def _setup_window_size(self) -> None:
        """Behave like a real desktop app.

        Rules:
          * never open bigger than the screen (the old fixed 1320x840 scaled
            by 1.5 on a hi-dpi laptop asked for 1980x1260 and got clipped);
          * a minimum that is the actual content requirement, but always
            capped to the screen so the window can still be shrunk;
          * reopen where you left it, remembering the maximized state.
        """
        root, px = self.root, self.t.px
        root.update_idletasks()

        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        # Leave room for the Windows taskbar / macOS dock.
        avail_w, avail_h = sw - px(40), sh - px(90)

        # What the layout genuinely needs, floored at a usable width.
        need_w = max(px(900), root.winfo_reqwidth())
        need_h = max(px(600), root.winfo_reqheight())

        min_w = min(need_w, max(720, int(sw * 0.55)))
        min_h = min(need_h, max(520, int(sh * 0.55)))
        root.minsize(min_w, min_h)

        saved = (self.s.window_geometry or "").strip()
        placed = False
        if saved:
            try:
                size = saved.split("+")[0]
                w, h = (int(v) for v in size.split("x"))
                # Ignore a saved geometry from a bigger monitor.
                if w <= sw and h <= sh:
                    root.geometry(saved)
                    placed = True
            except Exception:                         # noqa: BLE001
                placed = False

        if not placed:
            w = max(min_w, min(px(1280), avail_w))
            h = max(min_h, min(px(800), avail_h))
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 3)      # a third down looks better centred
            root.geometry(f"{w}x{h}+{x}+{y}")

        if self.s.window_maximized:
            try:
                root.state("zoomed")                  # Windows / most Linux WMs
            except Exception:                         # noqa: BLE001
                try:
                    root.attributes("-zoomed", True)  # some X11 WMs
                except Exception:                     # noqa: BLE001
                    pass

    def _save_window_state(self) -> None:
        try:
            maximized = self.root.state() == "zoomed"
        except Exception:                             # noqa: BLE001
            maximized = False
        try:
            geom = self.root.geometry() if not maximized else self.s.window_geometry
        except Exception:                             # noqa: BLE001
            return
        self.s.window_geometry = geom or ""
        self.s.window_maximized = maximized
        try:
            self.s.save()
        except Exception:                             # noqa: BLE001
            pass

    # ---------------------------------------------------------- toolbar --
    def _build_toolbar(self) -> None:
        t, px = self.t, self.t.px
        bar = card(self.root)
        bar.pack(fill="x", padx=px(12), pady=(px(10), 0))
        inner = ttk.Frame(bar, style="Card.TFrame", padding=(px(14), px(9)))
        inner.pack(fill="x")

        brand = ttk.Frame(inner, style="Card.TFrame")
        brand.pack(side="left")
        ttk.Label(brand, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(brand, text=f"v{APP_VERSION}  ·  offline queue, resumable",
                  style="Muted.TLabel").pack(anchor="w")

        RoundedButton(inner, text="Output folder", variant="secondary",
                      theme=t, command=self._open_output).pack(side="right")

        # API keys button + status chip. The customer gets the .exe, not the
        # source, so this is the only place keys can be entered. Supports as
        # many keys as the operator wants to add - see open_api_key_dialog.
        RoundedButton(inner, text="API keys", variant="secondary", theme=t,
                      command=self.open_api_key_dialog).pack(
            side="right", padx=(0, px(8)))

        # Not packed: the client operator does not need to see the raw key
        # status text, only the "API key" button above to set one. The
        # widgets still exist because _refresh_key_status() configures them.
        chip = tk.Frame(inner, bg=CARD, cursor="hand2")
        self.key_dot = tk.Label(chip, text="\u25cf", bg=CARD, fg=MUTED,
                                font=t.f(0))
        self.key_dot.pack(side="left", padx=(0, px(5)))
        self.key_label = tk.Label(chip, text="API key", bg=CARD, fg=INK_SOFT,
                                  font=t.f(-1), cursor="hand2")
        self.key_label.pack(side="left")
        for w in (chip, self.key_dot, self.key_label):
            w.bind("<Button-1>", lambda e: self.open_api_key_dialog())

        # Not packed: engine/workers/model are operator-facing knobs the
        # client should not see or change - they ship pre-set in
        # settings.json. The widgets still exist so the rest of App (engine
        # switching, key-status refresh, tests) keeps working unchanged.
        cfg = ttk.Frame(inner, style="Card.TFrame")

        ttk.Label(cfg, text="Engine", style="Field.TLabel").pack(
            side="left", padx=(0, px(6)))
        self.engine_var = tk.StringVar(
            value=_label_for(ENGINE_CHOICES, self.s.engine))
        self.engine_box = ttk.Combobox(
            cfg, textvariable=self.engine_var, state="readonly", font=t.f(-1),
            width=24, values=[lbl for _, lbl in ENGINE_CHOICES])
        self.engine_box.pack(side="left")
        self.engine_box.bind("<<ComboboxSelected>>", self._on_engine_change)

        # Workers first, then the model picker, which is packed only for
        # OpenAI - a model box means nothing for Tesseract or the demo engine.
        ttk.Label(cfg, text="Workers", style="Field.TLabel").pack(
            side="left", padx=(px(12), px(6)))
        self.workers_var = tk.IntVar(value=self.s.workers)
        ttk.Spinbox(
            cfg, from_=1, to=128, width=4, textvariable=self.workers_var,
            font=t.f(-1),
            command=lambda: self._apply_setting(
                "workers", self.workers_var.get())).pack(side="left")

        self.model_frame = ttk.Frame(cfg, style="Card.TFrame")
        ttk.Label(self.model_frame, text="Model", style="Field.TLabel").pack(
            side="left", padx=(px(12), px(6)))
        model_labels = [lbl for _, lbl in OPENAI_MODELS]
        known = [k for k, _ in OPENAI_MODELS]
        if self.s.model and self.s.model not in known:
            model_labels.append(self.s.model)      # keep a hand-typed model
        self.model_var = tk.StringVar(
            value=_label_for(OPENAI_MODELS, self.s.model))
        self.model_box = ttk.Combobox(
            self.model_frame, textvariable=self.model_var, state="readonly",
            font=t.f(-1), width=46, values=model_labels)
        self.model_box.pack(side="left")
        self.model_box.bind(
            "<<ComboboxSelected>>",
            lambda e: self._apply_setting(
                "model", _value_for(OPENAI_MODELS, self.model_var.get())))

        self._sync_engine_fields()

    def _on_engine_change(self, _event=None) -> None:
        self._apply_setting(
            "engine", _value_for(ENGINE_CHOICES, self.engine_var.get()))
        self._sync_engine_fields()

    def _sync_engine_fields(self) -> None:
        """Show the model picker only when it can actually do something."""
        if self.s.engine == "openai":
            self.model_frame.pack(side="left")
        else:
            self.model_frame.pack_forget()
        try:
            self._refresh_key_status()
        except Exception:                             # noqa: BLE001
            pass

    # ---------------------------------------------------------- api key --
    def _refresh_key_status(self) -> None:
        """Update the toolbar chip: green = at least one usable key, from
        either provider - OpenAI and NVIDIA keys are pooled together (see
        extractor.KeyPool), there is no separate engine to switch between
        them, so the count combines both."""
        infos = {p: describe(self.s, p) for p in PROVIDERS}
        total = sum(i["count"] for i in infos.values())
        if self.s.engine != "openai":
            self.key_dot.configure(fg=MUTED)
            self.key_label.configure(text="API key not needed")
        elif total:
            parts = ["%d %s" % (i["count"], i["provider_label"])
                    for i in infos.values() if i["count"]]
            self.key_dot.configure(fg=GREEN)
            self.key_label.configure(
                text="%d API key%s  (%s)" % (
                    total, "" if total == 1 else "s", " + ".join(parts)))
        else:
            self.key_dot.configure(fg=RED)
            self.key_label.configure(text="Add an OpenAI or NVIDIA API key")

    def open_api_key_dialog(self) -> None:
        """Manage the API key pools - OpenAI and NVIDIA, add as many keys of
        either as you like.

        Every worker draws from ONE shared pool made of both providers'
        keys together (extractor.KeyPool) and automatically moves to the
        next key if one is rate-limited or out of quota, instead of failing
        the file - a file only ever goes to whichever single key answers
        first, never both. NVIDIA keys are usually much cheaper per image
        than OpenAI's, so adding some alongside OpenAI keys lowers the
        average cost of a run; there is no separate engine to switch
        between them, both just feed the same "openai" engine's pool. Keys
        are never echoed to the screen by default and never written to the
        CSV, the log or the queue database.
        """
        t, px = self.t, self.t.px
        win = tk.Toplevel(self.root)
        win.title("API keys")
        win.configure(bg=BG)
        win.transient(self.root)
        win.resizable(False, False)

        body = ttk.Frame(win, style="Card.TFrame", padding=(px(16), px(14)))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="API keys", style="Section.TLabel").pack(anchor="w")
        ttk.Label(body, style="Muted.TLabel", wraplength=px(460), justify="left",
                  text=("Add one or more keys, from either provider. "
                        "Extraction spreads requests across every key from "
                        "both pools and switches to the next the moment one "
                        "hits a rate limit or runs out of quota, so one "
                        "key's limit never stalls or fails a run - a file "
                        "is still only ever sent to ONE key, never both. "
                        "NVIDIA keys are typically much cheaper per image, "
                        "so mixing some in lowers the average cost. Stored "
                        "in your operating system's credential manager when "
                        "available, otherwise in an obfuscated file only "
                        "your user account can read.")
                  ).pack(anchor="w", pady=(px(4), px(10)))

        provider_var = tk.StringVar(value="openai")
        prov_row = ttk.Frame(body, style="Card.TFrame")
        prov_row.pack(anchor="w", pady=(0, px(8)))
        for pid, info in PROVIDERS.items():
            ttk.Radiobutton(prov_row, text=info.label, value=pid,
                            variable=provider_var,
                            command=lambda: refresh_rows()).pack(
                side="left", padx=(0, px(14)))

        self._dlg_status = ttk.Label(body, style="Muted.TLabel",
                                     wraplength=px(460), justify="left", text="")
        rows = ttk.Frame(body, style="Card.TFrame")
        rows.pack(fill="x", pady=(0, px(6)))

        def set_status(msg: str) -> None:
            try:
                self._dlg_status.configure(text=msg)
            except Exception:                         # noqa: BLE001
                pass

        def do_test(key: str, provider: str) -> None:
            set_status("Testing %s\u2026" % mask(key))
            win.update_idletasks()
            ok, msg = test_key(self.s, key, provider=provider)
            set_status(("OK - " if ok else "Failed - ") + msg)

        def do_remove(key: str, provider: str) -> None:
            remove_api_key(self.s, key, provider=provider)
            set_status("Removed %s." % mask(key))
            refresh_rows()

        def refresh_rows() -> None:
            provider = provider_var.get()
            for w in rows.winfo_children():
                w.destroy()
            keys = list_api_keys(self.s, provider)
            if not keys:
                ttk.Label(rows, text="No %s keys saved yet - add one below."
                          % PROVIDERS[provider].label,
                          style="Muted.TLabel").pack(anchor="w")
            for key in keys:
                r = ttk.Frame(rows, style="Card.TFrame")
                r.pack(fill="x", pady=(0, px(4)))
                ttk.Label(r, text=mask(key), style="Card.TLabel",
                          font=t.fm(-1)).pack(side="left")
                RoundedButton(r, text="Remove", variant="danger", theme=t,
                              command=lambda k=key, p=provider: do_remove(k, p)
                              ).pack(side="right")
                RoundedButton(r, text="Test", variant="secondary", theme=t,
                              command=lambda k=key, p=provider: do_test(k, p)
                              ).pack(side="right", padx=(0, px(6)))
            count_label.configure(
                text="%d %s key%s configured" % (
                    len(keys), PROVIDERS[provider].label,
                    "" if len(keys) == 1 else "s"))
            self._refresh_key_status()

        count_label = ttk.Label(body, style="Field.TLabel", text="")
        count_label.pack(anchor="w")
        rows.pack(fill="x")

        add_row = ttk.Frame(body, style="Card.TFrame")
        add_row.pack(fill="x", pady=(px(10), 0))
        key_var = tk.StringVar(value="")
        entry = ttk.Entry(add_row, textvariable=key_var, width=42, show="\u2022",
                          font=t.fm(-1))
        entry.pack(side="left")
        entry.focus_set()

        def do_add() -> None:
            provider = provider_var.get()
            key = key_var.get().strip()
            if not key:
                set_status("Paste a key first.")
                return
            if not looks_like_key(provider, key):
                prefix = PROVIDERS[provider].key_prefixes[0]
                set_status("That does not look like an %s key (they start "
                           "with '%s'). Adding it anyway."
                           % (PROVIDERS[provider].label, prefix))
            where = add_api_key(self.s, key, provider=provider)
            key_var.set("")
            set_status("Added, stored in %s." % where)
            refresh_rows()

        entry.bind("<Return>", lambda e: do_add())
        RoundedButton(add_row, text="Add key", variant="primary", theme=t,
                      command=do_add).pack(side="left", padx=(px(8), 0))

        show_var = tk.IntVar(value=0)

        def toggle_show():
            entry.configure(show="" if show_var.get() else "\u2022")

        ttk.Checkbutton(body, text="Show key while typing", variable=show_var,
                        command=toggle_show).pack(anchor="w", pady=(px(6), 0))
        self._dlg_status.pack(anchor="w", pady=(px(6), 0))

        footer = ttk.Frame(body, style="Card.TFrame")
        footer.pack(fill="x", pady=(px(10), 0))
        RoundedButton(footer, text="Close", variant="ghost", theme=t,
                      command=win.destroy).pack(side="right")

        refresh_rows()

    # --------------------------------------------------------- dropzone --
    def _build_dropzone(self) -> None:
        t, px = self.t, self.t.px
        panel = card(self.root)
        panel.pack(fill="x", padx=px(12), pady=(px(8), px(8)))
        box = ttk.Frame(panel, style="Card.TFrame", padding=(px(14), px(10)))
        box.pack(fill="x")

        # Dashed-look drop target: tinted fill + soft blue edge reads as a
        # target without the heavy 2px navy outline we had before.
        self.drop = tk.Frame(box, bg=BLUE_TINT, highlightbackground=BLUE_EDGE,
                             highlightcolor=BLUE_EDGE, highlightthickness=1,
                             height=px(92), cursor="hand2")
        self.drop.pack(fill="x")
        self.drop.pack_propagate(False)

        self.drop_icon = tk.Label(self.drop, text="\U0001F5BC", bg=BLUE_TINT,
                                  fg=BLUE, font=(t.ui, t.base + 12))
        self.drop_icon.pack(pady=(px(9), 0))
        self.drop_title = tk.Label(
            self.drop, text="Click to select  or  drag & drop images here",
            bg=BLUE_TINT, fg=BLUE, font=t.f(1, "bold"), cursor="hand2")
        self.drop_title.pack(pady=(px(3), 0))
        self.drop_hint = tk.Label(
            self.drop,
            text="JPG  ·  PNG  ·  WEBP  ·  TIFF  ·  PDF      multiple files or a whole folder",
            bg=BLUE_TINT, fg=MUTED, font=t.f(-1))
        self.drop_hint.pack(pady=(px(2), 0))

        # Hover feedback
        def _tint(colour):
            for w in (self.drop, self.drop_icon, self.drop_title, self.drop_hint):
                w.configure(bg=colour)
        self.drop.bind("<Enter>", lambda e: _tint("#e4edff"))
        self.drop.bind("<Leave>", lambda e: _tint(BLUE_TINT))

        for w in (self.drop, self.drop_title, self.drop_hint):
            w.bind("<Button-1>", lambda e: self.pick_files())

        if _DND:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>", self._on_drop)

        btns = ttk.Frame(box, style="Card.TFrame")
        btns.pack(fill="x", pady=(px(10), 0))
        self.btn_extract = RoundedButton(btns, text="Extract", variant="primary",
                                         theme=t, min_width=px(130),
                                         command=self.start_extract)
        self.btn_extract.pack(side="left")
        RoundedButton(btns, text="Select folder", variant="secondary", theme=t,
                   command=self.pick_folder).pack(side="left", padx=px(8))
        self.btn_pause = RoundedButton(btns, text="Pause", variant="secondary", theme=t,
                                    command=self.toggle_pause, state="disabled")
        self.btn_pause.pack(side="left")
        self.btn_stop = RoundedButton(btns, text="Stop", variant="danger", theme=t,
                                   command=self.stop_extract, state="disabled")
        self.btn_stop.pack(side="left", padx=px(8))
        # Row actions live HERE, in the fixed-height action card, rather than
        # in a footer under the results table. Two reasons:
        #   * the results card expands, and anything packed after the table
        #     was the first thing Tk dropped when the window was shorter than
        #     the content - these buttons were being pushed off-screen
        #     entirely on a 1080p display;
        #   * every other verb (Extract, Stop, Pause) is already on this row,
        #     so this is where an operator looks for an action.
        RoundedButton(btns, text="Clear all", variant="danger", theme=t,
                   command=self.clear_all).pack(side="right")
        RoundedButton(btns, text="Delete selected", variant="danger", theme=t,
                   command=self.delete_selected).pack(side="right", padx=(0, px(8)))
        RoundedButton(btns, text="Select all", variant="secondary", theme=t,
                   command=self.select_all_rows).pack(side="right", padx=(0, px(8)))

        self.sel_label = ttk.Label(btns, text="Nothing selected", style="Muted.TLabel")
        self.sel_label.pack(side="right", padx=(0, px(16)), pady=(px(6), 0))
        # The "Open" button was removed by request; double-clicking a row still
        # opens the scan behind it (see _open_source_file).

    # ---------------------------------------------------------- results --
    def _build_results(self) -> None:
        t, px = self.t, self.t.px
        panel = card(self.root)
        panel.pack(fill="both", expand=True, padx=px(12))
        box = ttk.Frame(panel, style="Card.TFrame", padding=(px(14), px(10)))
        box.pack(fill="both", expand=True)

        head = ttk.Frame(box, style="Card.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="Results", style="Section.TLabel").pack(side="left")
        self.badge = tk.Label(head, text="0 records", bg=BLUE_TINT, fg=BLUE,
                              font=t.f(-2, "bold"), padx=px(10), pady=px(3),
                              bd=0)
        self.badge.pack(side="left", padx=px(10))

        # "All" (extracted rows) and "Failed" (files that errored out during
        # extraction - see _reload_failed()). Packed in this order because
        # side="right" stacks right-to-left, so this reads "All  Failed"
        # left-to-right.
        self.filter_var = tk.StringVar(value="all")
        ttk.Radiobutton(head, text="Failed", value="failed", variable=self.filter_var,
                        command=self._reload_table).pack(side="right", padx=px(12))
        ttk.Radiobutton(head, text="All", value="all", variable=self.filter_var,
                        command=self._reload_table).pack(side="right", padx=px(12))

        # Legend so the row tints are self-explanatory.
        legend = ttk.Frame(box, style="Card.TFrame")
        legend.pack(fill="x", pady=(px(6), 0))
        for colour, label in ((BLUE_TINT, "add-on not captured"),
                              (AMBER_TINT, "needs review")):
            chip = tk.Frame(legend, bg=colour, width=px(14), height=px(14),
                            highlightbackground=BORDER, highlightthickness=1)
            chip.pack(side="left", padx=(0, px(5)))
            chip.pack_propagate(False)
            tk.Label(legend, text=label, bg=CARD, fg=MUTED,
                     font=t.f(-2)).pack(side="left", padx=(0, px(16)))

        # Thumbnail strip is packed on demand. Reserving ~96px for it when
        # nothing is selected was the single biggest band of dead space.
        # It scrolls horizontally so a selection larger than MAX_THUMBS is
        # still fully reachable by scrolling, not just the first screenful.
        self.thumb_bar = tk.Frame(box, bg=CARD)
        self._thumb_canvas = tk.Canvas(self.thumb_bar, bg=CARD,
                                       height=self.t.px(96),
                                       highlightthickness=0)
        thumb_scroll = ttk.Scrollbar(self.thumb_bar, orient="horizontal",
                                     command=self._thumb_canvas.xview)
        self._thumb_canvas.configure(xscrollcommand=thumb_scroll.set)
        self._thumb_canvas.pack(side="top", fill="x")
        thumb_scroll.pack(side="top", fill="x")
        self.thumb_inner = tk.Frame(self._thumb_canvas, bg=CARD)
        self._thumb_canvas.create_window((0, 0), window=self.thumb_inner,
                                         anchor="nw")
        self.thumb_inner.bind(
            "<Configure>",
            lambda e: self._thumb_canvas.configure(
                scrollregion=self._thumb_canvas.bbox("all")))

        wrap = ttk.Frame(box, style="Card.TFrame")
        wrap.pack(fill="both", expand=True, pady=(px(8), 0))
        self._table_wrap = wrap
        # A modest requested height: the table expands to fill the window
        # anyway, and the default inflated winfo_reqheight() enough to push
        # the computed minimum window size past the screen.
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in COLUMNS],
                                 show="headings", selectmode="extended",
                                 height=6)
        for key, label, width in COLUMNS:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w",
                             stretch=key in ("company", "holder", "latest", "flags"))
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.tree.tag_configure("flagged", background=AMBER_TINT, foreground=INK)
        self.tree.tag_configure("addon", background=BLUE_TINT, foreground=INK)
        self.tree.tag_configure("odd", background=ZEBRA, foreground=INK)
        self.tree.bind("<Double-1>", self._open_source_file)
        self.tree.bind("<Delete>", lambda e: self.delete_selected())
        self.tree.bind("<Control-a>", lambda e: self.select_all_rows())
        self.tree.bind("<Control-A>", lambda e: self.select_all_rows())
        # No footer under the table: the row actions moved up to the action
        # row, and the CSV buttons are gone (shards are written continuously,
        # and the toolbar's "Output folder" already opens the same place).

    # -------------------------------------------------------- statusbar --
    def _build_statusbar(self) -> None:
        t, px = self.t, self.t.px
        panel = card(self.root)
        panel.pack(side="bottom", fill="x", padx=px(12), pady=(px(8), px(10)))
        bar = ttk.Frame(panel, style="Card.TFrame", padding=(px(14), px(9)))
        bar.pack(fill="x")
        self.progress = ttk.Progressbar(bar, mode="determinate", maximum=100)
        self.progress.pack(fill="x")
        self.status = ttk.Label(bar, text="Ready.", style="Status.TLabel")
        self.status.pack(anchor="w", pady=(px(6), 0))

    # ------------------------------------------------------ interaction --
    def _apply_setting(self, key: str, value) -> None:
        setattr(self.s, key, value)
        self.s.save()
        self.pipeline.s = self.s

    def pick_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Select certificate images / PDFs",
            filetypes=[("Certificates", " ".join(f"*{e}" for e in SUPPORTED_EXT)),
                       ("All files", "*.*")])
        if paths:
            self.selected_paths = list(paths)
            self._update_selection()

    def pick_folder(self) -> None:
        d = filedialog.askdirectory(title="Select a folder of certificates")
        if d:
            self.selected_paths = [d]
            self._update_selection()

    def _on_drop(self, event) -> None:
        paths = self.root.tk.splitlist(event.data)
        self.selected_paths = [p for p in paths if os.path.exists(p)]
        self._update_selection()

    # Stop counting a monstrous tree once the number stops being useful to
    # read; the exact total comes from the queue after Extract anyway.
    SELECTION_COUNT_CAP = 200_000

    @staticmethod
    def count_selection(paths: List[str], cap: int = SELECTION_COUNT_CAP):
        """(images, pdfs, capped) for a selection of files and/or folders.

        Only counts extensions the pipeline will actually accept, so the
        number shown matches the number that gets queued.
        """
        images = pdfs = 0
        for p in paths:
            if os.path.isdir(p):
                for fp, _, _ in scan_paths(p):
                    if fp.lower().endswith(SUPPORTED_DOC):
                        pdfs += 1
                    else:
                        images += 1
                    if images + pdfs >= cap:
                        return images, pdfs, True
            elif os.path.isfile(p):
                low = p.lower()
                if low.endswith(SUPPORTED_DOC):
                    pdfs += 1
                elif low.endswith(SUPPORTED_IMG):
                    images += 1
        return images, pdfs, False

    @staticmethod
    def describe_selection(images: int, pdfs: int, capped: bool = False) -> str:
        """'10 files found  ·  3 images, 7 PDFs' - plural-correct."""
        total = images + pdfs
        if not total:
            return "No supported files found in the selection"
        parts = []
        if images:
            parts.append(f"{images:,} image" + ("s" if images != 1 else ""))
        if pdfs:
            parts.append(f"{pdfs:,} PDF" + ("s" if pdfs != 1 else ""))
        prefix = f"{total:,}+" if capped else f"{total:,}"
        return (f"{prefix} file" + ("s" if total != 1 else "") +
                " found  ·  " + ", ".join(parts))

    def _update_selection(self) -> None:
        if not self.selected_paths:
            self.sel_label.configure(text="Nothing selected")
            return
        # Counting walks the whole tree, which for the 30-lakh job is
        # millions of entries - never on the UI thread. Show the folder
        # immediately, then fill in the breakdown when the walk finishes.
        first = os.path.basename(self.selected_paths[0]) or self.selected_paths[0]
        self.sel_label.configure(text=f"Counting {first}…")
        self._sel_token += 1
        token = self._sel_token
        paths = list(self.selected_paths)
        threading.Thread(target=self._count_selection_bg, args=(paths, token),
                         daemon=True, name="sel-count").start()
        self._show_thumbs(self.selected_paths, token=token)

    def _count_selection_bg(self, paths: List[str], token: int) -> None:
        try:
            images, pdfs, capped = self.count_selection(paths)
        except Exception:                             # noqa: BLE001
            return
        self.ui_queue.put(("selection", (token, images, pdfs, capped)))

    @staticmethod
    def _selection_files(paths: List[str], limit: int) -> Tuple[List[str], bool]:
        """First `limit` files in the selection, plus 'there were more'."""
        files: List[str] = []
        for p in paths:
            if os.path.isdir(p):
                for fp, _, _ in scan_paths(p):
                    if len(files) >= limit:
                        return files, True
                    files.append(fp)
            elif os.path.isfile(p):
                if len(files) >= limit:
                    return files, True
                files.append(p)
        return files, False

    def _show_thumbs(self, paths: List[str],
                     token: Optional[int] = None) -> None:
        # One token per selection change, shared with the file count. Bumping
        # it again here would make the count's own result look stale and get
        # thrown away.
        if token is None:
            self._sel_token += 1
            token = self._sel_token
        for w in self.thumb_inner.winfo_children():
            w.destroy()
        self._thumb_imgs.clear()
        if not paths:
            self.thumb_bar.pack_forget()
            return
        # Show the strip only when there is something in it.
        self.thumb_bar.pack(fill="x", pady=(self.t.px(8), 0),
                            before=self._table_wrap)
        tk.Label(self.thumb_inner, text="Rendering previews…", bg=CARD, fg=MUTED,
                 font=self.t.f(-2)).pack(side="left", padx=4)
        # Previews are built off the UI thread: a PDF page has to be rasterised
        # to preview it, and a dozen of those on the UI thread freezes the
        # window. The worker only writes JPEGs; Tk images are created back on
        # the main thread, which is the only place that is safe.
        threading.Thread(target=self._build_thumbs_bg,
                         args=(list(paths), token),
                         daemon=True, name="thumbs").start()

    def _build_thumbs_bg(self, paths: List[str], token: int) -> None:
        files, more = self._selection_files(paths, MAX_THUMBS)
        size = self.t.px(76)
        items = []
        for i, f in enumerate(files, start=1):
            if token != self._sel_token:
                return                      # selection changed under us
            dest = self.s.thumb_dir / f"t{token}_{i}.jpg"
            items.append((i, f, make_thumbnail(f, dest, size)))
        self.ui_queue.put(("thumbs", (token, items, more)))

    def _render_thumbs(self, items, more: bool) -> None:
        for w in self.thumb_inner.winfo_children():
            w.destroy()
        self._thumb_imgs.clear()
        for i, src, thumb in items:
            holder = tk.Frame(self.thumb_inner, bg=CARD, cursor="hand2")
            holder.pack(side="left", padx=4)
            if thumb:
                try:
                    from PIL import Image, ImageTk
                    img = ImageTk.PhotoImage(Image.open(thumb))
                    self._thumb_imgs.append(img)
                    body = tk.Label(holder, image=img, bg=CARD, cursor="hand2")
                except Exception:                    # noqa: BLE001
                    body = tk.Label(holder, text="?", bg=BLUE_TINT, fg=MUTED,
                                    font=self.t.f(-2), width=10, height=4)
            else:
                # A preview can fail (corrupt scan, encrypted PDF) without the
                # file itself being unreadable, so still offer it for opening.
                body = tk.Label(holder, text=Path(src).suffix.upper().lstrip(".")
                                or "FILE", bg=BLUE_TINT, fg=MUTED,
                                font=self.t.f(-2), width=10, height=4,
                                cursor="hand2")
            body.pack()
            cap = tk.Label(holder, text=f"#{i}", bg=BLUE, fg="white",
                           font=self.t.f(-3, "bold"), pady=self.t.px(2),
                           cursor="hand2")
            cap.pack(fill="x")
            # Click a preview to open the actual scan - the fastest way to
            # check a row against the certificate it came from.
            for w in (holder, body, cap):
                w.bind("<Button-1>", lambda e, p=src: self._open_preview(p))
        if more:
            tk.Label(self.thumb_inner,
                     text=f"…  first {MAX_THUMBS} of the selection",
                     bg=CARD, fg=MUTED, font=self.t.f(-2)).pack(
                side="left", padx=8)

    def _open_preview(self, path: str) -> None:
        if not os.path.exists(path):
            self.status.configure(text=f"Missing on disk: {path}")
            return
        if self._open_path(path):
            self.status.configure(text=f"Opened {os.path.basename(path)}")
        else:
            self.status.configure(text=f"Could not open {path}")

    # ----------------------------------------------------------- run ----
    def start_extract(self) -> None:
        if not self.selected_paths:
            messagebox.showinfo(APP_NAME, "Select files or a folder first.")
            return
        # No key? Open the key dialog instead of a dead-end "this will fail"
        # warning. The key may live in the OS keyring or the settings file,
        # so ask secrets.resolve() rather than only the environment. Either
        # provider's key is enough - the pool draws from both (see
        # extractor.KeyPool), there is no separate "nvidia" engine to pick.
        if (self.s.engine == "openai"
                and not resolve(self.s, "openai")[0]
                and not resolve(self.s, "nvidia")[0]):
            self.status.configure(text="Add an OpenAI or NVIDIA API key to continue.")
            self.open_api_key_dialog()
            return
        self.btn_extract.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_pause.configure(state="normal")
        self.status.configure(text="Indexing files… (this streams, so millions are fine)")
        threading.Thread(target=self._run_job, daemon=True).start()

    def _run_job(self) -> None:
        try:
            added = self.pipeline.ingest(self.selected_paths)
            # Scope this run to what is actually selected. Without this,
            # Extract drains the whole resumable queue, so a single new
            # file picked after an earlier selection left other files
            # pending would silently reprocess those too.
            scope_ids = self.pipeline.scope_ids_for(self.selected_paths)
            self._enqueue_log(f"{added:,} file(s) queued. Starting {self.s.workers} workers "
                              f"on {len(scope_ids):,} file(s) from this selection.")
            self.pipeline.start(scope_ids=scope_ids)
            self.pipeline.join()
            self._enqueue_log("Finished.")
        except Exception as e:                        # noqa: BLE001
            self._enqueue_log(f"FATAL: {e}")
        finally:
            self.ui_queue.put(("finished", None))

    def toggle_pause(self) -> None:
        paused = self.btn_pause.cget("text") == "Pause"
        self.pipeline.pause(paused)
        self.btn_pause.configure(text="Resume" if paused else "Pause")

    def stop_extract(self) -> None:
        self.pipeline.stop()
        self.status.configure(text="Stopping… (progress is saved, you can resume later)")

    # ------------------------------------------------------ ui updates --
    def _enqueue_row(self, row: Dict) -> None:
        self.ui_queue.put(("row", row))

    def _enqueue_progress(self, stats: Stats) -> None:
        self.ui_queue.put(("progress", stats))

    def _enqueue_log(self, msg: str) -> None:
        self.ui_queue.put(("log", msg))

    def _drain_ui_queue(self) -> None:
        if self._closing:
            return
        processed = 0
        while processed < 200:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if kind == "row":
                self._insert_row(payload)
            elif kind == "progress":
                self._update_progress(payload)
            elif kind == "log":
                self.status.configure(text=str(payload))
            elif kind == "selection":
                token, images, pdfs, capped = payload
                # Ignore a count that finished after the operator already
                # picked something else.
                if token == self._sel_token:
                    self.sel_label.configure(
                        text=self.describe_selection(images, pdfs, capped))
            elif kind == "thumbs":
                token, items, more = payload
                if token == self._sel_token:
                    self._render_thumbs(items, more)
            elif kind == "finished":
                self.btn_extract.configure(state="normal")
                self.btn_stop.configure(state="disabled")
                self.btn_pause.configure(state="disabled", text="Pause")
                if self.filter_var.get() == "failed":
                    self._reload_table()
        if not self._closing:
            self._after_id = self.root.after(150, self._drain_ui_queue)

    def _insert_row(self, row: Dict) -> None:
        self.row_count += 1
        if self.filter_var.get() != "all":
            # A live successful extraction arrived while the operator is
            # looking at the "Failed" tab. Don't leak it into that list or
            # stomp its badge with this row's "N record(s)" text - row_count
            # still advances so the running total is correct the moment
            # they switch back to "All" (which also fully reloads from the
            # DB regardless, so nothing is lost either way).
            return
        dist = ""
        if row.get("distinctive_from") or row.get("distinctive_to"):
            dist = f"{row.get('distinctive_from','')} - {row.get('distinctive_to','')}"
        flags = str(row.get("validation_flags", ""))
        tags = []
        if flags and flags.startswith("Add-on not captured"):
            tags.append("addon")       # blue: only a billed add-on is missing
        elif flags:
            tags.append("flagged")     # amber: a core field failed validation
        elif self.row_count % 2:
            tags.append("odd")
        # iid = the DB row_id (as a string) so a Treeview selection maps
        # straight back to real rows for delete_selected(). Falls back to
        # an auto-generated iid if a row somehow arrives without one.
        row_id = row.get("row_id")
        iid = str(row_id) if row_id is not None and not self.tree.exists(str(row_id)) else None
        self.tree.insert(
            "", 0, iid=iid, values=(
                self.row_count, row.get("source_file", ""), row.get("company_name", ""),
                row.get("folio_no", ""), row.get("registered_folio_no", ""),
                row.get("certificate_no", ""), row.get("share_holder_name", ""),
                row.get("no_of_shares", ""), row.get("face_value_per_share", ""),
                row.get("share_type", ""), dist, row.get("date_of_issue", ""),
                row.get("latest_share_holder_name", ""), row.get("latest_folio_no", ""),
                row.get("folio_no_history", ""), row.get("share_holder_history", ""),
                row.get("remarks", ""), row.get("validation_flags", "")),
            tags=tags)
        children = self.tree.get_children()
        if len(children) > MAX_TABLE_ROWS:
            self.tree.delete(*children[MAX_TABLE_ROWS:])
        self.badge.configure(text=f"{self.row_count:,} record(s)")

    def _update_progress(self, st: Stats) -> None:
        # st.failed only counts a file once it is permanently dead (every
        # retry, across every configured API key, exhausted - see
        # extractor.KeyPool and pipeline._process_one), so it is a small,
        # meaningful number rather than per-attempt noise - worth showing.
        # What stays suppressed is the scary PER-FILE "ERROR ..." message
        # (routed to share_ocr.log only); this is just the running total,
        # and the "Failed" tab (see _reload_failed) lists which files.
        total = max(1, st.total)
        pct = 100.0 * (st.done + st.failed) / total
        self.progress.configure(value=min(100.0, pct))
        done_part = f"{st.done:,}/{st.total:,} done"
        if st.failed:
            done_part += f" · {st.failed:,} failed"
        self.status.configure(
            text=(f"{done_part} · {st.rows:,} rows · "
                  f"{st.rate:.1f} files/s · "
                  f"ETA {human_eta(st.eta_seconds)} · CSV → {self.s.csv_dir}"))

    def _restore_counts(self) -> None:
        c = self.pipeline.counts()
        self.row_count = c.get("rows", 0)
        self.badge.configure(text=f"{self.row_count:,} record(s)")
        if c.get("total"):
            self.status.configure(
                text=(f"Resumable queue found: {c['total']:,} files "
                      f"({c['done']:,} done, {c['pending']:,} pending). "
                      "Press Extract to continue."))
            self._reload_table()

    def _reload_table(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._failed_paths.clear()
        if self.filter_var.get() == "failed":
            self._reload_failed()
            return
        import json
        rows = self.pipeline.q.recent_rows(400)
        shown = 0
        for r in reversed(rows):
            rec = json.loads(r["payload"])
            rec["source_file"] = r["name"]
            rec["validation_flags"] = r["flags"]
            rec["row_id"] = r["row_id"]
            self.row_count = shown
            self._insert_row(rec)
            shown = self.row_count
        # recent_rows() is capped at 400 for table-preview performance, but
        # the badge must reflect the TRUE total extracted so far - a bulk
        # run can have far more than 400 rows. Re-query it rather than
        # trusting `shown`, which would otherwise silently cap the badge at
        # 400 forever after the first reload (e.g. every app restart).
        self.row_count = self.pipeline.q.counts().get("rows", shown)
        self.badge.configure(text=f"{self.row_count:,} record(s)")

    def _reload_failed(self) -> None:
        """Populate the table with files that errored out during extraction
        (queue status 'failed' - will auto-retry on the next Extract - or
        'dead' - gave up after max_attempts). This is a read-only look at
        what could not be read; there is deliberately no retry action here,
        since the multi-key pool (extractor.KeyPool) already tries every
        configured key before a file can end up in this list at all, so
        what's left is either a genuinely bad scan or a run still in
        progress. Only the File and Flags columns apply to a failed file -
        everything else is blank."""
        fails = self.pipeline.q.failures(400)
        for i, r in enumerate(fails, start=1):
            iid = f"f{r['id']}"
            self._failed_paths[iid] = r["path"]
            detail = f"{r['error']}  (attempt {r['attempts']}/{self.s.max_attempts})"
            values = [""] * len(COLUMNS)
            values[0] = i
            values[1] = r["name"]
            values[-1] = detail
            if not self.tree.exists(iid):
                self.tree.insert("", "end", iid=iid, values=values,
                                 tags=("flagged",))
        self.badge.configure(
            text=f"{len(fails):,} failed file" + ("" if len(fails) == 1 else "s"))

    # ---------------------------------------------------------- output --
    def download_csv(self) -> None:
        dest = filedialog.asksaveasfilename(
            title="Save merged CSV", defaultextension=".csv",
            initialfile="certificates.csv", filetypes=[("CSV", "*.csv")])
        if not dest:
            return
        try:
            out = self.pipeline.export_single_csv(dest)
            messagebox.showinfo(APP_NAME, f"Saved:\n{out}")
        except Exception as e:                        # noqa: BLE001
            messagebox.showerror(APP_NAME, str(e))

    def download_excel(self) -> None:
        """One polished .xlsx with real (blue, underlined) Source File
        links - a plain .csv cannot carry that at all, since it has no
        concept of cell styling; Excel only auto-applies the hyperlink
        look to a real hyperlink object, never to a =HYPERLINK() formula
        result. See csv_writer.ShardedCsvWriter.merge_into_excel."""
        dest = filedialog.asksaveasfilename(
            title="Save as Excel", defaultextension=".xlsx",
            initialfile="certificates.xlsx",
            filetypes=[("Excel workbook", "*.xlsx")])
        if not dest:
            return
        try:
            out = self.pipeline.export_excel(dest)
            messagebox.showinfo(APP_NAME, f"Saved:\n{out}")
        except Exception as e:                        # noqa: BLE001
            messagebox.showerror(APP_NAME, str(e))

    @staticmethod
    def _open_path(path: str) -> bool:
        """Hand a file or folder to the OS default application."""
        try:
            if os.name == "nt":
                os.startfile(path)                    # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            return True
        except Exception:                             # noqa: BLE001
            return False

    def _open_output(self) -> None:
        path = str(self.s.csv_dir)
        if not self._open_path(path):
            messagebox.showinfo(APP_NAME, path)

    # How many scans one click may open. Enough to compare a few rows,
    # few enough that "Select all" then "Open" cannot carpet the desktop
    # with 400 image viewers.
    MAX_OPEN_AT_ONCE = 5

    def open_selected_source(self) -> None:
        """Open the original image/PDF for the highlighted row(s)."""
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(
                APP_NAME, "Select a row in the table first, then press Open "
                          "to view the certificate it was read from.")
            return

        wanted = list(sel)[:self.MAX_OPEN_AT_ONCE]
        opened, missing, unknown = [], [], 0
        for iid in wanted:
            path = self._row_source_path(iid)
            if not path:
                unknown += 1
            elif not os.path.exists(path):
                missing.append(path)
            elif self._open_path(path):
                opened.append(os.path.basename(path))
            else:
                missing.append(path)

        if missing:
            messagebox.showwarning(
                APP_NAME,
                "Could not open:\n\n" + "\n".join(missing[:5]) +
                "\n\nThe scan may have been moved, renamed or deleted since "
                "it was extracted.")
        if unknown and not opened:
            messagebox.showinfo(
                APP_NAME, "That row has no source file recorded, so there is "
                          "nothing to open.")
        if opened:
            extra = ""
            if len(sel) > len(wanted):
                extra = (f"  ({len(sel):,} rows selected; opened the first "
                         f"{len(wanted)})")
            self.status.configure(
                text="Opened " + ", ".join(opened) + extra)

    def _row_source_path(self, iid) -> Optional[str]:
        """Absolute path of the scan behind a table row.

        For an extracted row, iid IS the database row_id, which is the only
        reliable link: the visible "File" column holds just the base name,
        and several folders in a 30-lakh run will contain the same name.
        For a "Failed" row, iid is "f<file_id>" instead (there is no results
        row_id to key off since extraction never succeeded), so its path
        comes from the cache _reload_failed() fills in.
        """
        if iid in self._failed_paths:
            return self._failed_paths[iid]
        try:
            row_id = int(iid)
        except (TypeError, ValueError):
            return None
        try:
            return self.pipeline.q.source_path(row_id)
        except Exception:                             # noqa: BLE001
            return None

    def _open_source_file(self, _event=None) -> None:
        """Double-click a row. Used to only print the file name."""
        self.open_selected_source()

    def select_all_rows(self) -> str:
        """Select every row in the table (button, or Ctrl+A).

        "Delete selected" has always accepted multiple rows, but the only way
        to select them was ctrl-clicking one at a time, which is unusable
        past a handful - so clearing a table of scraped results looked like
        it needed a feature that was already there.
        """
        children = self.tree.get_children()
        if not children:
            self.status.configure(text="Nothing in the table to select.")
            return "break"
        self.tree.selection_set(children)
        self.status.configure(
            text=(f"Selected {len(children):,} row(s). 'Delete selected' "
                  "removes them from the CSV and the queue; 'Clear all' "
                  "wipes everything including the queue."))
        return "break"

    def delete_selected(self) -> None:
        """Delete the checked/highlighted row(s): from the table, the CSV
        shards and the queue database. The source file goes back to
        'pending' so a later Extract can redo it if it's still needed."""
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Select one or more rows first.")
            return
        if self.filter_var.get() == "failed":
            # A failed file never produced an extracted row, so there is
            # nothing in the CSV/results table to delete here - it just
            # needs a real fix (or simply leaving alone: it auto-retries on
            # the next Extract unless it's already 'dead').
            messagebox.showinfo(
                APP_NAME, "Failed files have no extracted row to delete. "
                          "Switch to 'All' to delete extracted rows, or "
                          "just press Extract again - anything not yet "
                          "given up on retries automatically.")
            return
        row_ids: List[int] = []
        for iid in sel:
            try:
                row_ids.append(int(iid))
            except ValueError:
                continue
        if not row_ids:
            return
        n = len(row_ids)
        if not messagebox.askyesno(
                APP_NAME,
                f"Delete {n} selected row(s)?\n\n"
                "They are removed from the CSV output and the queue "
                "database. The source file is put back to pending so "
                "Extract can redo it if you still need that certificate."):
            return
        try:
            self.pipeline.delete_rows(row_ids)
        except Exception as e:                        # noqa: BLE001
            messagebox.showerror(APP_NAME, str(e))
            return
        self.tree.delete(*sel)
        self.row_count = max(0, self.row_count - n)
        self.badge.configure(text=f"{self.row_count:,} record(s)")
        self.status.configure(text=f"Deleted {n} row(s).")

    def clear_all(self) -> None:
        if not messagebox.askyesno(
                APP_NAME, "Delete the queue, all results and all CSV shards?"):
            return
        self.pipeline.clear()
        self.tree.delete(*self.tree.get_children())
        for w in self.thumb_inner.winfo_children():
            w.destroy()
        self.row_count = 0
        self.selected_paths = []
        self.badge.configure(text="0 records")
        self.sel_label.configure(text="Nothing selected")
        self.progress.configure(value=0)
        self.status.configure(text="Cleared.")

    def _on_close(self) -> None:
        """Close silently.

        No confirmation dialog: the queue is on disk and every finished row is
        already flushed to CSV, so closing loses nothing and a prompt would
        just be noise. Work in progress resumes on the next launch.
        """
        if self._closing:
            return
        self._closing = True

        # Stop the UI pump first, or its next tick fires against a destroyed
        # widget tree and Tk prints a traceback after the window has gone.
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:                         # noqa: BLE001
                pass
            self._after_id = None

        self._save_window_state()

        try:
            self.pipeline.stop()
        except Exception:                             # noqa: BLE001
            pass
        try:
            self.pipeline.csv.close()
        except Exception:                             # noqa: BLE001
            pass
        try:
            self.root.destroy()
        except Exception:                             # noqa: BLE001
            pass


def asset_path(name: str) -> Optional[Path]:
    """Find a bundled asset, both when run from source and from the .exe.

    PyInstaller unpacks --add-data files into sys._MEIPASS at runtime.
    """
    roots = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        roots += [Path(meipass) / "assets", Path(meipass)]
    here = Path(__file__).resolve().parent
    roots += [here.parent / "assets", here / "assets"]
    for r in roots:
        p = r / name
        if p.exists():
            return p
    return None


def set_app_icon(root) -> None:
    """Window + taskbar icon. Silently ignored if the asset is missing."""
    ico = asset_path("icon.ico")
    if ico is not None:
        try:
            root.iconbitmap(default=str(ico))
            return
        except Exception:                             # noqa: BLE001
            pass                                      # not Windows, try PNG
    png = asset_path("icon.png")
    if png is not None:
        try:
            from PIL import Image, ImageTk
            with Image.open(png) as im:
                img = ImageTk.PhotoImage(im.copy())
            root._app_icon = img                      # keep a reference alive
            root.iconphoto(True, img)
        except Exception:                             # noqa: BLE001
            pass


def main() -> None:
    # Must happen before the first Tk() call or Windows bitmap-stretches the
    # whole window and every glyph looks soft.
    enable_hidpi()
    settings = Settings.load()
    root = TkinterDnD.Tk() if _DND else tk.Tk()
    set_app_icon(root)

    # Every launch requires a fresh email + admin-relayed OTP before the OCR
    # tool becomes reachable - see login_gui.LoginGate / otp_auth.py.
    from .login_gui import LoginGate

    app_holder: dict = {}

    def _launch_app() -> None:
        app_holder["app"] = App(root, settings)

    LoginGate(root, on_success=_launch_app)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        app = app_holder.get("app")
        if app is not None:
            try:
                app._on_close()
            except Exception:                         # noqa: BLE001
                pass


if __name__ == "__main__":
    main()
