"""Startup preflight checks: platform (NFR-08) and ffmpeg (FR-02, C4).

Both the web server and the CLI run these before doing anything else and
refuse to start on failure, printing the exact remediation.
"""

from __future__ import annotations

import platform
import shutil
import subprocess


class PreflightError(Exception):
    """A failed preflight check. Message is user-facing and actionable."""


def check_platform() -> None:
    """WHISPER is Apple Silicon-only in v1 (NFR-08): fail loudly elsewhere."""
    system, machine = platform.system(), platform.machine()
    if system != "Darwin" or machine != "arm64":
        raise PreflightError(
            f"WHISPER v1 requires macOS on Apple Silicon (arm64); "
            f"this machine is {system}/{machine}. MLX has no backend here."
        )


def check_ffmpeg() -> str:
    """Return the ffmpeg path, or raise with the exact remediation (FR-02)."""
    path = shutil.which("ffmpeg")
    if path is None:
        raise PreflightError(
            "ffmpeg was not found on PATH. It is required to decode audio.\n"
            "Install it with:\n\n    brew install ffmpeg"
        )
    try:
        result = subprocess.run(
            [path, "-version"], capture_output=True, timeout=10, check=False
        )
    except OSError as e:
        raise PreflightError(
            f"ffmpeg at {path} could not be executed ({e}).\n"
            "Reinstall it with:\n\n    brew reinstall ffmpeg"
        ) from e
    if result.returncode != 0:
        raise PreflightError(
            f"ffmpeg at {path} exists but 'ffmpeg -version' failed.\n"
            "Reinstall it with:\n\n    brew reinstall ffmpeg"
        )
    return path


def run_preflight() -> str:
    """Run all checks; return the ffmpeg path for callers that need it."""
    check_platform()
    return check_ffmpeg()
