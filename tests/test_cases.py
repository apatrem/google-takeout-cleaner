#!/usr/bin/env python3
"""Granular tests for takeout_cleaner.py — one tempdir per test.

Run: python tests/test_cases.py

Each test_* function takes a Path to a fresh empty tempdir, builds the smallest
fixture that exercises one specific behavior, invokes one subcommand, and
asserts on the result. Failures print the failing assert and the tool's stdout
/ stderr for fast triage.

Covers branches the end-to-end smoke test in test_smoke.py doesn't hit:
  - each fix decision (write_missing / skip / overwrite_bogus / conflict_logged
    / conflict_override) in isolation
  - bogus heuristic: year-range and sentinel cases
  - midnight-crossing tolerance for the agree branch
  - GPS write / skip / 0,0-treated-as-absent / negative-lat → S ref
  - dedup year-vs-album, sidecar deletion, --keep-jsons
  - --album filter restricting scope
  - --include-trash flag
  - dry-run safety (no --apply → no changes)
  - OffsetTimeOriginal honored for conflict-case mtime
  - rename: already-canonical names skipped, sidecar paired
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Iterable

# Minimal 1x1 white JPEG (333 bytes) — exiftool can write to it.
_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAHwAA"
    "AQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQR"
    "BRIhMUEGE1FhByJxFDKBkUIjobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNE"
    "RUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6g4SFhoeIiYqSk5SVlpeYmZqio6Slpqeo"
    "qaqys7S1tre4ubrCw8TFxsfIycrS09TV1tfY2drh4uPk5ebn6Onq8fLz9PX29/j5+v/aAAgB"
    "AQAAPwD70P/Z"
)
JPEG_BYTES = base64.b64decode(_JPEG_B64)

# Repo root, computed once.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Reference timestamps used across tests (all UTC).
TS_2019_05_01_NOON = 1556712000  # 2019-05-01 12:00:00 UTC
TS_2019_07_15_0930 = 1563183000  # 2019-07-15 09:30:00 UTC
TS_2020_03_10_1400 = 1583848800  # 2020-03-10 14:00:00 UTC


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def write_jpeg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(JPEG_BYTES)


def write_video_placeholder(path: Path) -> None:
    """Write a stub byte sequence with a .mp4 extension. Not a real video —
    exiftool won't be able to write to it. Use only for paths that don't need
    a successful EXIF write (e.g. dry-run detection)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00")


def set_exif(path: Path, **fields: str) -> None:
    args = ["exiftool", "-overwrite_original", "-m"]
    for tag, value in fields.items():
        args.append(f"-{tag}={value}")
    args.append(str(path))
    subprocess.run(args, check=True, capture_output=True, text=True)


def write_sidecar(
    media: Path,
    ts: int,
    *,
    lat: float = 0.0,
    lon: float = 0.0,
    title: str | None = None,
) -> None:
    sidecar = media.with_name(media.name + ".supplemental-metadata.json")
    sidecar.write_text(json.dumps({
        "title": title if title is not None else media.name,
        "photoTakenTime": {"timestamp": str(ts)},
        "creationTime": {"timestamp": str(ts)},
        "geoData": {"latitude": lat, "longitude": lon, "altitude": 0.0},
    }))


def read_exif(path: Path, *fields: str) -> dict[str, str]:
    """Return a dict {field: value} for the requested EXIF fields. Missing
    fields are absent from the dict."""
    args = ["exiftool", "-s", "-n"]
    for f in fields:
        args.append(f"-{f}")
    args.append(str(path))
    proc = subprocess.run(args, check=True, capture_output=True, text=True)
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def run_tool(*args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(REPO_ROOT / "takeout_cleaner.py"), *args]
    return subprocess.run(cmd, check=False, capture_output=True, text=True)


def assert_tool_ok(p: subprocess.CompletedProcess) -> None:
    if p.returncode != 0:
        raise AssertionError(
            f"tool exited {p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}"
        )


# ---------------------------------------------------------------------------
# Tests — fix decisions
# ---------------------------------------------------------------------------

def test_write_missing_writes_json_exif(tmp: Path) -> None:
    """No EXIF date present → write the JSON timestamp into EXIF."""
    f = tmp / "a.jpg"
    write_jpeg(f)
    write_sidecar(f, TS_2019_05_01_NOON)

    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)

    tags = read_exif(f, "DateTimeOriginal")
    assert tags.get("DateTimeOriginal") == "2019:05:01 12:00:00", tags


