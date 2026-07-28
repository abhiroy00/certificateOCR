#!/usr/bin/env python3
"""One-command GUI check - no API key, no real scans needed.

    python demo_gui.py

It generates 9 fake share-certificate images in a temp folder, forces the
'mock' engine, points the app at a throwaway workdir (so your real queue.db
and CSVs are untouched), pre-selects the demo folder and opens the window.

Just press Extract and you will see the whole flow: thumbnails, live rows
streaming into the table, the record badge, the progress bar with files/sec
and ETA, flagged rows highlighted in amber, and real CSV shards on disk.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def make_demo_images(folder: Path, n: int = 9) -> None:
    """Draw certificate-looking images so the thumbnail strip has content."""
    from PIL import Image, ImageDraw

    folder.mkdir(parents=True, exist_ok=True)
    companies = ["KABRA DRUGS LIMITED", "FABWORTH (INDIA) LTD.", "RELIANCE MILLS LTD."]
    holders = ["YUNUSBHAI YAJUBBHAI PATEL", "D P PANJWANI", "SHREYA R MEHTA"]
    tints = [(253, 245, 230), (240, 248, 255), (245, 255, 245)]

    for i in range(n):
        img = Image.new("RGB", (900, 620), tints[i % 3])
        d = ImageDraw.Draw(img)
        d.rectangle([14, 14, 886, 606], outline=(120, 40, 40), width=6)
        d.rectangle([28, 28, 872, 592], outline=(160, 90, 90), width=2)
        d.text((300, 60), "SHARE CERTIFICATE", fill=(90, 20, 20))
        d.text((60, 130), companies[i % 3], fill=(20, 20, 20))
        d.text((60, 170), "Folio No. %d" % (8866 + i), fill=(30, 30, 30))
        d.text((60, 240), "Certificate No. %d" % (31013 + i), fill=(30, 30, 30))
        d.text((60, 280), "Registered Holder: %s" % holders[i % 3], fill=(30, 30, 30))
        d.text((60, 320), "No. of Shares: ONE HUNDRED (100)", fill=(30, 30, 30))
        d.text((60, 360), "Distinctive Nos. %d to %d" % (4193501 + i * 100, 4193600 + i * 100),
               fill=(30, 30, 30))
        d.text((60, 400), "Dated this 15TH DAY OF MAY 1993", fill=(30, 30, 30))
        d.text((60, 200), "Regd. Folio No. %d" % (8866 + i), fill=(30, 30, 30))
        d.text((60, 470), "Rs. 10/- each fully paid up  -  Equity", fill=(60, 60, 60))
        img.save(folder / ("WhatsApp Image 2026-06-09 at 6.31.0%d.jpeg" % i), quality=88)


def main() -> int:
    try:
        import tkinter as tk
    except Exception:
        print("Tkinter is not installed. Run `python check_env.py` for the fix.")
        return 1

    from share_ocr.config import Settings
    from share_ocr.gui import App, _DND
    from share_ocr.theme import enable_hidpi

    # Same DPI fix the real entry point uses - before any Tk() call.
    enable_hidpi()

    tmp = Path(tempfile.mkdtemp(prefix="share_ocr_demo_"))
    demo_dir = tmp / "certificates"
    make_demo_images(demo_dir)

    s = Settings()
    s.workdir = tmp / "home"      # throwaway - your real data is untouched
    s.engine = "mock"             # no API key required
    s.workers = 4
    s.csv_flush_rows = 1          # flush every row so the CSV updates instantly
    s.ensure_dirs()

    print("Demo images : %s" % demo_dir)
    print("Demo workdir: %s" % s.workdir)
    print("Press 'Extract' in the window that just opened.")

    if _DND:
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()

    app = App(root, s)
    app.selected_paths = [str(demo_dir)]
    app._update_selection()
    app.status.configure(
        text="DEMO MODE (mock engine, temp workdir) - press Extract to see the flow.")
    try:
        root.mainloop()
    except KeyboardInterrupt:
        # Ctrl+C in the console should not dump a traceback at the user.
        try:
            app._on_close()
        except Exception:                         # noqa: BLE001
            pass
    print("Demo closed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
