# Share Certificate OCR — Tkinter desktop app (image → CSV, built for 30 lakh files)

A desktop OCR tool that reads scanned share certificates, extracts the required
fields with a vision LLM (or offline Tesseract), validates them, and streams the
result into **CSV** files.

The original script was fine for a few hundred images. This version is a rewrite
around a durable job queue so it can chew through **3,000,000 images** without
running out of RAM and without losing work if the machine reboots.

---

## 1. Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# PDF support needs poppler
#   Ubuntu : sudo apt install poppler-utils
#   Mac    : brew install poppler
#   Windows: download poppler and add /bin to PATH

# offline engine (optional)
#   Ubuntu : sudo apt install tesseract-ocr

export OPENAI_API_KEY="sk-..."     # Windows: setx OPENAI_API_KEY "sk-..."
```

## 2. Run the GUI

```bash
python run_gui.py
```

* Click the dashed box (or drag & drop) to pick files, or **Select folder…** for a whole tree
* Choose engine (`openai` / `tesseract`), model and worker count in the toolbar
* **Extract** → rows appear live in the table, CSV is written as it goes
* **Pause / Stop** anytime — progress is saved, press Extract again to resume
* **Download CSV** merges all shards into one file; **Clear All** wipes the queue
* Flagged rows are highlighted and also written to `certificates-needs-review.csv`

## 3. Run headless for the big job

```bash
# index + process a whole tree with 32 workers
python -m share_ocr.cli run /mnt/scans --workers 32 --engine openai --model gpt-4o-mini

# or split the two phases
python -m share_ocr.cli ingest /mnt/scans
python -m share_ocr.cli run --workers 48

python -m share_ocr.cli status      # counts + ETA
python -m share_ocr.cli failures    # why things failed
python -m share_ocr.cli retry       # requeue failures
python -m share_ocr.cli export /out/certificates.csv
```

**Horizontal scaling:** put `~/.share_ocr/queue.db` on shared storage (or set
`SHARE_OCR_HOME`) and run `cli run` on as many machines as you like — claims are
atomic, so no image is processed twice.

---

## Output

```
~/.share_ocr/
  queue.db                              # resumable job queue + all results
  csv/certificates-part-00001.csv       # 200k rows per shard
  csv/certificates-part-00002.csv
  csv/certificates-needs-review.csv     # only flagged rows
  share_ocr.log
