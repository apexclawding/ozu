"""WHISPER CLI wrapper (FR-10) and §7 transcript parser (FR-13).

The CLI is the whole integration surface — argument list, never a shell
string, output to a path the bridge owns. The WHISPER repo is not imported,
not modified, not even on sys.path (REQUIREMENTS N1).
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config

log = logging.getLogger("ozu_capture.transcribe")

NO_SPEECH_BODY = "_No speech detected._"


class TranscriptionError(Exception):
    """CLI failed; str(exc) carries the stderr tail for the .failed marker."""


@dataclass(frozen=True)
class Transcript:
    fields: dict[str, str]      # §7 frontmatter, quotes stripped
    body: str                   # the paragraph stream after '## Transcript'
    no_speech: bool

    @property
    def language(self) -> str:
        return self.fields.get("language", "")

    @property
    def language_confidence(self) -> float:
        try:
            return float(self.fields.get("language_confidence", "0"))
        except ValueError:
            return 0.0

    @property
    def duration(self) -> str:
        return self.fields.get("duration", "")

    @property
    def duration_seconds(self) -> int:
        try:
            return int(float(self.fields.get("duration_seconds", "0")))
        except ValueError:
            return 0


def run_whisper(cfg: Config, audio: Path, out_md: Path) -> None:
    """One transcription, serialized by the caller (FR-06). Raises
    TranscriptionError on non-zero exit or timeout."""
    cmd = [*cfg.whisper_cmd, str(audio), "--out", str(out_md)]
    log.info("transcribing %s", audio.name)
    # WHISPER resolves its config.toml (and ./data) from the cwd — run it
    # in its own project dir or it reads OUR config.toml and refuses it.
    # VIRTUAL_ENV (the bridge's venv) is dropped so uv targets WHISPER's.
    # Under launchd PATH is minimal, so WHISPER's ffmpeg lookup needs the
    # Homebrew bin dir prepended.
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    if cfg.whisper_extra_path:
        env["PATH"] = ":".join([*cfg.whisper_extra_path,
                                env.get("PATH", "")])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=cfg.whisper_timeout,
                              cwd=cfg.whisper_cwd or None, env=env)
    except subprocess.TimeoutExpired:
        raise TranscriptionError(
            f"transcription exceeded {cfg.whisper_timeout}s and was killed")
    except OSError as e:                       # cmd itself missing/broken
        raise TranscriptionError(f"could not run WHISPER CLI: {e}")
    if proc.returncode != 0 or not out_md.exists():
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise TranscriptionError(
            f"whisper-notes exited {proc.returncode}\n{tail}")


def parse(md_text: str) -> Transcript:
    fields: dict[str, str] = {}
    body = md_text
    if md_text.startswith("---"):
        end = md_text.find("\n---", 3)
        if end != -1:
            for line in md_text[3:end].strip().splitlines():
                if ":" not in line:
                    continue
                k, _, v = line.partition(":")
                v = v.strip()
                if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                    v = v[1:-1]
                fields[k.strip()] = v
            body = md_text[end + 4:]

    marker = "## Transcript"
    i = body.find(marker)
    if i != -1:
        body = body[i + len(marker):]
    else:                                     # tolerate a schema drift: drop
        lines = body.strip().splitlines()     # the H1 and blockquote header
        body = "\n".join(l for l in lines
                         if not l.startswith(("# ", "> ")))
    body = body.strip()
    return Transcript(fields=fields, body=body,
                      no_speech=NO_SPEECH_BODY in body)