def test_agree_same_day_keeps_exif(tmp: Path) -> None:
    """EXIF and JSON refer to the same calendar day → skip EXIF write."""
    f = tmp / "a.jpg"
    write_jpeg(f)
    set_exif(f, DateTimeOriginal="2019:05:01 14:00:00", CreateDate="2019:05:01 14:00:00")
    write_sidecar(f, TS_2019_05_01_NOON)

    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)
    tags = read_exif(f, "DateTimeOriginal")
    assert tags.get("DateTimeOriginal") == "2019:05:01 14:00:00", tags


def test_agree_midnight_crossing_keeps_exif(tmp: Path) -> None:
    """EXIF is May 2 at 02:30, JSON is May 1 22:00 UTC → adjacent days but
    EXIF is within ±3 h of midnight, so still "agree"."""
    f = tmp / "a.jpg"
    write_jpeg(f)
    set_exif(f, DateTimeOriginal="2019:05:02 02:30:00", CreateDate="2019:05:02 02:30:00")
    # JSON: 2019-05-01 22:00:00 UTC = 1556748000
    write_sidecar(f, 1556748000)

    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)
    tags = read_exif(f, "DateTimeOriginal")
    assert tags.get("DateTimeOriginal") == "2019:05:02 02:30:00", tags


def test_bogus_1970_overwritten(tmp: Path) -> None:
    """EXIF sentinel 1970-01-01 00:00:00 → overwrite with JSON."""
    f = tmp / "a.jpg"
    write_jpeg(f)
    set_exif(f, DateTimeOriginal="1970:01:01 00:00:00", CreateDate="1970:01:01 00:00:00")
    write_sidecar(f, TS_2019_05_01_NOON)

    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)
    tags = read_exif(f, "DateTimeOriginal")
    assert tags.get("DateTimeOriginal") == "2019:05:01 12:00:00", tags


def test_bogus_year_too_old_overwritten(tmp: Path) -> None:
    """Year < 1995 → bogus, overwrite."""
    f = tmp / "a.jpg"
    write_jpeg(f)
    set_exif(f, DateTimeOriginal="1989:06:15 10:00:00", CreateDate="1989:06:15 10:00:00")
    write_sidecar(f, TS_2019_05_01_NOON)
    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)
    tags = read_exif(f, "DateTimeOriginal")
    assert tags.get("DateTimeOriginal") == "2019:05:01 12:00:00", tags


def test_bogus_year_too_future_overwritten(tmp: Path) -> None:
    """Year > current + 1 → bogus, overwrite."""
    f = tmp / "a.jpg"
    write_jpeg(f)
    set_exif(f, DateTimeOriginal="2099:01:01 00:00:00", CreateDate="2099:01:01 00:00:00")
    write_sidecar(f, TS_2019_05_01_NOON)
    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)
    tags = read_exif(f, "DateTimeOriginal")
    assert tags.get("DateTimeOriginal") == "2019:05:01 12:00:00", tags


def test_conflict_default_preserves_exif(tmp: Path) -> None:
    """Disagreeing EXIF, not bogus → keep EXIF, log CONFLICT."""
    f = tmp / "a.jpg"
    write_jpeg(f)
    set_exif(f, DateTimeOriginal="2015:03:10 14:20:00", CreateDate="2015:03:10 14:20:00")
    write_sidecar(f, TS_2019_05_01_NOON)

    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)
    tags = read_exif(f, "DateTimeOriginal")
    assert tags.get("DateTimeOriginal") == "2015:03:10 14:20:00", tags
    assert "CONFLICT" in p.stdout, "expected CONFLICT line in stdout"


def test_conflict_override_with_flag_writes_json(tmp: Path) -> None:
    """--prefer-json-on-conflict → overwrite EXIF, log OVERRIDE."""
    f = tmp / "a.jpg"
    write_jpeg(f)
    set_exif(f, DateTimeOriginal="2015:03:10 14:20:00", CreateDate="2015:03:10 14:20:00")
    write_sidecar(f, TS_2019_05_01_NOON)

    p = run_tool(
        "fix", "--root", str(tmp), "--timezone", "UTC",
        "--prefer-json-on-conflict", "--apply",
    )
    assert_tool_ok(p)
    tags = read_exif(f, "DateTimeOriginal")
    assert tags.get("DateTimeOriginal") == "2019:05:01 12:00:00", tags
    assert "OVERRIDE" in p.stdout, "expected OVERRIDE line in stdout"


