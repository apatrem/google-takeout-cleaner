# Google Takeout Cleaner

Clean up your Google Photos export so the photos and videos end up in one
folder, with correct dates, GPS, and (optionally) tidy filenames.

This guide assumes **you have never used a terminal** and that nothing is
installed yet. Follow the steps in order. Works on **macOS** and **Linux**.

---

## What this tool does

When you export your Google Photos with [Google Takeout](https://takeout.google.com/),
you typically end up with:

- Several giant ZIP files (`takeout-...001.zip`, `takeout-...002.zip`, ...).
- The same photo appearing in both an album folder (e.g. `Vacation 2019`) and
  a year folder (e.g. `Photos from 2019`).
- A small `.json` file next to every photo containing the real capture date,
  GPS coordinates, title, etc.
- Photo and video files whose **date on disk does not match** the actual
  capture date — because Google rewrote the dates when packaging the export.

This tool helps you clean all of that, in four steps:

1. **Merge** all your unzipped Takeout folders into a single folder.
2. **Fix** dates and GPS on every photo/video from the JSON sidecars, AND
   set the file's "modified" date on disk to the real capture date.
3. **Dedup** (optional): remove the duplicate copies that live in
   `Photos from YYYY` folders when an album copy already exists.
4. **Rename** (optional): rename your files to `YYYY-MM-DD_HH-mm-ss.ext`
   so they sort chronologically.

**The tool runs in "dry-run" mode by default** — it tells you what it would
do without changing anything. Nothing is modified until you add `--apply`.

---

## Step 1 — Unzip your Takeout

Google emails you several download links. Download all of them into the same
folder, e.g. `~/Downloads/google-export/`. You should end up with files like:

```
takeout-20260101T120000Z-001.zip
takeout-20260101T120000Z-002.zip
takeout-20260101T120000Z-003.zip
...
```

**Unzip them.** Just double-click each ZIP file in Finder (macOS) or your
file manager (Linux). Each one creates a folder called `Takeout` next to
the ZIP. Rename them so they don't overwrite each other:

```
Takeout-001/
Takeout-002/
Takeout-003/
...
```

(Or unzip them into separate subfolders — the tool handles either layout.)

---

## Step 2 — Install the prerequisites

You need two things: **Python 3** and **exiftool**. Both are free.

### On macOS

Open the **Terminal** app (press <kbd>Cmd</kbd>+<kbd>Space</kbd>, type
"Terminal", press <kbd>Enter</kbd>).

1. **Install Homebrew** (a package manager — only needed if you don't have it).
   Paste this single line and press <kbd>Enter</kbd>:

   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```

   It will ask for your password. Type it (you won't see characters appear —
   that's normal) and press <kbd>Enter</kbd>. The install takes a few minutes.

2. **Install Python and exiftool**:

   ```bash
   brew install python exiftool
   ```

3. **Check they work**:

   ```bash
   python3 --version
   exiftool -ver
   ```

   You should see version numbers, not errors.

### On Linux (Debian / Ubuntu / Mint)

Open a terminal and run:

```bash
sudo apt update
sudo apt install -y python3 libimage-exiftool-perl
python3 --version
exiftool -ver
```

### On Linux (Fedora / RHEL)

```bash
sudo dnf install -y python3 perl-Image-ExifTool
python3 --version
exiftool -ver
```

---

## Step 3 — Get the tool

In the terminal:

```bash
cd ~/Downloads
git clone https://github.com/apatrem/google-takeout-cleaner.git
cd google-takeout-cleaner
```

(If you don't have `git`, install it with `brew install git` on macOS or
`sudo apt install git` on Linux. Or just download the repo as a ZIP from
GitHub and unzip it.)

---

## Step 4 — Run the tool

### The happy path — one command

If you just want everything done at once:

```bash
python3 takeout_cleaner.py all \
  --target ~/Pictures/google-photos-cleaned \
  --source ~/Downloads/google-export/Takeout-001 \
  --source ~/Downloads/google-export/Takeout-002 \
  --source ~/Downloads/google-export/Takeout-003 \
  --dedup \
  --rename
```

This is a **dry run** — it just shows what would happen. Read the output.

When you're happy, add `--apply` to actually do it:

```bash
python3 takeout_cleaner.py all \
  --target ~/Pictures/google-photos-cleaned \
  --source ~/Downloads/google-export/Takeout-001 \
  --source ~/Downloads/google-export/Takeout-002 \
  --source ~/Downloads/google-export/Takeout-003 \
  --dedup \
  --rename \
  --apply
```

**What the flags mean:**
- `--target` — where the cleaned-up library should end up. Pick a fresh
  folder; the tool will create it.
- `--source` — one of your unzipped Takeout folders. Repeat the flag for
  each one.
- `--dedup` — remove year-folder duplicates when an album copy exists.
  Optional.
- `--rename` — rename files to `YYYY-MM-DD_HH-mm-ss.ext`. Optional.
- `--apply` — actually do the work. **Without this, nothing is changed.**

**Optional flags worth knowing:**
- `--timezone "Europe/Paris"` — IANA timezone for photo EXIF dates and
  rename filenames. Defaults to your system's local timezone. Videos are
  always stored in UTC (per the QuickTime spec).
- `--album "Vacation 2019"` — only process one album. Handy for testing
  the tool on a small subset before running on everything.
- `--log-file ~/takeout_cleaner.log` — write a detailed log to disk in
  addition to the terminal output.
- `--prefer-json-on-conflict` — when EXIF disagrees with JSON, overwrite
  EXIF with the JSON date. See "How EXIF and JSON are reconciled" below.

### How EXIF and JSON are reconciled

For each file, the `fix` step reads the existing EXIF date and compares it
to the JSON sidecar:

| Existing EXIF | What happens | Filesystem date |
|---|---|---|
| Missing | Write JSON date | JSON date |
| Agrees with JSON (same calendar day) | Skip — keep EXIF as-is | JSON date |
| **"Bogus"** (`1970-01-01`, `1980-01-01`, `2000-01-01`, `2010-01-01`, `2036-01-01`, or any year < 1995 / > next year) | Overwrite with JSON | JSON date |
| Disagrees with JSON, not bogus | Leave EXIF alone, log a `CONFLICT` line | **EXIF date** (we're trusting EXIF, so the file date matches) |
| Disagrees with JSON, not bogus, with `--prefer-json-on-conflict` | Overwrite with JSON, log an `OVERRIDE` line | JSON date |

In other words: **the tool only overwrites your EXIF dates when they look
clearly broken**. Anything plausible is preserved, with a log line so you
can audit disagreements after the run.

### Or: run each step separately

If you want more control, you can run the four phases one at a time. Each
one has its own `--help`:

```bash
python3 takeout_cleaner.py merge  --help
python3 takeout_cleaner.py fix    --help
python3 takeout_cleaner.py dedup  --help
python3 takeout_cleaner.py rename --help
```

Typical sequence:

```bash
# 1. Merge multiple Takeout extractions into one folder.
python3 takeout_cleaner.py merge \
  --target ~/Pictures/google-photos-cleaned \
  --source ~/Downloads/google-export/Takeout-001 \
  --source ~/Downloads/google-export/Takeout-002 \
  --apply

# 2. Fix dates and GPS from JSON sidecars.
python3 takeout_cleaner.py fix \
  --root ~/Pictures/google-photos-cleaned \
  --apply

# 3. (Optional) Remove year-folder duplicates.
python3 takeout_cleaner.py dedup \
  --root ~/Pictures/google-photos-cleaned \
  --apply

# 4. (Optional) Rename to date-based filenames.
python3 takeout_cleaner.py rename \
  --root ~/Pictures/google-photos-cleaned \
  --apply
```

---

## Safety notes

- **Always do a dry run first.** That's the default — just don't add
  `--apply`. Read the summary. Then re-run with `--apply` if it looks right.
- **Keep your original Takeout ZIPs** until you've verified the cleaned
  library looks good. Once you're happy, you can delete them.
- **The `merge` step uses hardlinks by default** on macOS and Linux. That
  means it's near-instant and uses no extra disk space — the cleaned folder
  shares the data with your source folders. **Don't move the source folders
  to a different disk** until after the merge, or the hardlinks won't work.
  Add `--copy` if you want full independent copies (slower, uses 2× space).
- **`Trash`, `Bin`, and `Archive` subfolders are skipped by default.** Add
  `--include-trash` if you want them included.

---

## On Windows

The instructions above assume macOS or Linux. Windows works too — the
commands and paths are different, but the tool itself is the same.

### 1. Unzip your Takeout

Same as before: download all the ZIP files into one folder (e.g.
`C:\Users\YOU\Downloads\google-export\`) and **right-click each one →
"Extract All"**. Rename the resulting folders so they don't collide:
`Takeout-001`, `Takeout-002`, ...

> **Note about album names:** if any of your Google Photos albums contain
> the characters `< > : " | ? * \ /`, Windows will silently rename those
> folders during unzip (e.g. `WTF?? party` becomes `WTF party`). Your
> photos and metadata are unaffected — only the folder name on disk will
> differ from what you saw in Google Photos.

### 2. Install Python, ExifTool, and Git

Open **Windows Terminal** (it ships with Windows 11; on Windows 10 install
it from the Microsoft Store, or just use **PowerShell**).

```powershell
winget install Python.Python.3.12 -e
winget install OliverBetz.ExifTool -e
winget install Git.Git -e
```

Close the terminal and reopen it (so `PATH` picks up the new tools).
Verify:

```powershell
py --version
exiftool -ver
git --version
```

You should see version numbers, not errors.

### 3. Enable long paths (one-time setup)

Google Photos exports can have file paths longer than Windows' default
260-character limit. Run this once in an **elevated PowerShell** (right-click
PowerShell → "Run as administrator"):

```powershell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
  -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
```

If you can't get admin access, you can instead flip the toggle in
**Settings → System → For developers → "Enable Win32 long paths"**.

When the tool starts, it warns you if this is still disabled.

### 4. Get the tool

```powershell
cd $HOME\Downloads
git clone https://github.com/apatrem/google-takeout-cleaner.git
cd google-takeout-cleaner
```

### 5. Run the tool

The happy-path single command:

```powershell
py takeout_cleaner.py all `
  --target $HOME\Pictures\google-photos-cleaned `
  --source $HOME\Downloads\google-export\Takeout-001 `
  --source $HOME\Downloads\google-export\Takeout-002 `
  --source $HOME\Downloads\google-export\Takeout-003 `
  --dedup `
  --rename
```

This is a **dry run**. When the output looks right, re-run with `--apply`
at the end.

The backticks (`` ` ``) are PowerShell's line-continuation character — the
same role `\` plays on macOS/Linux. If you'd rather write a single long
line, that works too.

All the optional flags (`--timezone`, `--album`, `--log-file`,
`--prefer-json-on-conflict`) work identically. See the Unix sections
above for what they do.

---

## Known limitations

The tool intentionally keeps a small scope. If any of these matter to you,
let us know:

- **Live Photos / motion photos** (`.HEIC` + `.MOV` pair, or `.JPG` + `.MP4`
  companion) are processed independently. Each gets its date fixed, but
  they aren't tracked as a pair.
- **Edited variants** (`IMG_1234-edited.jpg`, `-modifié`, `-effects`, etc.)
  share metadata with the original in Google Takeout. The current matcher
  may not always associate the edited variant with the original's JSON.
- **Trash/Archive folder names** are matched in a handful of languages
  (English, French, German, Italian, Spanish). Open an issue if your
  locale needs handling.

---

## Troubleshooting

**"command not found: python3"** — Re-run the install step for your OS in
Step 2. On macOS, also try `which python3` to see where it landed.

**"exiftool not found"** — Same: re-run the install step. Try
`which exiftool`.

**"target overlaps with a source"** — Pick a `--target` folder that is
*not* inside any of your `--source` folders.

**The summary shows lots of "Errors" but everything looks fine** — The
errors usually come from corrupted files exiftool can't write to (e.g. a
broken HEIC or a video with an unusual container). Filesystem mtime is
still synced for those. Look at the listed paths and inspect them.

**Nothing happened** — Did you forget `--apply`? By default the tool only
shows what it would do.
