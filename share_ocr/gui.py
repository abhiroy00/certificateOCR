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
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

from .config import APP_NAME, APP_VERSION, SUPPORTED_EXT, Settings
from .extractor import make_thumbnail
from .pipeline import Pipeline, Stats, scan_paths
from .secrets import (delete_api_key, describe, looks_like_openai_key, mask,
                      resolve, save_api_key, test_key)
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
    ("remarks", "Remarks", 160),                   # add-on #11
    ("flags", "Flags", 200),
]

MAX_TABLE_ROWS = 2000       # keep the Treeview light
MAX_THUMBS = 12


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
        self.row_count = 0
        self._thumb_imgs: List[tk.PhotoImage] = []

        # Fonts + ttk styles first: every widget below reads self.t for sizes.
        self.t = apply_theme(root)
        px = self.t.px

        root.title(f"{APP_NAME} v{APP_VERSION}")
        root.configure(bg=BG)
        self._closing = False
        self._after_id = None

        self._build_toolbar()
        self._build_dropzone()
        self._build_results()
        self._build_statusbar()
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

        # API key button + status chip. The customer gets the .exe, not the
        # source, so this is the only place a key can be entered.
        RoundedButton(inner, text="API key", variant="secondary", theme=t,
                      command=self.open_api_key_dialog).pack(
            side="right", padx=(0, px(8)))

        chip = tk.Frame(inner, bg=CARD, cursor="hand2")
        chip.pack(side="right", padx=(0, px(10)))
        self.key_dot = tk.Label(chip, text="\u25cf", bg=CARD, fg=MUTED,
                                font=t.f(0))
        self.key_dot.pack(side="left", padx=(0, px(5)))
        self.key_label = tk.Label(chip, text="API key", bg=CARD, fg=INK_SOFT,
                                  font=t.f(-1), cursor="hand2")
        self.key_label.pack(side="left")
        for w in (chip, self.key_dot, self.key_label):
            w.bind("<Button-1>", lambda e: self.open_api_key_dialog())

        # Settings sit on ONE line: label beside field. The old "LABEL above
        # field" stack cost a whole extra row of height and read like a form.
        cfg = ttk.Frame(inner, style="Card.TFrame")
        cfg.pack(side="right", padx=(0, px(16)))

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
        """Update the toolbar chip: green = usable key, grey = none needed."""
        info = describe(self.s)
        if self.s.engine != "openai":
            self.key_dot.configure(fg=MUTED)
            self.key_label.configure(text="API key not needed")
        elif info["configured"]:
            self.key_dot.configure(fg=GREEN)
            self.key_label.configure(
                text="Key %s  (%s)" % (info["masked"], info["source_label"]))
        else:
            self.key_dot.configure(fg=RED)
            self.key_label.configure(text="Add OpenAI API key")

    def open_api_key_dialog(self) -> None:
        """Enter / test / remove the OpenAI key. Never echoed to the screen."""
        t, px = self.t, self.t.px
        info = describe(self.s)
        win = tk.Toplevel(self.root)
        win.title("OpenAI API key")
        win.configure(bg=BG)
        win.transient(self.root)
        win.resizable(False, False)

        body = ttk.Frame(win, style="Card.TFrame", padding=(px(16), px(14)))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="OpenAI API key", style="Section.TLabel").pack(anchor="w")
        ttk.Label(body, style="Muted.TLabel", wraplength=px(420), justify="left",
                  text=("Stored in your operating system's credential manager "
                        "when available, otherwise in an obfuscated file that "
                        "only your user account can read. It is never written "
                        "to the CSV, the log or the queue database.")
                  ).pack(anchor="w", pady=(px(4), px(10)))

        key_var = tk.StringVar(value="")
        entry = ttk.Entry(body, textvariable=key_var, width=52, show="\u2022",
                          font=t.fm(-1))
        entry.pack(fill="x")
        entry.focus_set()

        show_var = tk.IntVar(value=0)

        def toggle_show():
            entry.configure(show="" if show_var.get() else "\u2022")

        ttk.Checkbutton(body, text="Show key", variable=show_var,
                        command=toggle_show).pack(anchor="w", pady=(px(6), 0))

        self._dlg_status = ttk.Label(
            body, style="Muted.TLabel", wraplength=px(420), justify="left",
            text=("Current: %s (%s)" % (info["masked"], info["source_label"]))
            if info["configured"] else "No key saved yet.")
        self._dlg_status.pack(anchor="w", pady=(px(10), px(4)))

        row = ttk.Frame(body, style="Card.TFrame")
        row.pack(fill="x", pady=(px(8), 0))

        def set_status(msg: str) -> None:
            try:
                self._dlg_status.configure(text=msg)
            except Exception:                         # noqa: BLE001
                pass

        def do_save():
            key = key_var.get().strip()
            if not key:
                set_status("Paste a key first.")
                return
            if not looks_like_openai_key(key):
                set_status("That does not look like an OpenAI key "
                           "(they start with 'sk-'). Saving anyway.")
            where = save_api_key(self.s, key)
            key_var.set("")
            set_status("Saved to %s as %s." % (where, mask(key)))
            self._refresh_key_status()

        def do_test():
            candidate = key_var.get().strip() or None
            set_status("Testing...")
            win.update_idletasks()
            ok, msg = test_key(self.s, candidate)
            set_status(("OK - " if ok else "Failed - ") + msg)

        def do_remove():
            removed = delete_api_key(self.s)
            set_status("Removed from: %s" % (", ".join(removed) or "nothing"))
            self._refresh_key_status()

        RoundedButton(row, text="Save", variant="primary", theme=t,
                      command=do_save).pack(side="left")
        RoundedButton(row, text="Test", variant="secondary", theme=t,
                      command=do_test).pack(side="left", padx=(px(8), 0))
        RoundedButton(row, text="Remove", variant="danger", theme=t,
                      command=do_remove).pack(side="left", padx=(px(8), 0))
        RoundedButton(row, text="Close", variant="ghost", theme=t,
                      command=win.destroy).pack(side="right")

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
        RoundedButton(btns, text="Retry failed", variant="secondary", theme=t,
                   command=self.retry_failed).pack(side="left")
        self.sel_label = ttk.Label(btns, text="Nothing selected", style="Muted.TLabel")
        self.sel_label.pack(side="right", pady=(px(6), 0))

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

        self.filter_var = tk.StringVar(value="all")
        ttk.Radiobutton(head, text="Needs review", value="review",
                        variable=self.filter_var,
                        command=self._reload_table).pack(side="right")
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
        self.thumb_bar = tk.Frame(box, bg=CARD)

        wrap = ttk.Frame(box, style="Card.TFrame")
        wrap.pack(fill="both", expand=True, pady=(px(8), 0))
        self._table_wrap = wrap
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in COLUMNS],
                                 show="headings", selectmode="extended")
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

        foot = ttk.Frame(box, style="Card.TFrame")
        foot.pack(fill="x", pady=(px(9), 0))
        RoundedButton(foot, text="Download CSV", variant="success", theme=t,
                      min_width=px(150),
                      command=self.download_csv).pack(side="left")
        RoundedButton(foot, text="Open CSV folder", variant="secondary", theme=t,
                      command=self._open_output).pack(side="left", padx=px(8))
        RoundedButton(foot, text="Clear all", variant="danger", theme=t,
                   command=self.clear_all).pack(side="right")
        RoundedButton(foot, text="Delete selected", variant="danger", theme=t,
                   command=self.delete_selected).pack(side="right", padx=(0, px(8)))

    # -------------------------------------------------------- statusbar --
    def _build_statusbar(self) -> None:
        t, px = self.t, self.t.px
        panel = card(self.root)
        panel.pack(fill="x", padx=px(12), pady=(px(8), px(10)))
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

    def _update_selection(self) -> None:
        if not self.selected_paths:
            self.sel_label.configure(text="Nothing selected")
            return
        first = self.selected_paths[0]
        extra = f" (+{len(self.selected_paths) - 1} more)" if len(self.selected_paths) > 1 else ""
        self.sel_label.configure(text=f"Selected: {os.path.basename(first) or first}{extra}")
        self._show_thumbs(self.selected_paths)

    def _show_thumbs(self, paths: List[str]) -> None:
        for w in self.thumb_bar.winfo_children():
            w.destroy()
        self._thumb_imgs.clear()
        if not paths:
            self.thumb_bar.pack_forget()
            return
        # Show the strip only when there is something in it.
        self.thumb_bar.pack(fill="x", pady=(self.t.px(8), 0),
                            before=self._table_wrap)
        files: List[str] = []
        for p in paths:
            if os.path.isdir(p):
                for fp, _, _ in scan_paths(p):
                    files.append(fp)
                    if len(files) >= MAX_THUMBS:
                        break
            else:
                files.append(p)
            if len(files) >= MAX_THUMBS:
                break
        for i, f in enumerate(files[:MAX_THUMBS], start=1):
            if f.lower().endswith(".pdf"):
                continue
            dest = self.s.thumb_dir / f"t{i}.jpg"
            # Render the thumbnail at the physical pixel size so it is not
            # upscaled (another source of the "blurry" look on hi-dpi).
            tp = make_thumbnail(f, dest, self.t.px(76))
            holder = tk.Frame(self.thumb_bar, bg=CARD)
            holder.pack(side="left", padx=4)
            if tp:
                try:
                    from PIL import Image, ImageTk
                    img = ImageTk.PhotoImage(Image.open(tp))
                    self._thumb_imgs.append(img)
                    tk.Label(holder, image=img, bg=CARD).pack()
                except Exception:                    # noqa: BLE001
                    tk.Label(holder, text="IMG", bg=BLUE_TINT, fg=MUTED,
                             font=self.t.f(-2), width=10, height=4).pack()
            tk.Label(holder, text=f"#{i}", bg=BLUE, fg="white",
                     font=self.t.f(-3, "bold"), pady=self.t.px(2)).pack(fill="x")

    # ----------------------------------------------------------- run ----
    def start_extract(self) -> None:
        if not self.selected_paths:
            messagebox.showinfo(APP_NAME, "Select files or a folder first.")
            return
        # No key? Open the key dialog instead of a dead-end "this will fail"
        # warning. The key may live in the OS keyring or the settings file,
        # so ask secrets.resolve() rather than only the environment.
        if self.s.engine == "openai" and not resolve(self.s)[0]:
            self.status.configure(text="Add an OpenAI API key to continue.")
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

    def retry_failed(self) -> None:
        ids = self.pipeline.q.failed_or_dead_ids()
        if not ids:
            messagebox.showinfo(APP_NAME, "No failed files to retry.")
            return
        if self.s.engine == "openai" and not resolve(self.s)[0]:
            self.status.configure(text="Add an OpenAI API key to continue.")
            self.open_api_key_dialog()
            return
        n = self.pipeline.q.retry_failed()
        self.btn_extract.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_pause.configure(state="normal")
        self.status.configure(text=f"Retrying {n:,} failed file(s)…")
        threading.Thread(target=self._run_retry, args=(ids,), daemon=True).start()

    def _run_retry(self, ids: List[int]) -> None:
        try:
            self.pipeline.start(scope_ids=ids)
            self.pipeline.join()
            self._enqueue_log("Retry finished.")
        except Exception as e:                        # noqa: BLE001
            self._enqueue_log(f"FATAL: {e}")
        finally:
            self.ui_queue.put(("finished", None))

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
            elif kind == "finished":
                self.btn_extract.configure(state="normal")
                self.btn_stop.configure(state="disabled")
                self.btn_pause.configure(state="disabled", text="Pause")
        if not self._closing:
            self._after_id = self.root.after(150, self._drain_ui_queue)

    def _insert_row(self, row: Dict) -> None:
        if self.filter_var.get() == "review" and not row.get("validation_flags"):
            self.row_count += 1
            self.badge.configure(text=f"{self.row_count:,} records")
            return
        self.row_count += 1
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
                row.get("latest_share_holder_name", ""), row.get("remarks", ""),
                row.get("validation_flags", "")),
            tags=tags)
        children = self.tree.get_children()
        if len(children) > MAX_TABLE_ROWS:
            self.tree.delete(*children[MAX_TABLE_ROWS:])
        self.badge.configure(text=f"{self.row_count:,} record(s)")

    def _update_progress(self, st: Stats) -> None:
        total = max(1, st.total)
        pct = 100.0 * (st.done + st.failed) / total
        self.progress.configure(value=min(100.0, pct))
        self.status.configure(
            text=(f"{st.done:,}/{st.total:,} files · {st.rows:,} rows · "
                  f"{st.failed:,} failed · {st.rate:.1f} files/s · "
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
        import json
        rows = self.pipeline.q.recent_rows(400)
        shown = 0
        for r in reversed(rows):
            rec = json.loads(r["payload"])
            if self.filter_var.get() == "review" and not r["flags"]:
                continue
            rec["source_file"] = r["name"]
            rec["validation_flags"] = r["flags"]
            rec["row_id"] = r["row_id"]
            self.row_count = shown
            self._insert_row(rec)
            shown = self.row_count

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

    def _open_output(self) -> None:
        path = str(self.s.csv_dir)
        try:
            if os.name == "nt":
                os.startfile(path)                    # type: ignore[attr-defined]
            elif os.uname().sysname == "Darwin":
                os.system(f'open "{path}"')
            else:
                webbrowser.open(f"file://{path}")
        except Exception:                             # noqa: BLE001
            messagebox.showinfo(APP_NAME, path)

    def _open_source_file(self, _event) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        name = self.tree.item(sel[0], "values")[1]
        self.status.configure(text=f"Row source: {name}")

    def delete_selected(self) -> None:
        """Delete the checked/highlighted row(s): from the table, the CSV
        shards and the queue database. The source file goes back to
        'pending' so a later Extract can redo it if it's still needed."""
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Select one or more rows first.")
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
        for w in self.thumb_bar.winfo_children():
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
    app = App(root, settings)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        try:
            app._on_close()
        except Exception:                             # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