def test_conflict_mtime_follows_exif(tmp: Path) -> None:
    """In conflict_logged, mtime should reflect EXIF (since we trust EXIF)."""
    f = tmp / "a.jpg"
    write_jpeg(f)
    set_exif(f, DateTimeOriginal="2015:03:10 14:20:00", CreateDate="2015:03:10 14:20:00")
    write_sidecar(f, TS_2019_05_01_NOON)

    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)
    # 2015-03-10 14:20:00 UTC
    expected = int(dt.datetime(2015, 3, 10, 14, 20, 0, tzinfo=dt.timezone.utc).timestamp())
    actual = int(f.stat().st_mtime)
    assert actual == expected, f"mtime {actual} != EXIF ts {expected}"


def test_agree_mtime_uses_json(tmp: Path) -> None:
    """In the agree case, mtime should still come from JSON (single source of truth)."""
    f = tmp / "a.jpg"
    write_jpeg(f)
    set_exif(f, DateTimeOriginal="2019:05:01 14:00:00", CreateDate="2019:05:01 14:00:00")
    write_sidecar(f, TS_2019_05_01_NOON)

    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)
    actual = int(f.stat().st_mtime)
    assert actual == TS_2019_05_01_NOON, f"mtime {actual} != JSON ts {TS_2019_05_01_NOON}"


# ---------------------------------------------------------------------------
# Tests — GPS
# ---------------------------------------------------------------------------

def test_gps_written_when_absent(tmp: Path) -> None:
    """File with no GPS and JSON GPS available → GPS + Ref tags written."""
    f = tmp / "a.jpg"
    write_jpeg(f)
    # No GPS in EXIF; JSON has Paris coords.
    write_sidecar(f, TS_2019_05_01_NOON, lat=48.8566, lon=2.3522)

    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)
    tags = read_exif(f, "GPSLatitude", "GPSLongitude", "GPSLatitudeRef", "GPSLongitudeRef")
    assert tags.get("GPSLatitudeRef") in ("N", "North"), tags
    assert tags.get("GPSLongitudeRef") in ("E", "East"), tags
    # -n flag gives numeric; lat ~ 48.86
    lat = float(tags.get("GPSLatitude", "0"))
    assert abs(lat - 48.8566) < 0.001, tags


def test_gps_skipped_when_present(tmp: Path) -> None:
    """File with EXIF GPS already → don't overwrite."""
    f = tmp / "a.jpg"
    write_jpeg(f)
    # Pre-set EXIF GPS to a different location (London).  With -n, exiftool
    # returns the signed combination of magnitude + Ref, so the read-back
    # values include the sign from GPSLongitudeRef=W.
    set_exif(
        f,
        GPSLatitude="51.5",
        GPSLatitudeRef="N",
        GPSLongitude="0.1",
        GPSLongitudeRef="W",
        DateTimeOriginal="2019:05:01 14:00:00",
    )
    # JSON has Paris coords — should NOT overwrite the London EXIF.
    write_sidecar(f, TS_2019_05_01_NOON, lat=48.8566, lon=2.3522)

    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)
    tags = read_exif(f, "GPSLatitude", "GPSLongitude")
    lat = float(tags.get("GPSLatitude", "0"))
    lon = float(tags.get("GPSLongitude", "0"))
    # London-ish, not Paris. -n returns signed (W -> negative lon).
    assert abs(lat - 51.5) < 0.01, f"GPS overwritten: {tags}"
    assert abs(lon - (-0.1)) < 0.01, f"GPS overwritten: {tags}"


def test_gps_zero_treated_as_absent(tmp: Path) -> None:
    """JSON GPS at (0, 0) → treated as absent, no GPS write."""
    f = tmp / "a.jpg"
    write_jpeg(f)
    write_sidecar(f, TS_2019_05_01_NOON, lat=0.0, lon=0.0)

    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)
    tags = read_exif(f, "GPSLatitude", "GPSLongitude")
    assert "GPSLatitude" not in tags, f"unexpected GPS write: {tags}"


