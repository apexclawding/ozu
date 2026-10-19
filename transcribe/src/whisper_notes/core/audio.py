"""ffmpeg probe + decode to 16 kHz mono float32 PCM (FR-01, FR-04).

ffmpeg/ffprobe are always invoked with argument lists, never a shell string
(NFR-07). Error messages are plain-language and user-facing (FR-38).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16_000

# FR-01: the accepted upload extensions. ffmpeg can decode more; this is the
# explicit contract the UI and upload endpoint enforce.
ALLOWED_EXTENSIONS = frozenset(
    {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus",
     ".wma", ".aiff", ".mp4", ".mov", ".webm", ".mkv"}
)


class AudioError(Exception):
    """Decode/probe failure. Message is user-facing (FR-38)."""


@dataclass(frozen=True)
class AudioInfo:
    duration_seconds: float
    format_name: str
    codec: str
    size_bytes: int


def probe(path: Path) -> AudioInfo:
    """Read container metadata via ffprobe; reject files with no audio stream."""
    if not path.exists():
        raise AudioError(f"File not found: {path}")
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    except subprocess.TimeoutExpired as e:
        raise AudioError(f"Reading {path.name} timed out; the file may be corrupt.") from e
    if result.returncode != 0:
        reason = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        detail = reason[-1] if reason else "unknown error"
        raise AudioError(
            f"{path.name} could not be read as audio ({detail}). "
            "Is it a supported audio or video file?"
        )

    info = json.loads(result.stdout)
    audio_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio_streams:
        raise AudioError(f"{path.name} contains no audio track.")

    fmt = info.get("format", {})
    duration_raw = fmt.get("duration") or audio_streams[0].get("duration")
    if duration_raw is None:
        raise AudioError(f"Could not determine the duration of {path.name}.")

    return AudioInfo(
        duration_seconds=float(duration_raw),
        format_name=fmt.get("format_name", "unknown"),
        codec=audio_streams[0].get("codec_name", "unknown"),
        size_bytes=path.stat().st_size,
    )


def decode(path: Path) -> np.ndarray:
    """Decode any supported container to 16 kHz mono float32 samples (FR-04)."""
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error",
        "-i", str(path),
        "-vn",                      # drop video streams (FR-01: extract audio)
        "-f", "f32le", "-acodec", "pcm_f32le",
        "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, check=False)
    except OSError as e:
        raise AudioError(f"ffmpeg could not be executed: {e}") from e
    if result.returncode != 0:
        reason = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        detail = reason[-1] if reason else "unknown error"
        raise AudioError(f"Decoding {path.name} failed ({detail}).")

    samples = np.frombuffer(result.stdout, dtype=np.float32)
    if samples.size == 0:
        raise AudioError(f"{path.name} decoded to zero audio samples.")
    return samples
