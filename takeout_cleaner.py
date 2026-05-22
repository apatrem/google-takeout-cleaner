#!/usr/bin/env python3
"""Google Takeout Cleaner.

Subcommands:
  merge   Reconcile multiple Takeout extractions (Takeout/, Takeout-2/, ...)
          into one folder structure, keeping all original files.
  fix     Restore EXIF date + GPS from JSON sidecars AND sync filesystem
          mtime/ctime to the capture (or upload) date.
  dedup   Remove duplicate copies in year folders ("Photos from 2019"),
          keeping the copies that live in album folders. Content-hash based.
  rename  Rename media to YYYY-MM-DD_HH-mm-ss.ext using JSON / EXIF / mtime.
  all     Run merge -> fix; optionally chain dedup and rename.

Dry-run by default. Pass --apply to actually modify files.
Requires `exiftool` on PATH. Cross-platform (macOS, Linux).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp",
    ".bmp", ".tif", ".tiff", ".dng", ".arw", ".cr2", ".nef", ".raw",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".avi", ".3gp", ".hevc", ".webm",
    ".mpg", ".mpeg", ".mts", ".m2ts", ".mkv",
}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# Folder names Google uses for year buckets across locales.
# Match: "Photos from 2019", "Photos de 2019", "Fotos de 2019", "Foto's van 2019", ...
YEAR_FOLDER_RE = re.compile(
    r"^(photos?|fotos?|foto's|bilder|immagini|zdj[eę]cia|снимки)\s+"
    r"(from|de|del|of|van|aus|da|do|з|із)\s+\d{4}$",
    re.IGNORECASE,
)

# Folders we skip by default (user can opt in with --include-trash).
TRASH_FOLDER_NAMES = {
    "Trash", "Bin", "Corbeille", "Papierkorb", "Cestino",
    "Archive", "Archived", "Archivos", "Archiv",
}

# Top-level Google Photos folder, varies by locale ("Google Photos", "Google Photos", "Google Fotos", ...).
GOOGLE_PHOTOS_FOLDER_PATTERNS = [
    re.compile(r"^Google[  ]Photos$"),
    re.compile(r"^Google[  ]Fotos$"),
    re.compile(r"^Google[  ]Foto's$"),
]

# JSON sidecar shapes Google has used (the suffix has been truncated to ~46 chars
# at various points, hence the variants).
STANDARD_JSON_REGEX = re.compile(r".*\.supplemental-metadata\.json$", re.IGNORECASE)
NUMBERED_JSON_REGEX = re.compile(r".*\.supplemental-metadata\(\d+\)\.json$", re.IGNORECASE)
GENERIC_SUPPLEMENTAL_REGEX = re.compile(r".*\.supplemental.*\.json$", re.IGNORECASE)
JSON_BASE_REGEX = re.compile(r"^(.*)\.supplemental-metadata\(\d+\)\.json$", re.IGNORECASE)
ZERO_DATE_REGEX = re.compile(r"^0{4}:0{2}:0{2}")

# Canonical date-time filename: 2024-08-15_14-32-07[.ext] or with _NN suffix.
CANONICAL_NAME_RE = re.compile(
    r"^[12]\d{3}-[01]\d-[0-3]\d_[0-2]\d-[0-5]\d-[0-5]\d(_\d+)?$"
)
# Embedded datetime, with or without separators.
EMBEDDED_DT_RE = re.compile(
    r"([12]\d{3})[-_.:]?([01]\d)[-_.:]?([0-3]\d)[ _T-]+"
    r"([0-2]\d)[-_:.]?([0-5]\d)[-_:.]?([0-5]\d)"
)

# Two-stage content hash settings.
HASH_PROBE_BYTES = 64 * 1024  # head + tail probe before full hash


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

_LOGGER = logging.getLogger("takeout_cleaner")


def log(msg: str) -> None:
    print(msg, flush=True)
    _LOGGER.info(msg)


def warn(msg: str) -> None:
    print(f"warn: {msg}", file=sys.stderr, flush=True)
    _LOGGER.warning(msg)


def err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr, flush=True)
    _LOGGER.error(msg)


def nfc(s: str) -> str:
    """Unicode NFC normalization. macOS stores filenames in NFD; Google's JSON
    titles are NFC. Matching without normalization fails on accented characters."""
    return unicodedata.normalize("NFC", s)


def resolve_timezone(name: Optional[str]) -> dt.tzinfo:
    """Return a tzinfo for `name` if given, else the system's local tz.

    UTC is short-circuited to `datetime.timezone.utc` so it works on systems
    that don't ship an IANA tz database (notably Windows — see the install
    note for `tzdata`)."""
    if not name:
        return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
    if name.strip().upper() == "UTC":
        return dt.timezone.utc
    if ZoneInfo is None:
        err("--timezone requires Python 3.9+ (zoneinfo). Falling back to system local tz.")
        return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
    try:
        return ZoneInfo(name)
    except Exception as exc:
        if sys.platform == "win32":
            err(
                f"invalid timezone {name!r}: {exc}. Windows does not ship an "
                f"IANA tz database; install one with:  py -m pip install tzdata"
            )
        else:
            err(f"invalid timezone {name!r}: {exc}")
        raise SystemExit(2)


def setup_file_logging(path: Optional[Path]) -> None:
    """Attach a file handler so all log() / warn() / err() output also lands in a file."""
    if path is None:
        return
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _LOGGER.setLevel(logging.DEBUG)
    _LOGGER.addHandler(handler)


_LONG_PATHS_CHECKED = False


def check_windows_long_paths() -> None:
    """Warn once if Windows long-path support is disabled. Best-effort:
    silent on non-Windows or if the registry is unreadable."""
    global _LONG_PATHS_CHECKED
    if _LONG_PATHS_CHECKED:
        return
    _LONG_PATHS_CHECKED = True
    if sys.platform != "win32":
        return
    try:
        import winreg  # stdlib on Windows only
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
    except (OSError, ImportError):
        return  # corporate lockdown or unexpected stdlib state — don't be noisy
    if value:
        return
    warn(
        "Windows long-path support is disabled. Files with paths longer than "
        "260 characters may fail. To fix (one-time, requires admin): in an "
        "elevated PowerShell run "
        "New-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\"
        "FileSystem' -Name 'LongPathsEnabled' -Value 1 -PropertyType DWORD -Force"
    )


def is_media_ext(ext: str) -> bool:
    return ext.lower() in MEDIA_EXTENSIONS


def is_video_ext(ext: str) -> bool:
    return ext.lower() in VIDEO_EXTENSIONS


def is_year_folder(name: str) -> bool:
    return bool(YEAR_FOLDER_RE.match(name))


def is_trash_folder(name: str) -> bool:
    return name in TRASH_FOLDER_NAMES


def is_google_photos_root(name: str) -> bool:
    return any(p.match(name) for p in GOOGLE_PHOTOS_FOLDER_PATTERNS)


def in_trash(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return any(is_trash_folder(part) for part in rel.parts)


def parse_float(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def valid_lat_lon(lat: Optional[float], lon: Optional[float]) -> bool:
    if lat is None or lon is None:
        return False
    return abs(lat) > 1e-12 and abs(lon) > 1e-12


def fmt_exif_time(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y:%m:%d %H:%M:%S")


def fmt_canonical(ts: int, tz: Optional[dt.tzinfo] = None) -> str:
    """Format a Unix timestamp as YYYY-MM-DD_HH-mm-ss.
    Defaults to local time so filenames match what photo viewers display."""
    if tz is None:
        tz = dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
    return dt.datetime.fromtimestamp(ts, tz=tz).strftime("%Y-%m-%d_%H-%M-%S")


def iter_media(root: Path, include_trash: bool = False) -> Iterable[Path]:
    """Yield media files under root, skipping Trash/Bin/Archive by default."""
    for dirpath, dirnames, filenames in os.walk(root):
        if not include_trash:
            dirnames[:] = [d for d in dirnames if not is_trash_folder(d)]
        for fn in filenames:
            p = Path(dirpath) / fn
            if is_media_ext(p.suffix):
                yield p


def relative_display(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except Exception:
        return str(path)


# ---------------------------------------------------------------------------
# Progress bar
# ---------------------------------------------------------------------------

class ProgressBar:
    def __init__(self, total: int, label: str, enabled: bool = True, width: int = 30) -> None:
        self.total = max(0, total)
        self.label = label
        self.enabled = enabled
        self.width = width
        self.current = 0
        self._last_render = -1
        if self.enabled:
            self._render(force=True)

    def advance(self, step: int = 1) -> None:
        self.current = min(self.total, self.current + step)
        self._render()

    def done(self) -> None:
        self.current = self.total
        self._render(force=True)
        if self.enabled:
            print(file=sys.stderr, flush=True)

    def _render(self, force: bool = False) -> None:
        if not self.enabled:
            return
        if self.total <= 0:
            print(f"\r{self.label}: [no items]", end="", file=sys.stderr, flush=True)
            return
        percent = int((self.current * 100) / self.total)
        if not force and percent == self._last_render:
            return
        self._last_render = percent
        filled = int((self.current * self.width) / self.total)
        bar = "#" * filled + "-" * (self.width - filled)
        line = f"\r{self.label}: [{bar}] {self.current}/{self.total} ({percent:3d}%)"
        print(line, end="", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# JSON sidecar handling
# ---------------------------------------------------------------------------

def load_json(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


def json_priority(path: Path) -> int:
    name = path.name
    if STANDARD_JSON_REGEX.fullmatch(name):
        return 0
    if NUMBERED_JSON_REGEX.fullmatch(name):
        return 1
    lname = name.lower()
    if lname.endswith(".suppl.json"):
        return 2
    if GENERIC_SUPPLEMENTAL_REGEX.fullmatch(name):
        return 3
    if lname.endswith(".json"):
        return 4
    return 5


def derived_media_base_from_json_name(name: str) -> str:
    """Strip Google's sidecar suffix (often truncated) to recover the media name."""
    match = JSON_BASE_REGEX.match(name)
    if match:
        return match.group(1)
    lowered = name.lower()
    truncations = [
        ".supplemental-metadata.json",
        ".suppl.json",
        ".supplemental-metadat.json",
        ".supplemental-metad.json",
        ".supplemental-.json",
    ]
    for suffix in truncations:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    if lowered.endswith(".json"):
        idx = lowered.rfind(".supplemental")
        if idx != -1:
            return name[:idx]
        return name[:-5]
    return name