def test_gps_negative_latitude_writes_south_ref(tmp: Path) -> None:
    """Negative lat in JSON → store absolute value + S ref. With -n, exiftool
    returns the signed combination, so the lat reads back as negative."""
    f = tmp / "a.jpg"
    write_jpeg(f)
    # Sydney: -33.86, 151.21
    write_sidecar(f, TS_2019_05_01_NOON, lat=-33.86, lon=151.21)

    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)
    tags = read_exif(f, "GPSLatitude", "GPSLatitudeRef", "GPSLongitudeRef")
    assert tags.get("GPSLatitudeRef") in ("S", "South"), tags
    assert tags.get("GPSLongitudeRef") in ("E", "East"), tags
    lat = float(tags.get("GPSLatitude", "0"))
    assert abs(lat - (-33.86)) < 0.01, f"signed lat from refs+magnitude: {tags}"


# ---------------------------------------------------------------------------
# Tests — dedup
# ---------------------------------------------------------------------------

def test_dedup_deletes_year_keeps_album(tmp: Path) -> None:
    """Same content in 'Photos from 2019' and an album folder → year deleted."""
    year = tmp / "Photos from 2019"
    album = tmp / "Vacation 2019"
    album.mkdir(parents=True)
    write_jpeg(year / "x.jpg")
    shutil.copy2(year / "x.jpg", album / "x.jpg")

    p = run_tool("dedup", "--root", str(tmp), "--apply")
    assert_tool_ok(p)
    assert not (year / "x.jpg").exists(), "year copy should be deleted"
    assert (album / "x.jpg").exists(), "album copy should be kept"


def test_dedup_deletes_sidecar_alongside_media(tmp: Path) -> None:
    """Sidecar JSON beside a deleted media is removed too (default)."""
    year = tmp / "Photos from 2019"
    album = tmp / "Vacation 2019"
    album.mkdir(parents=True)
    write_jpeg(year / "x.jpg")
    write_sidecar(year / "x.jpg", TS_2019_05_01_NOON)
    shutil.copy2(year / "x.jpg", album / "x.jpg")
    write_sidecar(album / "x.jpg", TS_2019_05_01_NOON)

    p = run_tool("dedup", "--root", str(tmp), "--apply")
    assert_tool_ok(p)
    assert not (year / "x.jpg.supplemental-metadata.json").exists(), "sidecar should be deleted"
    assert (album / "x.jpg.supplemental-metadata.json").exists(), "album sidecar should be kept"


def test_dedup_keep_jsons_preserves_sidecar(tmp: Path) -> None:
    """--keep-jsons keeps the sidecar even when the media is deleted."""
    year = tmp / "Photos from 2019"
    album = tmp / "Vacation 2019"
    album.mkdir(parents=True)
    write_jpeg(year / "x.jpg")
    write_sidecar(year / "x.jpg", TS_2019_05_01_NOON)
    shutil.copy2(year / "x.jpg", album / "x.jpg")

    p = run_tool("dedup", "--root", str(tmp), "--keep-jsons", "--apply")
    assert_tool_ok(p)
    assert not (year / "x.jpg").exists()
    assert (year / "x.jpg.supplemental-metadata.json").exists(), "sidecar should be kept with --keep-jsons"


def test_dedup_content_hash_catches_renamed_dup(tmp: Path) -> None:
    """Files with DIFFERENT names but identical bytes are still detected."""
    year = tmp / "Photos from 2019"
    album = tmp / "Vacation 2019"
    album.mkdir(parents=True)
    write_jpeg(year / "IMG_001.jpg")
    shutil.copy2(year / "IMG_001.jpg", album / "vacation_pic.jpg")

    p = run_tool("dedup", "--root", str(tmp), "--apply")
    assert_tool_ok(p)
    assert not (year / "IMG_001.jpg").exists(), "duplicate in year folder should be deleted"
    assert (album / "vacation_pic.jpg").exists()


# ---------------------------------------------------------------------------
# Tests — flags
# ---------------------------------------------------------------------------

def test_album_filter_restricts_scope(tmp: Path) -> None:
    """--album NAME limits processing to that top-level folder only."""
    a = tmp / "Album A" / "a.jpg"
    b = tmp / "Album B" / "b.jpg"
    write_jpeg(a)
    write_sidecar(a, TS_2019_05_01_NOON)
    write_jpeg(b)
    write_sidecar(b, TS_2019_05_01_NOON)

    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC", "--album", "Album A", "--apply")
    assert_tool_ok(p)
    # A got EXIF written, B did not (untouched).
    a_tags = read_exif(a, "DateTimeOriginal")
    b_tags = read_exif(b, "DateTimeOriginal")
    assert a_tags.get("DateTimeOriginal") == "2019:05:01 12:00:00", a_tags
    assert "DateTimeOriginal" not in b_tags, f"B should be untouched: {b_tags}"


