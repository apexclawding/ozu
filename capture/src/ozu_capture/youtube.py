"""The youtube source: a playlist becomes transcript notes (Amendment 1
§6.1, FR-01..FR-11).

Everything YouTube-facing is one CLI, yt-dlp, run as an argument list —
never a shell string — so the tests substitute a fake and the container
pins a version. Three calls exist: a flat listing of the playlist, the
per-video metadata (which languages have captions), and the caption fetch
itself. A fourth, the audio download, only runs for the WHISPER hand-off
(FR-05). Nothing here touches git or the ledger; run.py owns the state
machine, this module owns the I/O and the VTT → transcript transform.
"""
from __future__ import annotations

import html
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

log = logging.getLogger("ozu_capture.youtube")


class FetchError(Exception):
    """yt-dlp failed or answered nonsense; transient unless it keeps
    happening (run.py counts attempts, FR-10)."""


@dataclass(frozen=True)
class VideoInfo:
    video_id: str
    title: str
    channel: str
    duration: int                 # seconds; 0 when YouTube did not say
    upload_date: str              # YYYYMMDD or ""
    language: str                 # e.g. "en-US", may be ""
    url: str
    subtitles: dict = field(default_factory=dict)          # manual tracks
    automatic_captions: dict = field(default_factory=dict)  # ASR tracks

    def as_meta(self) -> dict:
        """The sidecar / note-writer view: plain JSON-able fields."""
        return {"video_id": self.video_id, "title": self.title,
                "channel": self.channel, "duration": self.duration,
                "upload_date": self.upload_date, "language": self.language,
                "url": self.url}


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _run(cfg: Config, args: list[str], timeout: int | None = None
         ) -> subprocess.CompletedProcess:
    cmd = [*cfg.ytdlp_cmd, *cfg.ytdlp_extra_args, *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout or cfg.ytdlp_timeout)
    except subprocess.TimeoutExpired:
        raise FetchError(f"yt-dlp exceeded {timeout or cfg.ytdlp_timeout}s")
    except OSError as e:
        raise FetchError(f"could not run yt-dlp: {e}")


def _tail(proc: subprocess.CompletedProcess) -> str:
    err = (proc.stderr or "").strip().splitlines()
    return "\n".join(err[-4:])[:300]


def ytdlp_version(cfg: Config) -> str:
    try:
        p = _run(cfg, ["--version"], timeout=60)
    except FetchError as e:
        return f"unavailable ({e})"
    return p.stdout.strip() or f"unknown (exit {p.returncode})"


def list_playlist(cfg: Config) -> list[dict]:
    """[{id, title, duration}] in playlist order. Raises FetchError."""
    p = _run(cfg, ["--flat-playlist", "-J", "--no-warnings",
                   cfg.playlist_url], timeout=120)
    if p.returncode != 0:
        raise FetchError(f"listing failed (exit {p.returncode}): {_tail(p)}")
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise FetchError("listing returned no JSON")
    out = []
    for e in data.get("entries") or []:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        out.append({"id": str(e["id"]), "title": str(e.get("title") or ""),
                    "duration": int(e.get("duration") or 0)})
    return out


def fetch_info(cfg: Config, video_id: str) -> VideoInfo:
    p = _run(cfg, ["-J", "--skip-download", "--no-warnings",
                   video_url(video_id)], timeout=120)
    if p.returncode != 0:
        raise FetchError(f"info failed (exit {p.returncode}): {_tail(p)}")
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise FetchError("info returned no JSON")
    return VideoInfo(
        video_id=str(d.get("id") or video_id),
        title=str(d.get("title") or ""),
        channel=str(d.get("channel") or d.get("uploader") or ""),
        duration=int(d.get("duration") or 0),
        upload_date=str(d.get("upload_date") or ""),
        language=str(d.get("language") or ""),
        url=str(d.get("webpage_url") or video_url(video_id)),
        subtitles=d.get("subtitles") or {},
        automatic_captions=d.get("automatic_captions") or {},
    )


# -- caption choice (FR-04) --------------------------------------------------

def _base(lang: str) -> str:
    return lang.split("-", 1)[0].lower()


def _manual_for(tracks: dict, base: str) -> str | None:
    keys = [k for k in tracks if _base(k) == base]
    if not keys:
        return None
    return base if base in keys else sorted(keys)[0]


def _auto_for(tracks: dict, base: str) -> str | None:
    # `xx-orig` is the recogniser's own track; every other code under
    # automatic_captions is a machine translation of it.
    for cand in (f"{base}-orig", base):
        if cand in tracks:
            return cand
    keys = [k for k in tracks if _base(k) == base]
    return sorted(keys)[0] if keys else None


def pick_captions(info: VideoInfo, preferred: tuple[str, ...]
                  ) -> tuple[str, str] | None:
    """(kind, track) where kind is 'manual' or 'auto', or None when the
    video has no captions of any kind. Order: the video's own language,
    manual before auto; then the preference list; then anything."""
    bases: list[str] = []
    if info.language:
        bases.append(_base(info.language))
    bases += [_base(l) for l in preferred if _base(l) not in bases]
    for b in bases:
        k = _manual_for(info.subtitles, b)
        if k:
            return "manual", k
        k = _auto_for(info.automatic_captions, b)
        if k:
            return "auto", k
    if info.subtitles:
        return "manual", sorted(info.subtitles)[0]
    if info.automatic_captions:
        origs = sorted(k for k in info.automatic_captions if k.endswith("-orig"))
        return "auto", (origs or sorted(info.automatic_captions))[0]
    return None