def extract_geo(data: dict) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    for key in ("geoDataExif", "geoData"):
        geo = data.get(key)
        if not isinstance(geo, dict):
            continue
        lat = parse_float(geo.get("latitude"))
        lon = parse_float(geo.get("longitude"))
        alt = parse_float(geo.get("altitude"))
        if valid_lat_lon(lat, lon):
            return lat, lon, alt
    return None, None, None


def extract_timestamp(data: dict) -> Optional[int]:
    """Prefer photoTakenTime (capture); fall back to creationTime (upload)."""
    for key in ("photoTakenTime", "creationTime"):
        block = data.get(key)
        if not isinstance(block, dict):
            continue
        raw = block.get("timestamp")
        if raw is None:
            continue
        try:
            ts = int(str(raw))
        except (TypeError, ValueError):
            continue
        if ts > 0:
            return ts
    return None


def list_media_in_dir(directory: Path, cache: Dict[Path, List[Path]]) -> List[Path]:
    if directory in cache:
        return cache[directory]
    out: List[Path] = []
    try:
        for child in directory.iterdir():
            if child.is_file() and is_media_ext(child.suffix):
                out.append(child)
    except OSError:
        pass
    cache[directory] = out
    return out


def _list_all_in_dir_nfc_index(directory: Path) -> Dict[str, Path]:
    """Return {NFC(name) -> real on-disk Path} for one directory. Used to find
    files whose names match by NFC even when the disk encoding is NFD (macOS)."""
    index: Dict[str, Path] = {}
    try:
        for child in directory.iterdir():
            index[nfc(child.name)] = child
    except OSError:
        pass
    return index


def associate_json_to_media(
    json_path: Path,
    data: dict,
    media_dir_cache: Dict[Path, List[Path]],
    nfc_index_cache: Optional[Dict[Path, Dict[str, Path]]] = None,
) -> Optional[Path]:
    parent = json_path.parent
    title = data.get("title")
    if not isinstance(title, str) or not title:
        return None

    decoded_title = unquote(title)
    derived_base = derived_media_base_from_json_name(json_path.name)

    # NFC-normalized lookup index for this directory.
    if nfc_index_cache is None:
        nfc_index = _list_all_in_dir_nfc_index(parent)
    else:
        if parent not in nfc_index_cache:
            nfc_index_cache[parent] = _list_all_in_dir_nfc_index(parent)
        nfc_index = nfc_index_cache[parent]

    candidate_names = [title, decoded_title, derived_base]
    if decoded_title != title:
        sanitized = decoded_title.replace("'", "_").replace(" ", "_")
        if sanitized != decoded_title:
            candidate_names.append(sanitized)

    seen: set[str] = set()
    for name in candidate_names:
        key = nfc(name)
        if key in seen:
            continue
        seen.add(key)
        match = nfc_index.get(key)
        if match is not None and match.is_file() and is_media_ext(match.suffix):
            return match

    # Fallback: unique stem-prefix match within the directory's media files.
    stems = {Path(title).stem, Path(decoded_title).stem, Path(derived_base).stem}
    media_files = list_media_in_dir(parent, media_dir_cache)
    for stem in sorted(s for s in stems if s):
        nfc_stem = nfc(stem)
        matches = [m for m in media_files if nfc(m.stem).startswith(nfc_stem)]
        if len(matches) == 1:
            return matches[0]
    return None


# ---------------------------------------------------------------------------
# exiftool helpers
# ---------------------------------------------------------------------------

def require_exiftool() -> None:
    if shutil.which("exiftool") is None:
        err("exiftool not found on PATH. Install with `brew install exiftool` (macOS) "
            "or `sudo apt install libimage-exiftool-perl` (Debian/Ubuntu).")
        raise SystemExit(2)