```

Columns: `row_id, source_file, source_path, page_no, company_name, share_type,
folio_no, registered_folio_no, certificate_no, share_holder_name,
latest_share_holder_name, no_of_shares, no_of_shares_words,
face_value_per_share, remarks, distinctive_from, distinctive_to, date_of_issue,
validation_flags, engine, model, latency_ms, extracted_at`

CSV is written as UTF-8-BOM so Excel opens Indian names correctly.

### Billed add-on fields (#10, #11, #12)

These three are optional add-ons in the quotation, so they get extra handling —
they are extracted, shown in the GUI table, and audited:

| # | Field | CSV column | How it is captured |
|---|---|---|---|
| 10 | Face Value per Share | `face_value_per_share` | Per-share nominal value only. Reads `Rs. 10/- each`, `of Rs. 100 each fully paid up`, `FV Rs. 2`, `face value`, `nominal value`. If only a total paid-up amount is printed, it is divided by `no_of_shares`. |
| 11 | Share Type / Remarks | `share_type`, `remarks` | Type from the heading (Equity / Preference / Redeemable Preference / Ordinary / Bonus). Remarks collect endorsements, transfer stamps, DUPLICATE, LIEN, SPLIT, CONSOLIDATED, CANCELLED and hand-written notes. |
| 12 | Registered Folio No. | `registered_folio_no` | Read from the separate `Regd. Folio No.` box. If the certificate prints only one folio number anywhere, the same value fills both `folio_no` and `registered_folio_no`. |

**Audit flag.** Because the client is paying for these, a row whose add-on came
back empty is flagged `Add-on not captured: <field names>` and lands in
`certificates-needs-review.csv`. In the GUI those rows are tinted **blue**
(add-on gap only) versus **amber** (a core field actually failed validation),
so you can tell a billing gap from a bad scan at a glance.

There is also a guard against the classic mix-up: if `face_value_per_share`
equals `no_of_shares` on a large parcel, the row is flagged
`Face value looks like the share count`.

To switch the audit off, set `"flag_missing_addons": false` in
`~/.share_ocr/settings.json`. To drop an add-on from the deliverable entirely,
remove it from `ADDON_FIELDS` (audit) and `FIELDS` / `CSV_COLUMNS` (output) in
`share_ocr/config.py`.

---

## What makes it scale to 30 lakh images

| Problem at 3M files | Fix in this build |
|---|---|
| `glob` + list of paths eats GBs of RAM | `os.scandir` generator streamed into SQLite, 5k rows per transaction |
| pandas DataFrame of 3M rows | never built — rows stream straight to sharded CSV, constant memory |
| One crash = start over | SQLite WAL queue; every file has `pending/running/done/failed/dead` state, resume is automatic |
| Duplicate work on restart / multi-machine | atomic `BEGIN IMMEDIATE` batch claim |
| API rate limits kill the run | per-file attempt counter, exponential backoff on 429/timeout/5xx, dead-letter after 3 tries |
| Upload cost & latency | images downscaled to 1600px JPEG q80 before upload (~85% fewer bytes) |
| One 3M-row CSV is unopenable | shards of 200k rows + optional merge |
| GUI freezes | workers are background threads, UI updates via a queue, Treeview capped at 2000 visible rows |
| Stuck `running` rows after a kill | `requeue_stale()` returns anything claimed >15 min ago |

### Throughput maths

At ~1.2 s/image and 32 concurrent workers ≈ **26 images/s ≈ 2.3M/day**.
So 30 lakh finishes in roughly **1.5 days on one box**, or a few hours across
4–5 boxes sharing the queue. Cost is the real constraint — use `gpt-4o-mini`,
keep `max_image_px` at 1600, and consider the two-pass trick below.

### Cheap two-pass strategy (recommended for 3M)

1. `--engine tesseract` over everything (free, offline, ~50 img/s/box).
2. Requeue only rows with `validation_flags` and re-run with `--engine openai`.

That typically sends 15–25% of the corpus to the LLM instead of 100%.

---

## Tuning

`~/.share_ocr/settings.json` (written by the GUI, read by both):

| Key | Default | Notes |
|---|---|---|
| `workers` | 8 | 24–64 is the sweet spot for API work |
| `claim_batch` | 200 | bigger = less SQLite contention |
| `csv_shard_rows` | 200000 | rows per CSV part |
| `max_image_px` | 1600 | drop to 1200 to cut cost further |
| `max_attempts` | 3 | then the file goes to `dead` |
| `flag_missing_addons` | true | soft-flag empty face value / share type / registered folio |
| `pdf_dpi` | 200 | 300 for faint old scans |

## Display quality (why text looked blurry)

If the GUI text looked soft or fuzzy, that was **not** your screen — it was
Windows bitmap-stretching a non-DPI-aware window. `share_ocr/theme.py` fixes it:

- `enable_hidpi()` calls `SetProcessDpiAwareness(2)` (per-monitor aware) **before
  the first `Tk()` call**, so Windows renders the window at native resolution
  instead of scaling up a 96-DPI bitmap. It has to run first — that is why it
  lives in `main()` / `demo_gui.py` and not inside `App.__init__`.
- `tk scaling` is then set from the real display DPI, so point sizes map to real
  pixels at 125% / 150% / 175%.
- Fonts are resolved from a stack (`Segoe UI Variable Text` → `SF Pro Text` →
  `Inter` → `DejaVu Sans`). The old hard-coded `"Segoe UI"` silently fell back to
  an unhinted bitmap face on macOS/Linux, which is the other half of the blur.
- All paddings, row heights and thumbnails go through `Theme.px()`, and
  thumbnails are generated at physical pixel size so they are never upscaled.

If text is still soft on Windows, right-click `python.exe` → Properties →
Compatibility → Change high DPI settings → tick “Override high DPI scaling” and
set it to **Application**.

## Colour system

One palette in `theme.py`, no ad-hoc hex values in `gui.py`:

| Role | Colour | Used for |
|---|---|---|
| Primary | `#2563eb` | exactly one button on screen — **Extract** |
| Secondary | `#e9edf4` + dark ink | Select folder, Pause, Retry, Open folder |
| Success | `#059669` | Download CSV |
| Danger | `#dc2626` | Stop, Clear all |
| Muted text | `#5b6472` | ≥7:1 contrast on white (the old `#6b7280` at 9pt read as blurry) |

