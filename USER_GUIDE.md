# Share Certificate OCR - User Guide

Every button, dropdown and indicator in the window, top to bottom, and what
it actually does. Written for the person operating the software, not for a
developer.

---

## 1. Top bar

### Output folder
Opens the folder where the CSV files and the working database are kept
(`C:\Users\<you>\.share_ocr\` on Windows). Use it when you want the raw
output without going through **Download CSV**. Nothing is changed by
clicking it - it only opens Explorer / Finder.

### API keys
Opens the key manager described in section 2. This is where OpenAI keys
live; you never edit a file or set an environment variable. You can add as
many keys as you have - 4, 10, however many - not just one.

### The coloured dot next to it
A one-glance status of the key pool:

| Dot | Text | Meaning |
|---|---|---|
| Grey | `Add OpenAI API key(s)` | No key found. Extraction with the OpenAI engine will refuse to start. |
| Green | `3 API keys (Windows Credential Manager)` | That many keys are available, and where they were found. |
| Grey | `API key not needed` | The Tesseract engine is selected, which runs offline. |

### Engine
Which OCR method reads the certificate.

* **OpenAI vision (best accuracy)** - sends each image to OpenAI. Needs a key
  and an internet connection. Costs money per image. This is the one that
  reads faded, stamped and handwritten certificates properly.
* **Tesseract (offline, free)** - runs entirely on your machine, no key, no
  cost, no data leaves the building. Much weaker on old certificates: it
  will often get the company and the numbers but miss the endorsements and
  the joint holders. Requires the Tesseract program to be installed
  separately (the app tells you exactly how if it is missing).

Switching the engine changes what else is shown - the Model dropdown only
appears for OpenAI, because Tesseract has no models.

### Workers
How many certificates are read at the same time. More workers = faster, up
to the point where your connection or the API rate limit becomes the
bottleneck.

* 4-8 for a laptop doing a few thousand.
* 16-32 for a bulk run on a good connection, especially with several API
  keys added (section 2) so the load spreads across them.
* If the rate (images/second, in the status bar) is lower than you expect,
  add more keys before adding more workers - a bigger key pool absorbs rate
  limits more effectively than raw worker count.

Changing it mid-run takes effect on the next run, not the current one.

### Model (OpenAI only)
Which OpenAI model reads the image. Accuracy and cost both go up as you go
down the list.

| Model | Use it for |
|---|---|
| `gpt-4o-mini` | Bulk. The default. Cheapest sensible choice for lakhs of scans. |
| `gpt-4.1-mini` | Balanced, a step up on messy scans. |
| `gpt-4.1-nano` | Cheapest of all. Only for clean, modern, high-contrast scans. |
| `gpt-4o` | Faded, stamped or handwritten certificates. Use it for the re-run of flagged rows. |
| `gpt-4.1` | Strongest reading, highest cost. |
| `o4-mini` | Slow and deliberate. Good for a final pass over stubborn rows. |

The practical workflow: run everything on `gpt-4o-mini`, then re-run only
the **Needs review** rows on `gpt-4o`. That gets you high accuracy without
paying the high price on all 30 lakh images.

---

## 2. The API keys dialog

### Why more than one key
Extraction spreads requests across every key you have added. The moment one
key hits its rate limit or runs out of credit, the app automatically moves
to the next key instead of failing the image - so a single key's limit
never stalls a run, and you should never need to manually retry anything.
For a large job (lakhs of images), add every key you have; the more keys in
the pool, the smoother the run.

### The list of keys
Each saved key is shown masked (e.g. `sk-proj…4f2A`), with its own **Test**
and **Remove** buttons.

### Test
Makes one tiny call to OpenAI with that specific key and reports back -
whether it is valid, out of credit, or blocked by a firewall. Use this
before a big run for every key you add.

### Remove
Deletes that one key from this machine, immediately. The others in the pool
keep working.

### Add key
Paste a key (looks like `sk-...`, get one from
https://platform.openai.com/api-keys) and press **Add key**. It is saved and
added to the pool right away - repeat for every key you have. Keys go into
the Windows Credential Manager (or the macOS Keychain), and only into an
encoded file in your user folder if no keychain is available. They are
**never** written into the settings file, the log, or the CSV, so you can
hand a CSV to a client safely.

### Show key while typing
Uncovers the text in the entry box so you can check what you pasted before
adding it. Off by default so a key is not visible over someone's shoulder or
in a screen share.

### Close
Closes the dialog. Keys are saved as soon as you press **Add key** or
**Remove**, not on Close.

---

## 3. The drop zone

### The large dashed panel
Click it to pick files, or drag images straight onto it from Explorer.
Accepts JPG, PNG, WEBP, TIFF and PDF. A multi-page PDF is split and each
page is treated as its own certificate.

### Select folder
Picks an entire folder, including everything in its sub-folders. This is the
normal way to start a bulk job - point it at the top folder and leave it.
The app indexes as it goes, so a folder with lakhs of files does not freeze
the window.

### Extract
Starts reading. Greyed out until you have selected something. If the OpenAI
engine is selected and no key is stored, this opens the key dialog instead
of failing halfway through.

### Pause / Resume
Stops handing out new images without losing anything. Workers finish what
they are holding, then wait. Useful when you need the bandwidth or want to
stop spending for a while. The button changes to **Resume** while paused.

### Stop
Ends the run. Everything already read is saved. You can close the app
entirely and press **Extract** again later - it picks up where it left off
and does not re-read or re-charge for files already done.

### There is no "Retry failed" button
There does not need to be one. A rate limit or a dropped connection on one
key automatically moves that request to the next key in your pool (see
section 2) - the file still gets read on the same pass. A file that
genuinely cannot be read (a corrupt scan) is simply left out of the results;
pressing **Extract** again later still picks up anything left pending
without re-reading or re-charging for files already done. If you want to
see which files those were, switch to the **Failed** tab in Results
(section 4) - it is a read-only list, there is nothing to click to retry it,
because there is nothing a retry would do that the next **Extract** press
does not already do on its own.

### "Nothing selected" / "1,240 files selected"
Just tells you what **Extract** is about to work on.

---

## 4. Results

### The records badge
How many rows have been extracted so far in this session.

### All / Failed
* **All** - every row that was successfully extracted. This is the normal
  view; rows the validator is not confident about are tinted amber or blue
  right in this list (see the flag table below) rather than hidden behind a
  separate filter.
* **Failed** - not extracted rows at all, but the files that errored out and
  never produced one. Shows the file name and the error, so you can see
  which scans need a human look (usually a corrupt or unreadable file - a
  rate limit or dropped connection does not end up here, since the key pool
  already moves that request to another key automatically). Double-click or
  **Open** still opens the original scan from this tab; **Delete selected**
  does not apply here since there is no extracted row to delete - just press
  **Extract** again to have another go at anything still pending.

A file that gives up for good (no more automatic retries left) also gets a
row in the CSV itself - File name linked to the scan, Review marked "Yes",
the error in Validation Flags, every other column blank. That way a file
that could never be read still shows up when you open the CSV in Excel, not
just here in the app.

In the **All** tab, a row is tinted amber or blue (see the legend above the
table) when something does not add up:

| Flag | What it means | Usual fix |
|---|---|---|
| `Share count 100 != distinctive span 90` | The range does not contain as many numbers as the certificate claims. | One digit misread in the range. |
| `Distinctive range runs backwards ... likely a misread digit` | The "to" number came out lower than the "from". Impossible. | One digit misread, almost always in the last two places. |
| `Missing company_name` (or certificate_no, holder, shares) | A required field could not be read. | Bad scan, cropped edge, or a fold across the box. |
| `Bad date format` / `Date out of plausible range` | The issue date did not parse or is outside 1850-today. | Handwritten or stamped-over date. |
| `Unusual face value (30) - check it` | The face value is not a real denomination, so it is probably a paid-up or total amount. | Read the capital clause on the certificate. |
| `Face value looks like the share count` | Both numbers are identical on a large holding. | The model copied the wrong box. |
| `Add-on not captured: face_value_per_share, ...` | An optional billed field came back empty. | Soft warning only. The row is otherwise fine. |

### The table
One row per certificate. Columns match the CSV exactly. Double-click a row
to open the original image, so you can compare against the scan without
hunting for the file.

**The exported CSV works the same way in Excel.** The File Name column in
the CSV is a live link - click it and Excel opens the exact scan that row
came from, the same as double-clicking the row here.

### The thumbnails
A quick visual check that the right images are being processed - useful for
spotting an upside-down or blank scan early in a big batch.

---

## 5. Bottom bar

### Download CSV
Writes everything extracted so far to a single CSV wherever you choose. Safe
to use mid-run. Opens in Excel directly; the encoding is set so Indian names
and the rupee symbol do not turn into junk characters.

### Open CSV folder
Opens the working folder that holds the automatically written CSV parts. For
very large jobs the output is split into files of 200,000 rows so Excel can
actually open them. There is one CSV per part - no separate needs-review
file - flagged rows (including files that failed extraction entirely) are
right there in the same file, marked "Yes" in the Review column.

### Clear all
Empties the queue, the results table and the working database. It does not
touch any CSV you have already saved, and it does not touch your original
images. Ask before using it on a job someone else started - progress cannot
be recovered afterwards.

### Status line and progress bar
Shows the current file count, the rate in images per second, and an
estimated finish time. During a 30 lakh run this is the number to watch: if
the rate drops sharply, you are being rate-limited and should lower the
worker count.

---

## 6. Things worth knowing

**Closing the window is safe.** No warning dialog, nothing is lost. Progress
lives in a database, so reopening and pressing Extract resumes the same job.

**Nothing is ever re-charged twice.** A file already read successfully is
skipped on the next run, even after a crash or a power cut.

**The window remembers its size and position** between sessions.

**A row in the CSV is never invented.** If a field could not be read it is
empty and flagged. The software has no demo or sample mode - every row you
see came from an actual image.

**Checking accuracy properly.** Run a sample of scans, export the CSV, then
from a command prompt in the software folder:

    python tools/check_accuracy.py C:\temp\out.csv

It compares against the four verified sample certificates and prints a
per-field accuracy percentage. Use that number when quoting a client rather
than an estimate.