class ExiftoolSession:
    """Long-running exiftool process driven over stdin (-stay_open True).

    Spawning exiftool per file costs ~50ms each — on a 50k-photo takeout that's
    ~40 minutes of pure overhead. A persistent session amortizes that down to
    a few microseconds per call.

    We redirect stderr to stdout (single combined pipe) to avoid the classic
    subprocess deadlock where exiftool's stderr buffer fills while we wait
    for a stdout marker. exiftool emits the `{ready<tag>}` marker on *both*
    streams when stay_open is active, so a single combined read is sufficient.
    """

    def __init__(self) -> None:
        self._proc = subprocess.Popen(
            ["exiftool", "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    def execute(self, args: Sequence[str], filepath: str) -> Tuple[int, str]:
        """Run one exiftool invocation. Returns (returncode, combined_output)."""
        if self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("exiftool session not started")
        # exiftool's ready-tag value must be numeric; use a session-monotonic
        # counter rather than hex.
        self._counter += 1
        tag = str(self._counter)
        payload = "\n".join(list(args) + [filepath, f"-execute{tag}"]) + "\n"
        self._proc.stdin.write(payload)
        self._proc.stdin.flush()
        ready_marker = f"{{ready{tag}}}"
        chunks: List[str] = []
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise RuntimeError("exiftool session terminated unexpectedly")
            if ready_marker in line:
                # Capture anything on the same line before the marker.
                before, _sep, _after = line.partition(ready_marker)
                if before.strip():
                    chunks.append(before)
                break
            chunks.append(line)
        out = "".join(chunks)
        # Heuristic: exiftool prints "Error:" or "Warning:" to the combined
        # stream. A "0 image files updated" line also signals nothing happened.
        rc = 1 if "Error:" in out else 0
        return rc, out

    _counter: int = 0

    def close(self) -> None:
        if self._proc.poll() is not None:
            return
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.write("-stay_open\nFalse\n")
                self._proc.stdin.flush()
            self._proc.wait(timeout=10)
        except Exception:
            self._proc.kill()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                pass


def exiftool_read(paths: Sequence[Path]) -> Dict[str, dict]:
    if not paths:
        return {}
    cmd = [
        "exiftool", "-j", "-n", "-api", "QuickTimeUTC=1",
        "-DateTimeOriginal", "-CreateDate",
        "-MediaCreateDate", "-TrackCreateDate",
        "-OffsetTimeOriginal",
        "-GPSLatitude", "-GPSLongitude", "-GPSAltitude",
    ] + [str(p) for p in paths]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(proc.stderr.strip() or "exiftool read failed")
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else []
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse exiftool JSON: {exc}") from exc
    result: Dict[str, dict] = {}
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and "SourceFile" in item:
                result[str(item["SourceFile"])] = item
    return result


def has_valid_datetime(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    text = str(value).strip()
    if not text:
        return False
    if ZERO_DATE_REGEX.match(text):
        return False
    return True


def has_gps(tags: dict) -> bool:
    return valid_lat_lon(parse_float(tags.get("GPSLatitude")), parse_float(tags.get("GPSLongitude")))


# ---------------------------------------------------------------------------
# EXIF / JSON reconciliation
# ---------------------------------------------------------------------------

# EXIF dates that are obviously not real (camera clock unset -> firmware default,
# files with corrupted EXIF). When EXIF matches one of these we ignore the EXIF
# and let JSON win.
BOGUS_SENTINEL_DATES: set[Tuple[int, int, int, int, int, int]] = {
    (1970, 1, 1, 0, 0, 0),  # Unix epoch
    (1980, 1, 1, 0, 0, 0),  # FAT epoch / DCF default
    (2000, 1, 1, 0, 0, 0),  # common firmware default
    (2010, 1, 1, 0, 0, 0),  # common firmware default
    (2036, 1, 1, 0, 0, 0),  # year-2038-wrap default
}

_EXIF_DT_RE = re.compile(
    r"^\s*(\d{4})[:\-/](\d{2})[:\-/](\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
)
_OFFSET_RE = re.compile(r"^\s*([+\-])(\d{2}):?(\d{2})\s*$")


def parse_exif_datetime(value: object) -> Optional[dt.datetime]:
    """Parse an EXIF wall-clock string into a *naive* datetime."""
    if not isinstance(value, str):
        return None
    m = _EXIF_DT_RE.match(value)
    if not m:
        return None
    try:
        return dt.datetime(*(int(x) for x in m.groups()))
    except ValueError:
        return None


def parse_exif_offset(value: object) -> Optional[dt.timezone]:
    """Parse an EXIF OffsetTimeOriginal value like '+02:00' or '-0500'."""
    if not isinstance(value, str):
        return None
    m = _OFFSET_RE.match(value)
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    hours, minutes = int(m.group(2)), int(m.group(3))
    return dt.timezone(sign * dt.timedelta(hours=hours, minutes=minutes))


def exif_dt_to_ts(
    exif_dt: dt.datetime, tz_fallback: dt.tzinfo, offset_value: object = None
) -> int:
    """Convert a naive EXIF datetime to a Unix timestamp.
    Prefer OffsetTimeOriginal when present; else interpret in `tz_fallback`."""
    tz = parse_exif_offset(offset_value) or tz_fallback
    return int(exif_dt.replace(tzinfo=tz).timestamp())


def is_bogus_exif_dt(exif_dt: dt.datetime, today: Optional[dt.date] = None) -> bool:
    """Narrow heuristic: year outside [1995, this_year+1] or exact sentinel."""
    if today is None:
        today = dt.date.today()
    if exif_dt.year < 1995:
        return True
    if exif_dt.year > today.year + 1:
        return True
    key = (exif_dt.year, exif_dt.month, exif_dt.day,
           exif_dt.hour, exif_dt.minute, exif_dt.second)
    return key in BOGUS_SENTINEL_DATES


def exif_json_dates_agree(
    exif_dt: dt.datetime, json_ts: int, tz: dt.tzinfo
) -> bool:
    """Date-only comparison with a tz-crossing tolerance.

    Returns True if EXIF and JSON refer to the same calendar day in `tz`, OR
    they're adjacent days and EXIF's clock time is within +/- 3 h of midnight
    (which is how a photo straddles midnight after a tz interpretation flip)."""
    json_date = dt.datetime.fromtimestamp(json_ts, tz=tz).date()
    if exif_dt.date() == json_date:
        return True
    day_delta = abs((exif_dt.date() - json_date).days)
    if day_delta == 1 and (exif_dt.hour <= 3 or exif_dt.hour >= 21):
        return True
    return False


# ---------------------------------------------------------------------------
# Filesystem date sync
# ---------------------------------------------------------------------------

_SETFILE_CACHED: Optional[bool] = None


def _has_setfile() -> bool:
    """Cache the result of `which SetFile` (macOS Xcode CLT). Avoid re-probing."""
    global _SETFILE_CACHED
    if _SETFILE_CACHED is None:
        _SETFILE_CACHED = sys.platform == "darwin" and shutil.which("SetFile") is not None
    return _SETFILE_CACHED


def sync_filesystem_dates(path: Path, ts: int) -> None:
    """Set mtime + atime to ts. On macOS, best-effort also set Finder creation date."""
    try:
        os.utime(path, (ts, ts))
    except OSError as exc:
        warn(f"utime failed on {path}: {exc}")
        return
    if _has_setfile():
        date_str = dt.datetime.fromtimestamp(ts).strftime("%m/%d/%Y %H:%M:%S")
        subprocess.run(
            ["SetFile", "-d", date_str, "-m", date_str, str(path)],
            check=False, capture_output=True,
        )


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------

def hash_probe(path: Path) -> str:
    """Cheap hash of (first 64KB + last 64KB). Used to bucket likely duplicates."""
    h = hashlib.blake2b(digest_size=16)
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head = f.read(HASH_PROBE_BYTES)
            h.update(head)
            if size > HASH_PROBE_BYTES * 2:
                f.seek(-HASH_PROBE_BYTES, os.SEEK_END)
                h.update(f.read(HASH_PROBE_BYTES))
            elif size > HASH_PROBE_BYTES:
                # Tail overlaps head; just hash what's left.
                h.update(f.read())
    except OSError as exc:
        warn(f"probe hash failed on {path}: {exc}")
        return ""
    return h.hexdigest()


def hash_full(path: Path) -> str:
    h = hashlib.blake2b(digest_size=16)
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError as exc:
        warn(f"full hash failed on {path}: {exc}")
        return ""
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Subcommand: merge
# ---------------------------------------------------------------------------

def find_google_photos_roots(takeout_root: Path) -> List[Path]:
    """Return all 'Google Photos'-flavored folders under a Takeout extraction."""
    out: List[Path] = []
    if not takeout_root.is_dir():
        return out
    # Typical layout: <root>/Takeout/Google Photos/<albums...>
    for sub in takeout_root.rglob("*"):
        if sub.is_dir() and is_google_photos_root(sub.name):
            out.append(sub)
    return out


@dataclass
class MergeStats:
    sources_seen: int = 0
    files_linked: int = 0
    files_copied: int = 0
    files_skipped_identical: int = 0
    conflicts: int = 0
    errors: int = 0


def merge_one_file(
    src: Path, dst: Path, *, apply: bool, use_copy: bool, stats: MergeStats
) -> None:
    if dst.exists():
        try:
            if dst.samefile(src):
                stats.files_skipped_identical += 1
                return
        except OSError:
            pass
        try:
            same_size = dst.stat().st_size == src.stat().st_size
        except OSError:
            same_size = False
        if same_size and hash_probe(src) == hash_probe(dst):
            stats.files_skipped_identical += 1
            return
        # Same name, different content -> conflict; preserve both with a suffix.
        stem, suffix = dst.stem, dst.suffix
        n = 1
        while True:
            candidate = dst.with_name(f"{stem}__merge{n}{suffix}")
            if not candidate.exists():
                dst = candidate
                break
            n += 1
        stats.conflicts += 1

    if not apply:
        return

    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not use_copy:
            try:
                os.link(src, dst)
                stats.files_linked += 1
                return
            except OSError:
                # Cross-device or hardlinks unsupported -> copy.
                pass
        shutil.copy2(src, dst)
        stats.files_copied += 1
    except OSError as exc:
        warn(f"merge failed {src} -> {dst}: {exc}")
        stats.errors += 1


def cmd_merge(args: argparse.Namespace) -> int:
    target: Path = args.target.expanduser().resolve()
    sources: List[Path] = [s.expanduser().resolve() for s in args.sources]
    apply = bool(args.apply)
    use_copy = bool(args.copy)

    if not sources:
        err("merge: at least one --source is required")
        return 2
    for s in sources:
        if not s.is_dir():
            err(f"merge: source is not a directory: {s}")
            return 2
    if target in sources or any(target == s or target.is_relative_to(s) or s.is_relative_to(target) for s in sources):
        err(f"merge: target {target} overlaps with a source; choose a distinct target path")
        return 2

    if apply:
        target.mkdir(parents=True, exist_ok=True)

    stats = MergeStats()
    all_files: List[Tuple[Path, Path, Path]] = []  # (src_file, src_gp_root, rel)
    for source in sources:
        stats.sources_seen += 1
        gp_roots = find_google_photos_roots(source)
        if not gp_roots:
            # No Google Photos root found; treat the source itself as the root.
            gp_roots = [source]
        for gp_root in gp_roots:
            for dirpath, dirnames, filenames in os.walk(gp_root):
                # Don't descend into trash by default.
                dirnames[:] = [d for d in dirnames if not is_trash_folder(d)]
                for fn in filenames:
                    src_file = Path(dirpath) / fn
                    rel = src_file.relative_to(gp_root)
                    all_files.append((src_file, gp_root, rel))

    progress = ProgressBar(total=len(all_files), label="Merge", enabled=True)
    for src_file, _gp_root, rel in all_files:
        dst = target / rel
        merge_one_file(src_file, dst, apply=apply, use_copy=use_copy, stats=stats)
        progress.advance()
    progress.done()

    mode = "APPLIED" if apply else "DRY RUN"
    log("")
    log("=== merge summary ===")
    log(f"Mode: {mode}")
    log(f"Target: {target}")
    log(f"Sources: {len(sources)}")
    log(f"Files seen: {len(all_files)}")
    log(f"Linked: {stats.files_linked}")
    log(f"Copied: {stats.files_copied}")
    log(f"Skipped (identical): {stats.files_skipped_identical}")
    log(f"Conflicts (renamed): {stats.conflicts}")
    log(f"Errors: {stats.errors}")
    return 1 if stats.errors else 0


# ---------------------------------------------------------------------------
# Subcommand: fix
# ---------------------------------------------------------------------------

@dataclass
class MediaEntry:
    media_path: Path
    json_path: Path
    timestamp: int  # JSON photoTakenTime / creationTime (Unix seconds, UTC)
    is_video: bool
    geo_lat: Optional[float] = None
    geo_lon: Optional[float] = None
    geo_alt: Optional[float] = None
    set_date: bool = False
    set_gps: bool = False
    sync_fs: bool = True
    errors: List[str] = field(default_factory=list)

    # Reconciliation fields (set by decide_fix_actions):
    #   "skip"               - EXIF agrees with JSON, no EXIF write
    #   "write_missing"      - no valid EXIF date; write from JSON
    #   "overwrite_bogus"    - EXIF date is plainly bogus; overwrite with JSON
    #   "conflict_logged"    - EXIF disagrees but isn't bogus; leave EXIF alone
    #   "conflict_override"  - same disagreement, but --prefer-json-on-conflict
    decision: str = "skip"
    exif_date_text: Optional[str] = None  # raw EXIF wall-clock, for log lines
    mtime_ts: Optional[int] = None        # ts to set as filesystem mtime

    @property
    def has_geo(self) -> bool:
        return self.geo_lat is not None and self.geo_lon is not None

    def exif_time_text(self, tz: dt.tzinfo) -> str:
        """Format the timestamp for EXIF. Images: local time per `tz`. Videos:
        UTC (QuickTime spec — exiftool stores wall-clock UTC for the container)."""
        if self.is_video:
            return dt.datetime.fromtimestamp(self.timestamp, tz=dt.timezone.utc).strftime(
                "%Y:%m:%d %H:%M:%S"
            )
        return dt.datetime.fromtimestamp(self.timestamp, tz=tz).strftime("%Y:%m:%d %H:%M:%S")


@dataclass
class FixStats:
    json_seen: int = 0
    json_invalid: int = 0
    json_no_title: int = 0
    json_no_timestamp: int = 0
    json_non_media: int = 0
    json_duplicate_for_media: int = 0
    media_not_found: int = 0
    media_matched: int = 0


def discover_fix_entries(
    root: Path,
    include_trash: bool,
    show_progress: bool,
    album: Optional[str] = None,
) -> Tuple[List[MediaEntry], FixStats]:
    stats = FixStats()
    media_dir_cache: Dict[Path, List[Path]] = {}
    nfc_index_cache: Dict[Path, Dict[str, Path]] = {}
    chosen: Dict[Path, MediaEntry] = {}
    album_nfc = nfc(album) if album else None

    json_paths = [p for p in root.rglob("*.json") if include_trash or not in_trash(p, root)]
    progress = ProgressBar(total=len(json_paths), label="Discovery", enabled=show_progress)
    for json_path in json_paths:
        stats.json_seen += 1
        # Optional album filter: only process JSONs under <root>/<album>/...
        if album_nfc is not None:
            try:
                rel = json_path.relative_to(root)
            except ValueError:
                progress.advance()
                continue
            if not rel.parts or nfc(rel.parts[0]) != album_nfc:
                progress.advance()
                continue
        data = load_json(json_path)
        if data is None:
            stats.json_invalid += 1
            progress.advance()
            continue

        title = data.get("title")
        if not isinstance(title, str) or not title.strip():
            stats.json_no_title += 1
            progress.advance()
            continue

        ts = extract_timestamp(data)
        if ts is None:
            stats.json_no_timestamp += 1
            progress.advance()
            continue

        title_ext = Path(unquote(title)).suffix.lower()
        if title_ext not in MEDIA_EXTENSIONS:
            stats.json_non_media += 1
            progress.advance()
            continue

        media_path = associate_json_to_media(
            json_path, data, media_dir_cache, nfc_index_cache
        )
        if media_path is None:
            stats.media_not_found += 1
            progress.advance()
            continue

        if not include_trash and in_trash(media_path, root):
            progress.advance()
            continue

        geo_lat, geo_lon, geo_alt = extract_geo(data)
        candidate = MediaEntry(
            media_path=media_path,
            json_path=json_path,
            timestamp=ts,
            is_video=is_video_ext(media_path.suffix),
            geo_lat=geo_lat,
            geo_lon=geo_lon,
            geo_alt=geo_alt,
        )

        existing = chosen.get(media_path)
        if existing is None:
            chosen[media_path] = candidate
        else:
            existing_rank = (json_priority(existing.json_path), str(existing.json_path))
            candidate_rank = (json_priority(candidate.json_path), str(candidate.json_path))
            if candidate_rank < existing_rank:
                chosen[media_path] = candidate
            stats.json_duplicate_for_media += 1
        progress.advance()
    progress.done()

    entries = sorted(chosen.values(), key=lambda e: str(e.media_path))
    stats.media_matched = len(entries)
    return entries, stats


def decide_fix_actions(
    entries: List[MediaEntry],
    tz: dt.tzinfo,
    prefer_json_on_conflict: bool = False,
    chunk_size: int = 300,
    show_progress: bool = True,
) -> List[str]:
    """For each entry, decide what to do with EXIF + filesystem mtime.

    Branches:
      no EXIF date                -> write JSON,         mtime = JSON ts
      EXIF date is bogus          -> overwrite with JSON, mtime = JSON ts
      EXIF agrees with JSON       -> skip EXIF,          mtime = JSON ts
      EXIF disagrees, not bogus
        without --prefer-json     -> skip EXIF,          mtime = EXIF ts
        with    --prefer-json     -> overwrite with JSON, mtime = JSON ts

    GPS is independent: write the JSON's coords iff EXIF GPS is absent.
    """
    today = dt.date.today()
    read_errors: List[str] = []
    progress = ProgressBar(total=len(entries), label="EXIF scan", enabled=show_progress)
    for i in range(0, len(entries), chunk_size):
        chunk = entries[i : i + chunk_size]
        try:
            tags_by_file = exiftool_read([e.media_path for e in chunk])
        except RuntimeError as exc:
            message = f"EXIF read failed for chunk starting at {chunk[0].media_path}: {exc}"
            read_errors.append(message)
            for entry in chunk:
                entry.errors.append(message)
            progress.advance(len(chunk))
            continue
        for entry in chunk:
            tags = tags_by_file.get(str(entry.media_path), {})

            # Pick the most authoritative EXIF date field for this media type.
            if entry.is_video:
                raw = (
                    tags.get("CreateDate")
                    or tags.get("MediaCreateDate")
                    or tags.get("TrackCreateDate")
                )
            else:
                raw = tags.get("DateTimeOriginal") or tags.get("CreateDate")
            offset_raw = tags.get("OffsetTimeOriginal")

            exif_dt: Optional[dt.datetime] = None
            if has_valid_datetime(raw):
                exif_dt = parse_exif_datetime(raw)

            # GPS: unchanged rule — only fill when absent.
            entry.set_gps = entry.has_geo and not has_gps(tags)

            if exif_dt is None:
                # Case 1: no usable EXIF date.
                entry.decision = "write_missing"
                entry.set_date = True
                entry.exif_date_text = None
                entry.mtime_ts = entry.timestamp
                continue

            entry.exif_date_text = exif_dt.strftime("%Y-%m-%d %H:%M:%S")

            if is_bogus_exif_dt(exif_dt, today=today):
                # Case 2: bogus EXIF -> overwrite with JSON.
                entry.decision = "overwrite_bogus"
                entry.set_date = True
                entry.mtime_ts = entry.timestamp
                continue

            if exif_json_dates_agree(exif_dt, entry.timestamp, tz):
                # Case 3: EXIF agrees with JSON (same calendar day in tz, or
                # tz-midnight crossing). Keep EXIF; mtime can come from JSON
                # (they're the same day, so it doesn't matter much).
                entry.decision = "skip"
                entry.set_date = False
                entry.mtime_ts = entry.timestamp
                continue

            # Case 4: non-bogus disagreement.
            if prefer_json_on_conflict:
                entry.decision = "conflict_override"
                entry.set_date = True
                entry.mtime_ts = entry.timestamp
            else:
                entry.decision = "conflict_logged"
                entry.set_date = False
                # mtime tracks EXIF, since we're treating EXIF as the truth here.
                try:
                    entry.mtime_ts = exif_dt_to_ts(
                        exif_dt, tz_fallback=tz, offset_value=offset_raw
                    )
                except (OverflowError, OSError, ValueError):
                    entry.mtime_ts = entry.timestamp  # fall back to JSON
        progress.advance(len(chunk))
    progress.done()
    return read_errors


def build_exif_write_args(entry: MediaEntry, tz: dt.tzinfo) -> List[str]:
    """exiftool argument list (without the binary name or filepath).
    Returns the empty list when there is nothing to write."""
    args: List[str] = []
    if entry.set_date:
        text = entry.exif_time_text(tz)
        if entry.is_video:
            args.extend([
                f"-CreateDate={text}",
                f"-MediaCreateDate={text}",
                f"-TrackCreateDate={text}",
            ])
        else:
            args.extend([
                f"-DateTimeOriginal={text}",
                f"-CreateDate={text}",
            ])
    if entry.set_gps and entry.geo_lat is not None and entry.geo_lon is not None:
        # Write absolute values + N/S/E/W refs (more compatible across viewers
        # than signed scalars).
        lat = entry.geo_lat
        lon = entry.geo_lon
        args.extend([
            f"-GPSLatitude={abs(lat)}",
            f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
            f"-GPSLongitude={abs(lon)}",
            f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}",
        ])
        if entry.geo_alt is not None and abs(entry.geo_alt) > 1e-12:
            args.append(f"-GPSAltitude={abs(entry.geo_alt)}")
            args.append(f"-GPSAltitudeRef={'0' if entry.geo_alt >= 0 else '1'}")
    if not args:
        return []
    # Standard prefix flags. -n: numeric values. -P: preserve mtime (we set it
    # ourselves to the JSON timestamp afterwards). -m: ignore minor errors.
    return [
        "-overwrite_original_in_place", "-m", "-P", "-n",
        "-api", "QuickTimeUTC=1",
    ] + args


def apply_fix(
    entries: List[MediaEntry], tz: dt.tzinfo, show_progress: bool = True
) -> None:
    meta_entries = [e for e in entries if (e.set_date or e.set_gps)]
    session: Optional[ExiftoolSession] = None
    if meta_entries:
        try:
            session = ExiftoolSession()
        except OSError as exc:
            warn(f"could not start exiftool session: {exc}")

    progress = ProgressBar(total=len(meta_entries), label="Write EXIF", enabled=show_progress)
    try:
        for entry in meta_entries:
            args = build_exif_write_args(entry, tz)
            if not args:
                progress.advance()
                continue
            if session is not None:
                try:
                    rc, out = session.execute(args, str(entry.media_path))
                    if rc != 0:
                        entry.errors.append((out or "exiftool write failed").strip())
                except RuntimeError as exc:
                    entry.errors.append(str(exc))
                    # Session is dead — fall back to per-file subprocess for the rest.
                    session.close()
                    session = None
            else:
                proc = subprocess.run(
                    ["exiftool", *args, str(entry.media_path)],
                    check=False, capture_output=True, text=True,
                )
                if proc.returncode != 0:
                    entry.errors.append(
                        (proc.stderr or proc.stdout or "exiftool write failed").strip()
                    )
            progress.advance()
    finally:
        if session is not None:
            session.close()
    progress.done()

    # mtime sync is independent of EXIF write success: the user wants
    # filesystem dates to reflect capture time regardless of whether the
    # file is structurally writable by exiftool. The mtime source is decided
    # per file in decide_fix_actions (JSON ts in most cases, EXIF ts when we
    # chose to trust EXIF over JSON in a non-bogus conflict).
    fs_entries = [e for e in entries if e.sync_fs and e.mtime_ts is not None]
    progress = ProgressBar(total=len(fs_entries), label="Sync mtime", enabled=show_progress)
    for entry in fs_entries:
        sync_filesystem_dates(entry.media_path, entry.mtime_ts)  # type: ignore[arg-type]
        progress.advance()
    progress.done()


def cmd_fix(args: argparse.Namespace) -> int:
    require_exiftool()
    setup_file_logging(getattr(args, "log_file", None))
    root: Path = args.root.expanduser().resolve()
    if not root.is_dir():
        err(f"fix: root is not a directory: {root}")
        return 2
    apply = bool(args.apply)
    tz = resolve_timezone(getattr(args, "timezone", None))
    prefer_json = bool(getattr(args, "prefer_json_on_conflict", False))

    entries, stats = discover_fix_entries(
        root=root,
        include_trash=args.include_trash,
        show_progress=True,
        album=getattr(args, "album", None),
    )
    read_errors = decide_fix_actions(
        entries, tz=tz, prefer_json_on_conflict=prefer_json, show_progress=True
    )

    # Emit per-file conflict lines in BOTH dry-run and apply, before any writes.
    # Format is plain and grep-able.
    for entry in entries:
        if entry.decision not in {"conflict_logged", "conflict_override"}:
            continue
        json_date = dt.datetime.fromtimestamp(entry.timestamp, tz=tz).strftime("%Y-%m-%d")
        exif_date = (entry.exif_date_text or "?").split(" ", 1)[0]
        tag = "OVERRIDE" if entry.decision == "conflict_override" else "CONFLICT"
        log(f"{tag} {entry.media_path}  EXIF={exif_date}  JSON={json_date}")

    if apply:
        apply_fix(entries, tz=tz, show_progress=True)

    # Per-decision counts.
    by_decision: Counter[str] = Counter(e.decision for e in entries)
    set_date_n = sum(1 for e in entries if e.set_date)
    set_gps_n = sum(1 for e in entries if e.set_gps)
    err_n = sum(1 for e in entries if e.errors)
    fs_sync_n = sum(1 for e in entries if e.sync_fs and e.mtime_ts is not None)

    log("")
    log("=== fix summary ===")
    log(f"Mode: {'APPLIED' if apply else 'DRY RUN'}")
    log(f"Root: {root}")
    log(f"JSON seen: {stats.json_seen}")
    log(f"JSON invalid / no title / no timestamp / non-media: "
        f"{stats.json_invalid} / {stats.json_no_title} / {stats.json_no_timestamp} / {stats.json_non_media}")
    log(f"Duplicate JSONs for same media: {stats.json_duplicate_for_media}")
    log(f"Media matched: {stats.media_matched}")
    log(f"Media not found from JSON: {stats.media_not_found}")
    log(f"EXIF/JSON reconciliation:")
    log(f"  skip (EXIF agrees):       {by_decision.get('skip', 0)}")
    log(f"  write_missing:            {by_decision.get('write_missing', 0)}")
    log(f"  overwrite_bogus:          {by_decision.get('overwrite_bogus', 0)}")
    log(f"  conflict_logged:          {by_decision.get('conflict_logged', 0)}")
    log(f"  conflict_override:        {by_decision.get('conflict_override', 0)}")
    log(f"Planned EXIF date writes: {set_date_n}")
    log(f"Planned EXIF GPS writes:  {set_gps_n}")
    log(f"Filesystem mtime sync:    {fs_sync_n}")
    log(f"Errors:                   {err_n}")
    if read_errors:
        log("Read errors:")
        for m in read_errors:
            log(f"  - {m}")
    return 1 if err_n else 0


# ---------------------------------------------------------------------------
# Subcommand: dedup
# ---------------------------------------------------------------------------

@dataclass
class DedupStats:
    files_scanned: int = 0
    groups: int = 0
    deletable: int = 0
    deleted: int = 0
    skipped_all_year: int = 0
    conflicts_all_album: int = 0
    errors: int = 0


def find_sidecar_jsons(media_path: Path) -> List[Path]:
    """Return JSON sidecars in the same folder whose derived base matches the media name."""
    parent = media_path.parent
    name = media_path.name
    out: List[Path] = []
    try:
        for child in parent.iterdir():
            if not (child.is_file() and child.suffix.lower() == ".json"):
                continue
            derived = derived_media_base_from_json_name(child.name)
            if derived == name:
                out.append(child)
    except OSError:
        pass
    return out


def is_under_year_folder(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return any(is_year_folder(part) for part in rel.parts[:-1])


def cmd_dedup(args: argparse.Namespace) -> int:
    root: Path = args.root.expanduser().resolve()
    if not root.is_dir():
        err(f"dedup: root is not a directory: {root}")
        return 2
    apply = bool(args.apply)
    delete_jsons = not args.keep_jsons

    stats = DedupStats()
    files = list(iter_media(root, include_trash=args.include_trash))
    stats.files_scanned = len(files)

    # Group by size first; only hash within size groups of 2+.
    by_size: Dict[int, List[Path]] = defaultdict(list)
    for f in files:
        try:
            by_size[f.stat().st_size].append(f)
        except OSError:
            continue

    candidate_groups: List[List[Path]] = [g for g in by_size.values() if len(g) >= 2]

    # Two-stage hashing.
    dup_groups: List[List[Path]] = []
    progress = ProgressBar(
        total=sum(len(g) for g in candidate_groups), label="Hashing", enabled=True
    )
    for size_group in candidate_groups:
        by_probe: Dict[str, List[Path]] = defaultdict(list)
        for f in size_group:
            by_probe[hash_probe(f)].append(f)
            progress.advance()
        for probe_group in by_probe.values():
            if len(probe_group) < 2:
                continue
            by_full: Dict[str, List[Path]] = defaultdict(list)
            for f in probe_group:
                by_full[hash_full(f)].append(f)
            for full_group in by_full.values():
                if len(full_group) >= 2:
                    dup_groups.append(full_group)
    progress.done()

    stats.groups = len(dup_groups)

    to_delete: List[Tuple[Path, Path]] = []
    for group in dup_groups:
        in_year = [p for p in group if is_under_year_folder(p, root)]
        in_album = [p for p in group if not is_under_year_folder(p, root)]
        if in_year and in_album:
            keeper = in_album[0]
            for victim in in_year:
                to_delete.append((victim, keeper))
        elif in_year and not in_album:
            stats.skipped_all_year += 1
        else:
            stats.conflicts_all_album += 1

    stats.deletable = len(to_delete)

    # Print a compact per-folder report.
    by_folder: Dict[Path, int] = defaultdict(int)
    for victim, _keeper in to_delete:
        by_folder[victim.parent] += 1
    log("")
    log("=== dedup plan ===")
    for folder, n in sorted(by_folder.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        log(f"  {n:>5d}  {folder}")
    log(f"Groups with duplicates: {stats.groups}")
    log(f"All-year groups skipped: {stats.skipped_all_year}")
    log(f"All-album conflicts (manual review): {stats.conflicts_all_album}")
    log(f"To delete: {stats.deletable}")

    if apply:
        for victim, _keeper in to_delete:
            try:
                victim.unlink()
                stats.deleted += 1
            except OSError as exc:
                warn(f"delete failed {victim}: {exc}")
                stats.errors += 1
                continue
            if delete_jsons:
                for sidecar in find_sidecar_jsons(victim):
                    try:
                        sidecar.unlink()
                    except OSError as exc:
                        warn(f"sidecar delete failed {sidecar}: {exc}")

    log("")
    log("=== dedup summary ===")
    log(f"Mode: {'APPLIED' if apply else 'DRY RUN'}")
    log(f"Files scanned: {stats.files_scanned}")
    log(f"Duplicate groups: {stats.groups}")
    log(f"Deleted: {stats.deleted}")
    log(f"Errors: {stats.errors}")
    return 1 if stats.errors else 0


# ---------------------------------------------------------------------------
# Subcommand: rename
# ---------------------------------------------------------------------------

def parse_filename_timestamp(stem: str) -> Optional[int]:
    m = EMBEDDED_DT_RE.search(stem)
    if not m:
        return None
    try:
        year, month, day, hour, minute, second = (int(x) for x in m.groups())
        dt_val = dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.timezone.utc)
        return int(dt_val.timestamp())
    except ValueError:
        return None


def cmd_rename(args: argparse.Namespace) -> int:
    require_exiftool()
    setup_file_logging(getattr(args, "log_file", None))
    root: Path = args.root.expanduser().resolve()
    if not root.is_dir():
        err(f"rename: root is not a directory: {root}")
        return 2
    apply = bool(args.apply)
    tz = resolve_timezone(getattr(args, "timezone", None))

    files = list(iter_media(root, include_trash=args.include_trash))

    # Build JSON timestamp index by scanning sidecars.
    json_paths = [p for p in root.rglob("*.json") if args.include_trash or not in_trash(p, root)]
    media_dir_cache: Dict[Path, List[Path]] = {}
    nfc_index_cache: Dict[Path, Dict[str, Path]] = {}
    json_ts_by_media: Dict[Path, int] = {}
    for json_path in json_paths:
        data = load_json(json_path)
        if not data:
            continue
        ts = extract_timestamp(data)
        if ts is None:
            continue
        media = associate_json_to_media(json_path, data, media_dir_cache, nfc_index_cache)
        if media is None:
            continue
        # First-write wins; json_priority picks the best one.
        if media not in json_ts_by_media or json_priority(json_path) < json_priority(
            json_ts_by_media.get(media, json_path)  # type: ignore[arg-type]
        ):
            json_ts_by_media[media] = ts

    # Fall back to EXIF for files without JSON.
    no_json = [f for f in files if f not in json_ts_by_media]
    exif_ts_by_media: Dict[Path, int] = {}
    if no_json:
        for i in range(0, len(no_json), 300):
            chunk = no_json[i : i + 300]
            try:
                tags_by_file = exiftool_read(chunk)
            except RuntimeError:
                continue
            for media in chunk:
                tags = tags_by_file.get(str(media), {})
                value = (
                    tags.get("DateTimeOriginal")
                    or tags.get("CreateDate")
                    or tags.get("MediaCreateDate")
                )
                if not isinstance(value, str) or ZERO_DATE_REGEX.match(value):
                    continue
                m = re.match(
                    r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", value
                )
                if not m:
                    continue
                try:
                    parts = [int(x) for x in m.groups()]
                    dt_val = dt.datetime(*parts, tzinfo=dt.timezone.utc)
                    exif_ts_by_media[media] = int(dt_val.timestamp())
                except ValueError:
                    continue

    # Plan renames per directory.
    renames: List[Tuple[Path, Path]] = []
    by_dir: Dict[Path, List[Path]] = defaultdict(list)
    for f in files:
        by_dir[f.parent].append(f)

    for directory, dir_files in by_dir.items():
        existing_names: set[str] = set()
        try:
            existing_names = {child.name for child in directory.iterdir() if child.is_file()}
        except OSError:
            pass
        moving_names = {f.name for f in dir_files}
        reserved = set(existing_names - moving_names)

        # Sort by chosen timestamp for stable per-dir ordering.
        def pick_ts(p: Path) -> Optional[int]:
            ts = json_ts_by_media.get(p) or exif_ts_by_media.get(p)
            if ts is None:
                ts = parse_filename_timestamp(p.stem)
            if ts is None:
                try:
                    ts = int(p.stat().st_mtime)
                except OSError:
                    return None
            return ts

        for f in sorted(dir_files, key=lambda x: (pick_ts(x) or 0, x.name)):
            if CANONICAL_NAME_RE.match(f.stem):
                continue
            ts = pick_ts(f)
            if ts is None:
                continue
            base = fmt_canonical(ts, tz=tz) + f.suffix
            final = base
            if final in reserved and final != f.name:
                stem = Path(base).stem
                ext = Path(base).suffix
                k = 1
                while True:
                    candidate = f"{stem}_{k:02d}{ext}"
                    if candidate not in reserved:
                        final = candidate
                        break
                    k += 1
            reserved.add(final)
            target = directory / final
            if target == f:
                continue
            renames.append((f, target))

    log("")
    log("=== rename plan ===")
    for src, dst in renames[:20]:
        log(f"  {src.name} -> {dst.name}")
    if len(renames) > 20:
        log(f"  ... and {len(renames) - 20} more")
    log(f"Total renames: {len(renames)}")

    if not apply:
        log("Mode: DRY RUN")
        return 0

    # Two-phase rename to avoid in-directory collisions. We also rename
    # the JSON sidecar(s) alongside so they stay paired with their media.
    token = uuid.uuid4().hex[:8]
    temps: List[Tuple[Path, Path, List[Tuple[Path, Path]]]] = []
    for i, (src, dst) in enumerate(renames):
        # Resolve sidecar pairs while we still have the old name on disk.
        sidecar_pairs: List[Tuple[Path, Path]] = []
        for sidecar in find_sidecar_jsons(src):
            new_sidecar_name = sidecar.name.replace(src.name, dst.name, 1)
            sidecar_pairs.append((sidecar, dst.parent / new_sidecar_name))
        tmp = src.with_name(f".takeout_tmp_{token}_{i:08d}{src.suffix}")
        try:
            src.rename(tmp)
            temps.append((tmp, dst, sidecar_pairs))
        except OSError as exc:
            warn(f"rename phase 1 failed {src}: {exc}")
    errors = 0
    for tmp, dst, sidecar_pairs in temps:
        try:
            tmp.rename(dst)
        except OSError as exc:
            warn(f"rename phase 2 failed {tmp} -> {dst}: {exc}")
            errors += 1
            continue
        for sidecar, new_sidecar in sidecar_pairs:
            if sidecar == new_sidecar or not sidecar.exists():
                continue
            try:
                sidecar.rename(new_sidecar)
            except OSError as exc:
                warn(f"sidecar rename failed {sidecar} -> {new_sidecar}: {exc}")
                errors += 1
    log("Mode: APPLIED")
    log(f"Errors: {errors}")
    return 1 if errors else 0


# ---------------------------------------------------------------------------
# Subcommand: all
# ---------------------------------------------------------------------------

def cmd_all(args: argparse.Namespace) -> int:
    setup_file_logging(getattr(args, "log_file", None))

    # 1. merge
    merge_args = argparse.Namespace(
        target=args.target, sources=args.sources, apply=args.apply, copy=args.copy,
    )
    rc = cmd_merge(merge_args)
    if rc != 0 and not args.continue_on_error:
        return rc

    # 2. fix
    fix_args = argparse.Namespace(
        root=args.target, apply=args.apply,
        include_trash=args.include_trash,
        timezone=getattr(args, "timezone", None),
        album=getattr(args, "album", None),
        log_file=None,  # already set up at the `all` level
        prefer_json_on_conflict=getattr(args, "prefer_json_on_conflict", False),
    )
    rc = cmd_fix(fix_args)
    if rc != 0 and not args.continue_on_error:
        return rc

    # 3. dedup (optional)
    if args.dedup:
        dedup_args = argparse.Namespace(
            root=args.target, apply=args.apply,
            include_trash=args.include_trash, keep_jsons=args.keep_jsons,
        )
        rc = cmd_dedup(dedup_args)
        if rc != 0 and not args.continue_on_error:
            return rc

    # 4. rename (optional)
    if args.rename:
        rename_args = argparse.Namespace(
            root=args.target, apply=args.apply,
            include_trash=args.include_trash,
            timezone=getattr(args, "timezone", None),
            log_file=None,
        )
        rc = cmd_rename(rename_args)
        if rc != 0 and not args.continue_on_error:
            return rc

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="takeout_cleaner",
        description="Clean up Google Takeout photo/video archives.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # merge ----------------------------------------------------------------
    m = sub.add_parser("merge", help="Merge multiple Takeout extractions into one tree.")
    m.add_argument("--target", type=Path, required=True, help="Output directory (must not overlap sources).")
    m.add_argument("--source", dest="sources", type=Path, action="append", default=[],
                   help="Source extraction directory (repeat for each).")
    m.add_argument("--apply", action="store_true", help="Actually link/copy files (default: dry-run).")
    m.add_argument("--copy", action="store_true", help="Use copy instead of hardlink (slower, uses 2x space).")
    m.set_defaults(func=cmd_merge)

    # fix ------------------------------------------------------------------
    f = sub.add_parser("fix", help="Restore EXIF date/GPS from JSON sidecars and sync filesystem mtime.")
    f.add_argument("--root", type=Path, required=True, help="Photos root to process.")
    f.add_argument("--apply", action="store_true", help="Actually modify files (default: dry-run).")
    f.add_argument("--include-trash", action="store_true",
                   help="Include Trash/Bin/Archive subfolders (default: skip).")
    f.add_argument("--timezone", metavar="TZ",
                   help='IANA timezone for image EXIF dates (e.g. "Europe/Paris"). '
                        "Videos are always written in UTC. Default: system local tz.")
    f.add_argument("--album", metavar="NAME",
                   help="Only process the named top-level album (NFC-compared). "
                        "Useful for testing on one album before unleashing the tool.")
    f.add_argument("--log-file", type=Path, metavar="PATH",
                   help="Also write a detailed log to this file.")
    f.add_argument("--prefer-json-on-conflict", action="store_true",
                   help="When EXIF disagrees with JSON and EXIF is not bogus, "
                        "overwrite EXIF with JSON instead of leaving it alone. "
                        "Default: leave EXIF alone and emit a CONFLICT log line.")
    f.set_defaults(func=cmd_fix)

    # dedup ----------------------------------------------------------------
    d = sub.add_parser("dedup", help="Remove duplicates that live in year folders, keeping album copies.")
    d.add_argument("--root", type=Path, required=True, help="Photos root to process.")
    d.add_argument("--apply", action="store_true", help="Actually delete duplicates (default: dry-run).")
    d.add_argument("--include-trash", action="store_true", help="Include Trash/Bin/Archive (default: skip).")
    d.add_argument("--keep-jsons", action="store_true",
                   help="Keep JSON sidecars alongside deleted media (default: delete them too).")
    d.set_defaults(func=cmd_dedup)

    # rename ---------------------------------------------------------------
    r = sub.add_parser("rename", help="Rename media to YYYY-MM-DD_HH-mm-ss.ext.")
    r.add_argument("--root", type=Path, required=True, help="Photos root to process.")
    r.add_argument("--apply", action="store_true", help="Actually rename files (default: dry-run).")
    r.add_argument("--include-trash", action="store_true", help="Include Trash/Bin/Archive (default: skip).")
    r.add_argument("--timezone", metavar="TZ",
                   help='IANA timezone for filename formatting (e.g. "Europe/Paris"). '
                        "Default: system local tz.")
    r.add_argument("--log-file", type=Path, metavar="PATH",
                   help="Also write a detailed log to this file.")
    r.set_defaults(func=cmd_rename)

    # all ------------------------------------------------------------------
    a = sub.add_parser("all", help="Run merge -> fix [-> dedup] [-> rename] in order.")
    a.add_argument("--target", type=Path, required=True, help="Output directory (must not overlap sources).")
    a.add_argument("--source", dest="sources", type=Path, action="append", default=[],
                   help="Source extraction directory (repeat for each).")
    a.add_argument("--apply", action="store_true", help="Actually modify files (default: dry-run).")
    a.add_argument("--copy", action="store_true", help="Copy instead of hardlink during merge.")
    a.add_argument("--include-trash", action="store_true", help="Include Trash/Bin/Archive (default: skip).")
    a.add_argument("--dedup", action="store_true", help="Also run dedup after fix.")
    a.add_argument("--keep-jsons", action="store_true", help="Keep JSON sidecars when dedup deletes media.")
    a.add_argument("--rename", action="store_true", help="Also run rename at the end.")
    a.add_argument("--timezone", metavar="TZ",
                   help='IANA timezone for image EXIF dates + rename. Default: system local tz.')
    a.add_argument("--album", metavar="NAME",
                   help="Only fix the named top-level album (affects fix, not merge).")
    a.add_argument("--log-file", type=Path, metavar="PATH",
                   help="Also write a detailed log to this file.")
    a.add_argument("--prefer-json-on-conflict", action="store_true",
                   help="When EXIF disagrees with JSON and EXIF is not bogus, "
                        "overwrite EXIF with JSON instead of logging a conflict.")
    a.add_argument("--continue-on-error", action="store_true",
                   help="Continue to the next phase even if a phase reports errors.")
    a.set_defaults(func=cmd_all)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    check_windows_long_paths()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
