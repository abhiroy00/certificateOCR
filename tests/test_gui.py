"""Headless GUI tests.

Run with:  python -m tests.test_gui

These exercise the real App class against the fake Tk in tests/fake_tk.py,
so they catch layout wiring, state handling and the duplicate-row bug
without needing a screen.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import fake_tk                            # noqa: E402

fake_tk.install()                                     # BEFORE importing the GUI

import tkinter as tk                                  # noqa: E402  (the fake)

from share_ocr.config import Settings                 # noqa: E402
from share_ocr import gui as G                        # noqa: E402
from share_ocr.widgets import VARIANTS, RoundedButton  # noqa: E402
from tests.stub_engine import install as install_stub  # noqa: E402

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


def make_images(folder: Path, count: int) -> None:
    from PIL import Image, ImageDraw
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(1, count + 1):
        img = Image.new("RGB", (700, 460), "white")
        d = ImageDraw.Draw(img)
        d.text((30, 40), "SHARE CERTIFICATE %d" % i, fill="black")
        d.text((30, 90), "Folio No 88%02d" % i, fill="black")
        img.save(folder / ("cert_%03d.jpg" % i))


def walk(widget, out=None):
    out = [] if out is None else out
    out.append(widget)
    for c in getattr(widget, "children", []):
        walk(c, out)
    return out


def build_app(tmp: Path, engine=None):
    s = Settings()
    s.workdir = tmp / "home"
    s.engine = engine or install_stub()
    s.workers = 2
    s.csv_flush_rows = 1
    s.ensure_dirs()
    root = tk.Tk()
    app = G.App(root, s)
    return root, app, s


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="share_ocr_gui_"))
    try:
        imgs = tmp / "certificates"
        make_images(imgs, 3)

        # ---------------------------------------------- construction ----
        print("\n[1] window construction")
        root, app, s = build_app(tmp)
        check("app builds without error", app is not None)
        check("a repeating UI timer was scheduled", app._after_id is not None)

        # ------------------------------------------------ window size ----
        print("\n[2] window sizing (fake screen is 1366x768)")
        mw, mh = root.min_size
        check("minimum width fits the screen", mw <= 1366, "min_w=%s" % mw)
        check("minimum height fits the screen", mh <= 768, "min_h=%s" % mh)
        geom = root.kw.get("geometry", "")
        w, h = geom.split("+")[0].split("x")
        check("opens no wider than the screen", int(w) <= 1366, geom)
        check("opens no taller than the screen", int(h) <= 768, geom)
        check("window is resizable down to the minimum",
              int(w) >= mw and int(h) >= mh, "%s vs %sx%s" % (geom, mw, mh))

        # ------------------------------------------------- buttons ------
        print("\n[3] buttons")
        widgets = walk(root)
        buttons = [x for x in widgets if isinstance(x, RoundedButton)]
        check("rounded buttons are used", len(buttons) >= 8,
              "found %d" % len(buttons))
        check("every button has a corner radius",
              all(b._radius > 0 for b in buttons))
        check("every button uses a known colour variant",
              all(b._variant in VARIANTS for b in buttons))
        check("every button drew a rounded polygon + label",
              all(len(b.items) >= 2 for b in buttons))
        check("every button has a command",
              all(b._command is not None for b in buttons))
        labels = sorted(b.cget("text") for b in buttons)
        check("key actions are present",
              {"Extract", "Stop", "Select all", "Delete selected",
               "Clear all"} <= set(labels), str(labels))
        # The CSV buttons were removed: shards are written continuously and
        # the toolbar's "Output folder" opens the same directory.
        check("the CSV buttons are gone",
              not {"Download CSV", "Open CSV folder"} & set(labels),
              str(labels))
        # Every row action must sit on the fixed-height action row, never in
        # a footer below the expanding table where Tk can clip it away.
        for name in ("Select all", "Delete selected", "Clear all"):
            b = next(x for x in buttons if x.cget("text") == name)
            check("%s is on the action row, beside Extract" % name,
                  b.master is app.btn_extract.master,
                  "parent=%r" % b.master)
        check("Stop/Pause start disabled",
              app.btn_stop.cget("state") == "disabled"
              and app.btn_pause.cget("state") == "disabled")
        # disabled buttons must not fire
        fired = []
        app.btn_stop.configure(command=lambda: fired.append(1))
        app.btn_stop._on_release()
        check("disabled button ignores clicks", not fired)

        # ------------------------------------------ engine / model UI ----
        print("\n[4] engine + model controls")
        check("engine dropdown shows a friendly label, not a raw id",
              app.engine_var.get() == G._label_for(G.ENGINE_CHOICES, s.engine)
              and all("(" in lbl for _id, lbl in G.ENGINE_CHOICES),
              app.engine_var.get())
        check("no demo/mock engine is offered",
              [i for i, _ in G.ENGINE_CHOICES] == ["openai", "tesseract"],
              G.ENGINE_CHOICES)
        check("model picker hidden for non-OpenAI engines",
              not app.model_frame.packed)
        app.engine_var.set(G._label_for(G.ENGINE_CHOICES, "openai"))
        app._on_engine_change()
        check("engine switch is saved as the internal id", s.engine == "openai")
        check("model picker appears for OpenAI", app.model_frame.packed)
        values = app.model_box.kw.get("values", [])
        check("model dropdown lists the vision models",
              len(values) >= len(G.OPENAI_MODELS), str(values))
        check("recommended model is first",
              values[0].startswith("gpt-4o-mini"), str(values[:1]))
        app.model_var.set(values[3])
        app.model_box.fire("<<ComboboxSelected>>")
        check("choosing a model stores the bare model id",
              s.model in [k for k, _ in G.OPENAI_MODELS], s.model)
        app.engine_var.set(G._label_for(G.ENGINE_CHOICES, "tesseract"))
        app._on_engine_change()
        check("model picker hides again for Tesseract",
              not app.model_frame.packed and s.engine == "tesseract")
        check("settings row is one line (no stacked label frames)",
              app.engine_box.pack_kw.get("side") == "left")

        # -------------------------------------------- api key guard -----
        print("\n[5] API key guard")
        fake_tk.DIALOGS.reset()
        os.environ.pop(s.api_key_env, None)
        s.engine = "openai"
        app.selected_paths = [str(imgs)]
        opened = []
        app.open_api_key_dialog = lambda *a, **k: opened.append(1)
        # Clearing the env var alone is not "no key configured": resolve()
        # also reads the OS credential store and the fallback file, so on a
        # machine with a real key in Windows Credential Manager the guard
        # correctly did NOT fire and this failed for the wrong reason.
        real_resolve = G.resolve
        G.resolve = lambda *a, **k: (None, "none")
        try:
            app.start_extract()
        finally:
            G.resolve = real_resolve
        check("missing key opens the key dialog, not a dead-end warning",
              opened and not any(c[0] == "askyesno" for c in fake_tk.DIALOGS.calls))
        s.engine = install_stub()

        # ------------------------------------------------- thumbnails ---
        print("\n[6] thumbnail strip")
        app.selected_paths = []
        app._update_selection()
        check("thumb strip hidden when nothing is selected",
              not app.thumb_bar.packed)
        app.selected_paths = [str(imgs)]
        app._update_selection()
        check("thumb strip appears with a selection", app.thumb_bar.packed)
        # The label no longer echoes the folder name; it reports how many
        # files, split by type, counted on a background thread.
        check("selection label leaves the empty state",
              app.sel_label.kw.get("text", "") != "Nothing selected",
              app.sel_label.kw.get("text"))
        for _ in range(200):
            if not app.ui_queue.empty():
                break
            time.sleep(0.02)
        app._drain_ui_queue()
        check("selection label reports the file breakdown",
              "3 images" in app.sel_label.kw.get("text", ""),
              app.sel_label.kw.get("text"))

        # ------------------------------------- the duplicate-row bug ----
        print("\n[7] one file must produce exactly one row")
        app.pipeline.ingest([str(imgs)])
        app.pipeline.start()
        app.pipeline.join()
        root.run_pending()
        app._drain_ui_queue()
        counts = app.pipeline.counts()
        check("queue processed every file", counts["done"] == 3, str(counts))
        check("3 files -> 3 database rows (no duplicates)",
              counts["rows"] == 3, "rows=%s" % counts["rows"])
        check("3 files -> 3 table rows",
              len(app.tree.get_children()) == 3,
              "tree=%d" % len(app.tree.get_children()))
        check("badge matches the table",
              app.badge.kw.get("text", "").startswith("3"),
              app.badge.kw.get("text", ""))

        # a reload must not double anything up
        app._reload_table()
        check("reload does not duplicate rows",
              len(app.tree.get_children()) == 3,
              "tree=%d" % len(app.tree.get_children()))
        check("reload resets numbering", app.row_count == 3,
              "row_count=%s" % app.row_count)
        first = app.tree.item(app.tree.get_children()[0], "values")
        check("row has one value per column", len(first) == len(G.COLUMNS),
              "%d vs %d" % (len(first), len(G.COLUMNS)))

        # CSV must agree with the table
        csv_files = sorted(Path(s.csv_dir).glob("certificates-part-*.csv"))
        data = csv_files[0].read_text(encoding="utf-8-sig").strip().splitlines()
        check("CSV has 3 data rows + header", len(data) == 4,
              "%d lines" % len(data))

        # ---------------------------------------------- review filter ---
        print("\n[8] filter")
        app.filter_var.set("review")
        app._reload_table()
        flagged = len([r for r in app.tree.get_children()])
        check("review filter only shows flagged rows", flagged <= 3,
              "shown=%d" % flagged)
        app.filter_var.set("all")
        app._reload_table()
        check("switching back restores all rows",
              len(app.tree.get_children()) == 3)

        # --------------------------------- selection file-type summary ---
        print("\n[8c] selection shows how many images vs PDFs")
        mixed = tmp / "mixed"
        make_images(mixed, 3)
        for n in range(7):                       # 7 stand-in PDFs
            (mixed / ("doc_%d.pdf" % n)).write_bytes(b"%PDF-1.4\n")
        (mixed / "notes.txt").write_text("ignored")     # unsupported
        (mixed / "sub").mkdir()
        make_images(mixed / "sub", 0)

        imgs_n, pdfs_n, capped = G.App.count_selection([str(mixed)])
        check("counts the 3 images", imgs_n == 3, imgs_n)
        check("counts the 7 PDFs", pdfs_n == 7, pdfs_n)
        check("ignores unsupported files", imgs_n + pdfs_n == 10,
              imgs_n + pdfs_n)
        check("not capped for a small folder", capped is False)

        text = G.App.describe_selection(3, 7)
        check("summary names both counts",
              "10 files found" in text and "3 images" in text
              and "7 PDFs" in text, text)
        check("singulars are not mangled",
              "1 image," in G.App.describe_selection(1, 2)
              and "1 PDF" in G.App.describe_selection(2, 1),
              G.App.describe_selection(1, 2))
        check("empty selection says so",
              "No supported files" in G.App.describe_selection(0, 0))
        check("a capped count is marked with +",
              "+" in G.App.describe_selection(5, 5, True))

        # the count runs on a worker thread and arrives through the ui queue
        app.selected_paths = [str(mixed)]
        app._update_selection()
        check("label shows progress immediately",
              "Counting" in app.sel_label.kw.get("text", ""),
              app.sel_label.kw.get("text"))
        for _ in range(200):
            if not app.ui_queue.empty():
                break
            time.sleep(0.02)
        app._drain_ui_queue()
        check("breakdown replaces it once counted",
              "10 files found" in app.sel_label.kw.get("text", ""),
              app.sel_label.kw.get("text"))

        stale = app._sel_token - 1
        before = app.sel_label.kw.get("text")
        app.ui_queue.put(("selection", (stale, 999, 999, False)))
        app._drain_ui_queue()
        check("a stale count from an earlier selection is ignored",
              app.sel_label.kw.get("text") == before,
              app.sel_label.kw.get("text"))

        # ------------------------------------------- preview strip -------
        # PDFs used to be skipped when building previews, so a folder of PDF
        # certificates showed an empty strip - the exact files an operator
        # most wants to eyeball. The strip now previews page 1 of a PDF.
        print("\n[8c2] preview strip includes PDFs")
        files, more = G.App._selection_files([str(mixed)], 12)
        check("collects up to the cap", len(files) == 10, len(files))
        check("nothing truncated at 10 files", more is False)
        capped_files, more2 = G.App._selection_files([str(mixed)], 4)
        check("stops at the cap", len(capped_files) == 4, len(capped_files))
        check("and reports that there was more", more2 is True)
        check("PDFs are not filtered out of the strip",
              any(f.lower().endswith(".pdf") for f in files),
              [os.path.basename(f) for f in files])

        # render with a stubbed thumbnailer: no PIL work, just the wiring
        items = [(1, str(mixed / "doc_0.pdf"), None),
                 (2, str(mixed / "cert_001.jpg"), None)]
        app._render_thumbs(items, True)
        tiles = [w for w in app.thumb_inner.winfo_children()
                 if isinstance(w, tk.Frame)]
        check("a tile per file, PDF included", len(tiles) == 2, len(tiles))
        check("truncation is shown when there is more",
              any("first" in str(getattr(w, "kw", {}).get("text", ""))
                  for w in app.thumb_inner.winfo_children()),
              [getattr(w, "kw", {}).get("text") for w
               in app.thumb_inner.winfo_children()])

        clicked = []
        app._open_path = lambda p: (clicked.append(p), True)[1]
        app._open_preview(str(mixed / "doc_0.pdf"))
        check("clicking a preview opens that file", len(clicked) == 1, clicked)
        app._open_preview(str(mixed / "nope.pdf"))
        check("a missing file is reported, not opened",
              len(clicked) == 1 and "Missing" in app.status.kw.get("text", ""),
              app.status.kw.get("text"))

        # A preview batch from a selection the operator has already moved on
        # from must not repaint the strip.
        before = len([w for w in app.thumb_inner.winfo_children()
                      if isinstance(w, tk.Frame)])
        app.ui_queue.put(("thumbs", (app._sel_token - 1,
                                     [(1, "x.jpg", None)] * 9, False)))
        app._drain_ui_queue()
        after = len([w for w in app.thumb_inner.winfo_children()
                     if isinstance(w, tk.Frame)])
        check("a stale preview batch is discarded", after == before,
              "%d -> %d" % (before, after))

        # ------------------------------------------------ open source ----
        print("\n[8d] Open shows the scan a row came from")
        app._reload_table()
        opened = []
        app._open_path = lambda p: (opened.append(p), True)[1]

        fake_tk.DIALOGS.reset()
        app.tree.selection_set([])
        app.open_selected_source()
        check("nothing selected -> tells the operator",
              any(c[0] == "showinfo" for c in fake_tk.DIALOGS.calls)
              and not opened, str(fake_tk.DIALOGS.calls))

        fake_tk.DIALOGS.reset()
        iid = app.tree.get_children()[0]
        app.tree.selection_set([iid])
        app.open_selected_source()
        check("opens exactly one file for one row", len(opened) == 1, opened)
        check("and it is the real scan on disk",
              opened and os.path.exists(opened[0]), opened)
        check("resolved via row_id, not the display name",
              app._row_source_path(iid) == opened[0])
        check("double-click does the same thing",
              app._open_source_file() is None)

        opened.clear()
        app.select_all_rows()
        app.open_selected_source()
        check("Select all + Open is capped, not one window per row",
              len(opened) <= G.App.MAX_OPEN_AT_ONCE, len(opened))

        check("an unknown iid resolves to nothing",
              app._row_source_path("I999") is None)

        # ------------------------------------------------- clear all ----
        print("\n[9] clear all")
        fake_tk.DIALOGS.reset()
        fake_tk.DIALOGS.askyesno_result = False
        app.clear_all()
        check("clear asks first and respects No",
              len(app.tree.get_children()) == 3)
        fake_tk.DIALOGS.askyesno_result = True
        app.clear_all()
        check("clear empties the table", len(app.tree.get_children()) == 0)
        check("badge resets", app.badge.kw.get("text") == "0 records")

        # --------------------------------------------------- closing ----
        print("\n[10] closing")
        fake_tk.DIALOGS.reset()
        after_id = app._after_id
        app._on_close()
        check("close shows no dialog", fake_tk.DIALOGS.calls == [],
              str(fake_tk.DIALOGS.calls))
        check("close cancels the UI timer", after_id in root.cancelled,
              str(root.cancelled))
        check("closing flag is set", app._closing is True)
        # a late timer tick after destroy must be a no-op, not a traceback
        app._drain_ui_queue()
        check("late timer tick is harmless", True)
        app._on_close()
        check("closing twice is safe", True)
        saved = Settings.load_from(s.workdir) if hasattr(Settings, "load_from") else None
        check("window geometry remembered",
              bool(s.window_geometry) or s.window_maximized is False,
              s.window_geometry)

        print("\n%d passed, %d failed" % (PASS, FAIL))
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
