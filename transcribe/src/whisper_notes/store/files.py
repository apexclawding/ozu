"""Output/upload path handling: sanitization (NFR-07) and collision-safe
allocation (FR-24)."""

from __future__ import annotations

import re
from pathlib import Path

_UNSAFE = re.compile(r'[/\\:\x00-\x1f<>"|?*]')


def sanitize_filename(name: str) -> str:
    """Reduce an untrusted filename to a safe basename — no path traversal."""
    base = Path(name).name  # strips any directory components
    base = _UNSAFE.sub("_", base).strip(" .")
    return base or "transcript"


def allocate_md_path(directory: Path, source_name: str) -> Path:
    """Source basename with .md, numeric suffix on collision (FR-24)."""
    stem = Path(sanitize_filename(source_name)).stem or "transcript"
    candidate = directory / f"{stem}.md"
    counter = 1
    while candidate.exists():
        counter += 1
        candidate = directory / f"{stem}-{counter}.md"
    return candidate


def allocate_upload_path(directory: Path, source_name: str) -> Path:
    """Collision-safe destination for an uploaded file, extension preserved."""
    safe = sanitize_filename(source_name)
    stem, ext = Path(safe).stem or "upload", Path(safe).suffix
    candidate = directory / f"{stem}{ext}"
    counter = 1
    while candidate.exists():
        counter += 1
        candidate = directory / f"{stem}-{counter}{ext}"
    return candidate
