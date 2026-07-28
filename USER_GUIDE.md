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

### API key
Opens the key dialog described in section 2. This is where the OpenAI key
lives; you never edit a file or set an environment variable.

### The coloured dot next to it
A one-glance status of the key:

| Dot | Text | Meaning |
|---|---|---|
| Grey | `Add OpenAI API key` | No key found. Extraction with the OpenAI engine will refuse to start. |
| Green | `Key sk-...abcd (Windows Credential Manager)` | A key is available, and where it was found. |
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
* 16-32 for a bulk run on a good connection.
* Start lower if you see rate-limit messages in the status bar; the app
  backs off and retries on its own, but fewer workers is smoother.

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

## 2. The API key dialog

### The key box
Paste your key here. It looks like `sk-...`. Get it from
https://platform.openai.com/api-keys.

### Show key
Uncovers the text so you can check what you pasted. Off by default so the
key is not visible over someone's shoulder or in a screen share.

### Test
Makes one tiny call to OpenAI and reports back. Use this before a big run -
it is the difference between finding out now and finding out after 200
failed images. It tells you specifically whether the key is invalid, out of
credit, or blocked by a firewall.

### Save
Stores the key and closes. It goes into the Windows Credential Manager (or
the macOS Keychain), and only into an encoded file in your user folder if no
keychain is available. It is **never** written into the settings file, the
log, or the CSV, so you can hand a CSV to a client safely.

### Remove
Deletes the stored key from this machine. Use it before handing the computer
to someone else.

### Close
Closes without saving changes.

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

### Retry failed
Re-queues only the rows that errored (network drop, rate limit, corrupt
file). Rows that were read successfully are left alone. Change the model to
a stronger one first if the failures were accuracy problems rather than
network problems.

### "Nothing selected" / "1,240 files selected"
Just tells you what **Extract** is about to work on.

---

## 4. Results

### The records badge
How many rows have been extracted so far in this session.

### Needs review / All
The important switch.

* **All** - every row.
* **Needs review** - only rows the validator is not confident about. This is
  your work queue. At scale you never check every row; you check these.

A row lands in **Needs review** when something does not add up:

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
actually open them, plus a separate `certificates-needs-review.csv`
containing only the flagged rows.

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