def fetch_captions(cfg: Config, video_id: str, kind: str, track: str,
                   out_dir: Path) -> Path:
    """Write the chosen track as VTT into out_dir; return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    flag = "--write-subs" if kind == "manual" else "--write-auto-subs"
    p = _run(cfg, ["--skip-download", "--no-warnings", flag,
                   "--sub-langs", track, "--sub-format", "vtt",
                   "-o", str(out_dir / "%(id)s.%(ext)s"), video_url(video_id)])
    if p.returncode != 0:
        raise FetchError(f"captions failed (exit {p.returncode}): {_tail(p)}")
    found = sorted(out_dir.glob(f"{video_id}*.vtt"))
    exact = [f for f in found if f".{track}." in f.name]
    if exact:
        return exact[0]
    if found:
        return found[0]
    raise FetchError(f"captions: yt-dlp exited 0 but wrote no .vtt for {track}")


def download_audio(cfg: Config, video_id: str, out_dir: Path) -> Path:
    """FR-05 hand-off: best native audio stream, no ffmpeg, no re-encode.
    yt-dlp writes a .part and renames it on completion, so a watcher never
    sees a half file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    p = _run(cfg, ["--no-warnings", "-f", "bestaudio[ext=m4a]/bestaudio",
                   "-o", str(out_dir / "yt-%(id)s.%(ext)s"),
                   video_url(video_id)])
    if p.returncode != 0:
        raise FetchError(f"audio failed (exit {p.returncode}): {_tail(p)}")
    found = [f for f in out_dir.glob(f"yt-{video_id}.*")
             if f.suffix not in (".part", ".json", ".ytdl")]
    if not found:
        raise FetchError("audio: yt-dlp exited 0 but wrote no file")
    return found[0]


# -- VTT → transcript (FR-07) ------------------------------------------------

_CUE = re.compile(r"^\s*((?:\d+:)?\d\d:\d\d\.\d{3})\s+-->\s+"
                  r"((?:\d+:)?\d\d:\d\d\.\d{3})")
_TAG = re.compile(r"<[^>]*>")
_RULE = re.compile(r"^-{3,}$")
_SENTENCE_END = re.compile(r"[.!?][\"'”’)]*$")

PARAGRAPH_MIN_SECONDS = 45
PARAGRAPH_MAX_SECONDS = 75


def _seconds(ts: str) -> float:
    parts = ts.split(":")
    h = int(parts[0]) if len(parts) == 3 else 0
    m, s = parts[-2], parts[-1]
    return h * 3600 + int(m) * 60 + float(s)


def hms(seconds: float) -> str:
    total = int(seconds)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_vtt(text: str) -> list[tuple[float, str]]:
    """Cues → (start_seconds, line) with the rolling repeats removed.

    Auto captions emit every line twice: first alone with word timings,
    then again as the top line of the next cue while the following line
    is being revealed. Comparing each line with the last one emitted is
    enough to fold that back to a single stream; manual subtitles have no
    repeats and pass through unchanged.

    Parsed line by line rather than by blank-line blocks: YouTube pads
    cues with a line holding a single space, which a block splitter reads
    as a cue boundary and so drops the text under it.
    """
    out: list[tuple[float, str]] = []
    last: str | None = None
    start: float | None = None
    lines = text.replace("\r\n", "\n").split("\n")
    # A cue identifier is the non-blank line right before a timing line
    # (manual subtitles number their cues); it is not caption text.
    identifiers = {i - 1 for i, l in enumerate(lines)
                   if i and _CUE.match(l) and lines[i - 1].strip()
                   and not _CUE.match(lines[i - 1])}
    for i, raw in enumerate(lines):
        m = _CUE.match(raw)
        if m:
            start = _seconds(m.group(1))
            continue
        if i in identifiers:
            continue
        if start is None:
            continue                       # WEBVTT header, Kind:, Language:
        if raw.strip() == "":
            continue                       # padding or a block separator
        if raw.startswith(("NOTE", "STYLE", "REGION")) and "-->" not in raw:
            start = None                   # a non-cue block until next timing
            continue
        t = html.unescape(_TAG.sub("", raw))
        t = re.sub(r"\s+", " ", t).strip()
        if not t or t == last:
            continue
        if _RULE.match(t):                 # never let a line read as frontmatter
            t = "\\-\\-\\-"
        out.append((start, t))
        last = t
    return out


def to_transcript(cues: list[tuple[float, str]],
                  min_seconds: int = PARAGRAPH_MIN_SECONDS,
                  max_seconds: int = PARAGRAPH_MAX_SECONDS) -> str:
    """Paragraphs of roughly min–max seconds, each opened with a
    `**[HH:MM:SS]**` marker — the house transcript shape (§7.1)."""
    paragraphs: list[tuple[float, list[str]]] = []
    cur_start = 0.0
    cur: list[str] = []
    for start, line in cues:
        if cur:
            elapsed = start - cur_start
            if elapsed >= max_seconds or (
                    elapsed >= min_seconds and _SENTENCE_END.search(cur[-1])):
                paragraphs.append((cur_start, cur))
                cur = []
        if not cur:
            cur_start = start
        cur.append(line)
    if cur:
        paragraphs.append((cur_start, cur))
    return "\n\n".join(f"**[{hms(s)}]** {' '.join(lines)}"
                       for s, lines in paragraphs)


def vtt_to_transcript(text: str) -> str:
    return to_transcript(parse_vtt(text))