The table header is now light grey with muted caps instead of a saturated blue
bar, so the eye goes to the data. Row tints: **blue** = a billed add-on is
missing, **amber** = a core field failed validation, alternating **`#fafbfd`**
otherwise. A legend above the table explains both.

## Where to put the OpenAI API key

There is no need to touch the terminal. Open the GUI and click **API key** in
the top bar (the coloured dot next to it is red until a key is configured,
green once it is). Paste the key, pick where to store it, hit **Save**. Use
**Test connection** to confirm the key works before you queue three million
images.

The same thing from the command line, with hidden input so the key never lands
in your shell history or in `ps` output:

```bash
python -m share_ocr.cli key --set     # hidden prompt
python -m share_ocr.cli key --test    # verify it works
python -m share_ocr.cli key           # show status (masked)
python -m share_ocr.cli key --remove  # delete it from this machine
```

### The three places a key can come from

Read in this order, first hit wins:

| # | Source | When to use it | Safety |
|---|--------|----------------|--------|
| 1 | `OPENAI_API_KEY` environment variable | Servers, CI, the 30-lakh batch run | Never written to disk by us. Always overrides the two below. |
| 2 | OS credential store (`keyring`) | **Default for the desktop GUI** | Encrypted by Windows Credential Manager / macOS Keychain / Linux Secret Service against your login. |
| 3 | `<workdir>/credentials.json` | Machines with no keyring (bare Linux, portable installs) | Permissions `0600`, obfuscated with a machine-bound pad. |

Install the keyring backend to get option 2:

```bash
pip install keyring
```

Without it the GUI disables the "OS credential store" radio button and tells
you so, rather than silently downgrading.

### What is actually protected

Being straight about this matters more than sounding impressive:

- **The key is never written to `settings.json`.** The `Settings` dataclass
  holds only the *name* of the environment variable, not the value, so the
  config file you might email to support or commit to git cannot leak it.
- **The key is never logged** and never appears in the CSV, the queue database
  or a thumbnail. Error messages from the API are scrubbed before display.
- **On screen it is always masked**, e.g. `sk-proj…4f2A`.
- **Option 3 is obfuscation, not encryption.** It defeats shoulder-surfing, a
  careless `cat`, an accidental commit, and copying the file to another
  machine (the pad is derived from the host and user, so it will not decode
  elsewhere). It does **not** defeat malware already running as your Windows
  or macOS user — nothing a desktop app can do would. If that is in your
  threat model, use option 1 or 2.

### Operational advice for the client job

- Create a **dedicated key** for this project with a spend limit on it, so a
  leak costs a capped amount and can be revoked without breaking anything else.
- For the multi-machine batch run, set `OPENAI_API_KEY` in each machine's
  service environment rather than storing it on each desktop.
- Rotate the key when the engagement ends; `key --remove` on every operator
  machine clears the stored copy.

## Project layout

```
share_ocr/
  config.py      settings, field list, prompt, CSV columns
  db.py          SQLite queue (claims, retries, stats, results)
  extractor.py   OpenAI / Tesseract engines + validation
  csv_writer.py  sharded streaming CSV writer
  pipeline.py    scanner + thread pool + progress/ETA
  gui.py         Tkinter UI
  cli.py         headless bulk runner
run_gui.py
tests/test_pipeline.py
```