def test_include_trash_processes_trash(tmp: Path) -> None:
    """Without --include-trash, Trash is skipped. With it, processed."""
    trash = tmp / "Trash" / "x.jpg"
    write_jpeg(trash)
    write_sidecar(trash, TS_2019_05_01_NOON)

    # Default: skipped.
    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)
    assert "DateTimeOriginal" not in read_exif(trash, "DateTimeOriginal"), \
        "Trash should be skipped by default"

    # With --include-trash: processed.
    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC", "--include-trash", "--apply")
    assert_tool_ok(p)
    tags = read_exif(trash, "DateTimeOriginal")
    assert tags.get("DateTimeOriginal") == "2019:05:01 12:00:00", tags


def test_dry_run_makes_no_changes(tmp: Path) -> None:
    """Without --apply, no files (or mtimes) should change."""
    f = tmp / "a.jpg"
    write_jpeg(f)
    set_exif(f, DateTimeOriginal="1970:01:01 00:00:00", CreateDate="1970:01:01 00:00:00")
    write_sidecar(f, TS_2019_05_01_NOON)
    # Reset mtime to a known value so we can detect a change.
    os.utime(f, (1000000, 1000000))

    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC")
    assert_tool_ok(p)
    tags = read_exif(f, "DateTimeOriginal")
    assert tags.get("DateTimeOriginal") == "1970:01:01 00:00:00", \
        f"dry-run should not rewrite EXIF, got {tags}"
    assert int(f.stat().st_mtime) == 1000000, \
        f"dry-run should not touch mtime, got {f.stat().st_mtime}"


def test_offset_time_original_honored_for_mtime(tmp: Path) -> None:
    """When EXIF carries OffsetTimeOriginal, the conflict-case mtime uses it
    instead of falling back to --timezone."""
    f = tmp / "a.jpg"
    write_jpeg(f)
    # EXIF says 2015-03-10 14:20:00 with offset +05:00 → UTC = 09:20:00
    set_exif(
        f,
        DateTimeOriginal="2015:03:10 14:20:00",
        CreateDate="2015:03:10 14:20:00",
        OffsetTimeOriginal="+05:00",
    )
    # JSON in 2019 — non-bogus conflict case.
    write_sidecar(f, TS_2019_05_01_NOON)

    # Pass --timezone UTC. If our code ignored OffsetTimeOriginal, it would
    # interpret EXIF in UTC → 14:20:00 UTC = 1425997200. With OffsetTimeOriginal
    # honored, EXIF is 14:20+05:00 → 09:20:00 UTC = 1425979200.
    p = run_tool("fix", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)
    expected = int(dt.datetime(2015, 3, 10, 9, 20, 0, tzinfo=dt.timezone.utc).timestamp())
    actual = int(f.stat().st_mtime)
    assert actual == expected, \
        f"OffsetTimeOriginal not honored: mtime {actual}, expected {expected}"


# ---------------------------------------------------------------------------
# Tests — rename
# ---------------------------------------------------------------------------

def test_rename_skips_already_canonical(tmp: Path) -> None:
    """A file already named YYYY-MM-DD_HH-mm-ss.ext is not renamed."""
    f = tmp / "2019-05-01_12-00-00.jpg"
    write_jpeg(f)
    write_sidecar(f, TS_2019_07_15_0930)  # JSON says a totally different time

    p = run_tool("rename", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)
    # Should still exist with the canonical name (not bumped to JSON's time).
    assert f.exists(), f"canonical name should be preserved; tree={list(tmp.iterdir())}"


def test_rename_paired_sidecar_renamed(tmp: Path) -> None:
    """Rename keeps the JSON sidecar paired with its media."""
    f = tmp / "IMG_001.jpg"
    write_jpeg(f)
    write_sidecar(f, TS_2019_05_01_NOON)

    p = run_tool("rename", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)
    renamed = tmp / "2019-05-01_12-00-00.jpg"
    assert renamed.exists(), f"media not renamed; tree={list(tmp.iterdir())}"
    assert (renamed.parent / (renamed.name + ".supplemental-metadata.json")).exists(), \
        f"sidecar not paired; tree={list(tmp.iterdir())}"
    assert not f.exists(), "original media name should be gone"


