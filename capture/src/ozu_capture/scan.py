"""Inbox scanning: mount discovery, accept list, quiesce, wall, markers.

The mount point is discovered from `mount` output rather than assumed:
macOS appends -1, -2… when a stale directory squats on /Volumes/<Share>,
so a hardcoded path would silently watch an empty husk.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import ledger
from .config import Config

log = logging.getLogger("ozu_capture.scan")

# WHISPER's FR-01 allowlist, verbatim (its core/audio.py enforces it).
ALLOWED_EXTENSIONS = frozenset(
    {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus",
     ".wma", ".aiff", ".mp4", ".mov", ".webm", ".mkv"})

MARKER_SUFFIX = ".failed.txt"


def marker_path(audio: Path) -> Path:
    return audio.with_name(audio.name + MARKER_SUFFIX)


def write_marker(audio: Path, reason: str) -> None:
    """FR-11: visible, once. Deleting the marker is the retry gesture."""
    m = marker_path(audio)
    if m.exists():
        return
    m.write_text(
        f"ozu-capture could not process {audio.name}\n\n{reason}\n\n"
        f"Fix the cause, then delete this file to retry.\n")
    log.warning("failed marker written: %s (%s)", m.name, reason.splitlines()[0])


def is_walled(audio: Path, prefixes: tuple[str, ...]) -> bool:
    stem = audio.stem.lower().strip()
    return any(stem.startswith(p) for p in prefixes if p)


def find_mount(share_url: str) -> Path | None:
    """Locate the share's live mount point in `mount` output."""
    u = urlparse(share_url)
    want = f"{u.hostname or ''}{unquote(u.path or '')}".lower()  # host/Share
    if not want:
        return None
    out = subprocess.run(["mount", "-t", "smbfs"], capture_output=True,
                         text=True, timeout=15).stdout
    for line in out.splitlines():
        m = re.match(r"//(?:[^@]+@)?([^ ]+) on (.+) \(smbfs", line)
        if m and m.group(1).lower() == want:
            return Path(m.group(2))
    return None


def ensure_mounted(cfg: Config, allow_remount: bool = True) -> Path | None:
    """FR-02: find the share; one keychain-backed remount attempt if absent.
    The caller rate-limits remount attempts — from a LaunchAgent a failing
    `mount volume` can raise a credentials dialog, and once per tick would
    make that a popup storm."""
    if cfg.inbox_override:
        p = Path(cfg.inbox_override)
        return p if p.is_dir() else None
    mp = find_mount(cfg.share_url)
    if mp or not allow_remount:
        return mp
    script = f'mount volume "{cfg.share_url}"'
    proc = subprocess.run(["osascript", "-e", script], capture_output=True,
                          text=True, timeout=60)
    if proc.returncode != 0:
        log.info("remount failed: %s", proc.stderr.strip()[:200])
        return None
    mp = find_mount(cfg.share_url)
    if mp:
        log.info("remounted %s at %s", cfg.share_url, mp)
    return mp


@dataclass
class Sweep:
    eligible: list[Path] = field(default_factory=list)   # stable, oldest first
    waiting: list[Path] = field(default_factory=list)    # seen, not yet stable
    marked: list[Path] = field(default_factory=list)     # carrying .failed.txt


def sweep_inbox(inbox: Path, con: sqlite3.Connection, cfg: Config,
                now: float | None = None) -> Sweep:
    """One pass over the inbox root (flat — processed/ etc. are not scanned)."""
    now = time.time() if now is None else now
    result = Sweep()
    present: set[str] = set()
    for p in sorted(inbox.iterdir(), key=lambda p: p.name):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.name.endswith(MARKER_SUFFIX):
            continue
        if p.suffix.lower() == ".json":
            continue                      # a youtube hand-off sidecar (FR-05)
        if marker_path(p).exists():
            result.marked.append(p)
            continue
        try:
            st = p.stat()
        except OSError:
            continue                      # vanished mid-scan; next tick
        if st.st_size == 0:
            continue
        if p.suffix.lower() not in ALLOWED_EXTENSIONS:
            write_marker(p, f"'{p.suffix}' is not a format WHISPER accepts.\n"
                         "Set the recorder to m4a (both phone checklists in "
                         "README.md), or convert the file and re-drop it.")
            result.marked.append(p)
            continue
        present.add(str(p))
        if ledger.observe(con, str(p), st.st_size, st.st_mtime, now,
                          cfg.quiesce_seconds):
            result.eligible.append(p)
        else:
            result.waiting.append(p)
    ledger.prune_observations(con, present)
    result.eligible.sort(key=lambda p: p.stat().st_mtime)
    return result


def move_to_processed(audio: Path, inbox: Path, cfg: Config) -> Path:
    """FR-34: NAS-side move; numeric suffix on name collision."""
    dest_dir = inbox / cfg.processed_subdir
    dest_dir.mkdir(exist_ok=True)
    dest, n = dest_dir / audio.name, 2
    while dest.exists():
        dest = dest_dir / f"{audio.stem}-{n}{audio.suffix}"
        n += 1
    audio.rename(dest)
    return dest