## Test

```bash
python -m tests.test_pipeline     # runs the pipeline end to end
```

## What changed in this build

### Rounded buttons + friendlier toolbar
All buttons are now drawn on a canvas with real rounded corners (9px radius,
anti-aliased), with hover, pressed, focus and disabled states. Colour meaning:
blue = main action, grey = secondary, green = download, red = destructive.

### Compact settings row
Engine, Workers and Model now sit on a single line with the label beside the
field instead of stacked above it. The Model dropdown is only shown when the
engine is OpenAI, because it means nothing for Tesseract or the demo engine.

Models offered, and when to pick each:

| Model | Use it for |
|---|---|
| gpt-4o-mini | fast and cheap - the default for bulk runs |
| gpt-4.1-mini | balanced accuracy and cost |
| gpt-4.1-nano | cheapest, clean scans only |
| gpt-4o | best on faded or handwritten certificates |
| gpt-4.1 | strongest reasoning on messy layouts |
| o4-mini | slow, good for re-checking flagged rows |

### The duplicate row bug (fixed)
A worker with nothing to do called claim() to probe the queue and threw the
result away. claim() marks rows as running, so that file sat claimed with
nobody processing it, requeue_stale() later returned it to pending, and a
second worker extracted the same file again. That produced two identical rows
in the table AND two identical rows in the CSV. It only looked size-dependent
because new rows are inserted at the top, so the older copy was below the fold
in a short window and visible once maximised. Workers now idle without
claiming.

### Tests

    python -m tests.test_all        # everything
    python -m tests.test_pipeline   # queue, retries, CSV sharding
    python -m tests.test_gui        # GUI, headless

The GUI suite runs without a display by swapping in a fake Tk
(tests/fake_tk.py) that records widget calls, so it can assert that one file
produces exactly one row, that the model picker hides for non-OpenAI engines,
that closing shows no dialog and cancels its timer, and that the window never
opens larger than the screen.

## No demo engine any more

The old build shipped a third "mock" engine that returned the same invented
certificate (KABRA DRUGS / folio 8866) for every image. If it was ever saved
in settings.json, or if you launched the old demo script, every run showed
that one fake row no matter which engine the dropdown said. That engine and
the demo script are gone. Only two engines exist now - OpenAI and Tesseract -
and a settings file still holding "mock" is migrated to OpenAI on startup.
Fake data can no longer reach a customer CSV.

## Fixing "Tesseract is not working"

Tesseract is a separate native program; the pip package is only a wrapper.
The app now checks for it when the engine starts and tells you exactly what
to install instead of failing image by image:

* Windows: https://github.com/UB-Mannheim/tesseract/wiki - keep the default
  folder `C:\Program Files\Tesseract-OCR`, which the app auto-detects.
* macOS: `brew install tesseract`
* Linux: `sudo apt install tesseract-ocr`

Installed somewhere unusual? Set `TESSERACT_CMD` to the full path of
`tesseract.exe`.

## Fixing "OpenAI is not working"

Click **API key** in the toolbar, paste your `sk-...` key, press **Test**,
then **Save**. The dot next to it turns green when a key is available. The
key goes into the Windows Credential Manager / macOS Keychain, or an
obfuscated 0600 file if no keychain exists - never into settings.json and
never into the CSV. Extract refuses to start without one and opens this
dialog instead of failing mid-run.

## Shipping the app as an .exe

    pip install pyinstaller
    python build_exe.py

Produces `dist/ShareCertificateOCR.exe` - one double-clickable file, no
Python needed on the client's machine, and no source code shared. The icon
comes from `assets/icon.ico` (a placeholder is included; drop in the real
artwork as a 256x256 .ico and rebuild). Build on Windows to get a Windows
exe - PyInstaller does not cross-compile. The API key is never baked into
the exe; each user enters it once in the app.

## .env.example

Optional, and only useful when running from source or on a server. Copy it
to `.env` and fill in what you need. Desktop users should use the API key
button instead - a keychain entry is safer than a text file. Never commit a
real `.env`.