def test_rename_collision_gets_suffix(tmp: Path) -> None:
    """Two files that would land on the same date+time → second gets _NN suffix."""
    f1 = tmp / "a.jpg"
    f2 = tmp / "b.jpg"
    write_jpeg(f1)
    write_jpeg(f2)
    write_sidecar(f1, TS_2019_05_01_NOON)
    write_sidecar(f2, TS_2019_05_01_NOON)  # identical ts

    p = run_tool("rename", "--root", str(tmp), "--timezone", "UTC", "--apply")
    assert_tool_ok(p)
    canonical = tmp / "2019-05-01_12-00-00.jpg"
    suffixed = list(tmp.glob("2019-05-01_12-00-00_*.jpg"))
    assert canonical.exists(), f"canonical not present; tree={list(tmp.iterdir())}"
    assert len(suffixed) == 1, f"expected one suffixed file; tree={list(tmp.iterdir())}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS: list[tuple[str, Callable[[Path], None]]] = [
    ("write_missing_writes_json_exif", test_write_missing_writes_json_exif),
    ("agree_same_day_keeps_exif", test_agree_same_day_keeps_exif),
    ("agree_midnight_crossing_keeps_exif", test_agree_midnight_crossing_keeps_exif),
    ("bogus_1970_overwritten", test_bogus_1970_overwritten),
    ("bogus_year_too_old_overwritten", test_bogus_year_too_old_overwritten),
    ("bogus_year_too_future_overwritten", test_bogus_year_too_future_overwritten),
    ("conflict_default_preserves_exif", test_conflict_default_preserves_exif),
    ("conflict_override_with_flag_writes_json", test_conflict_override_with_flag_writes_json),
    ("conflict_mtime_follows_exif", test_conflict_mtime_follows_exif),
    ("agree_mtime_uses_json", test_agree_mtime_uses_json),
    ("gps_written_when_absent", test_gps_written_when_absent),
    ("gps_skipped_when_present", test_gps_skipped_when_present),
    ("gps_zero_treated_as_absent", test_gps_zero_treated_as_absent),
    ("gps_negative_latitude_writes_south_ref", test_gps_negative_latitude_writes_south_ref),
    ("dedup_deletes_year_keeps_album", test_dedup_deletes_year_keeps_album),
    ("dedup_deletes_sidecar_alongside_media", test_dedup_deletes_sidecar_alongside_media),
    ("dedup_keep_jsons_preserves_sidecar", test_dedup_keep_jsons_preserves_sidecar),
    ("dedup_content_hash_catches_renamed_dup", test_dedup_content_hash_catches_renamed_dup),
    ("album_filter_restricts_scope", test_album_filter_restricts_scope),
    ("include_trash_processes_trash", test_include_trash_processes_trash),
    ("dry_run_makes_no_changes", test_dry_run_makes_no_changes),
    ("offset_time_original_honored_for_mtime", test_offset_time_original_honored_for_mtime),
    ("rename_skips_already_canonical", test_rename_skips_already_canonical),
    ("rename_paired_sidecar_renamed", test_rename_paired_sidecar_renamed),
    ("rename_collision_gets_suffix", test_rename_collision_gets_suffix),
]


def main(only: Iterable[str] = ()) -> int:
    if shutil.which("exiftool") is None:
        print("exiftool not on PATH", file=sys.stderr)
        return 2

    only_set = set(only)
    selected = [t for t in TESTS if not only_set or t[0] in only_set]
    passed = 0
    failed = 0
    failures: list[str] = []
    for name, fn in selected:
        with tempfile.TemporaryDirectory(prefix=f"tc_{name}_") as tmpdir:
            tmp = Path(tmpdir)
            try:
                fn(tmp)
            except AssertionError as exc:
                failed += 1
                failures.append(f"FAIL  {name}\n    {exc}")
                print(f"  FAIL  {name}", flush=True)
                continue
            except Exception as exc:
                failed += 1
                failures.append(f"ERROR {name}: {type(exc).__name__}: {exc}")
                print(f"  ERROR {name}: {type(exc).__name__}: {exc}", flush=True)
                continue
            passed += 1
            print(f"  ok    {name}", flush=True)

    total = passed + failed
    print(f"\n{total} tests run — {passed} passed, {failed} failed")
    if failures:
        print("\n--- failure details ---")
        for line in failures:
            print(line)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
