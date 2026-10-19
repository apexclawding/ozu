"""§7 transcript → the vault note (FR-20..24).

The vault is an Open Knowledge Format (v0.2) bundle since 2026-09-02
(ozu `docs/okf.md`). What that means for this writer:

- a note carries `type`, `description`, `generated` and `sources` beside the
  pipeline's own keys, and its filename is its id;
- frontmatter is strict YAML — `_scalar` below quotes what needs quoting,
  mirroring Ozu's `okf.py` line for line (this package has no
  dependencies, so the few rules are copied rather than imported);
- **no registry is touched.** `index.md` and `log.md` are derived views the
  VPS regenerates from frontmatter on every `ozu index`; the bridge's job
  ends at the note file and its commit. That is the single-writer rule
  the project board asked for, and AC 9 now pins it the other way round.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path

from .transcribe import Transcript

log = logging.getLogger("ozu_capture.note")

LANGUAGE_NAMES = {
    "en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French",
    "de": "German", "it": "Italian", "nl": "Dutch", "ca": "Catalan",
    "gl": "Galician", "ru": "Russian", "zh": "Chinese", "ja": "Japanese",
    "ar": "Arabic", "hi": "Hindi", "pl": "Polish", "tr": "Turkish",
}

# OKF: a note's kind, by producer. Ozu's okf.TYPE_BY_SOURCE agrees.
TYPE_VOICE = "Voice Memo"
TYPE_TRANSCRIPT = "Transcript"
ACTOR_VOICE = "ozu-capture/whisper"
ACTOR_YOUTUBE = "ozu-capture/youtube"


def _slugify(s: str) -> str:                      # vault.py, verbatim
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:60] or "note"


def _unique_id(topic_dir: Path, slug: str, date: str) -> tuple[str, Path]:
    """Id and path together — the file is named after the id (vault.py)."""
    base = f"{date.replace('-', '')}-{slug}"
    note_id, n = base, 2
    while (topic_dir / f"{note_id}.md").exists():
        note_id = f"{base}-{n}"
        n += 1
    return note_id, topic_dir / f"{note_id}.md"


# -- frontmatter (okf.py, verbatim) -----------------------------------------

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(T[\d:]+(\.\d+)?(Z|[+-]\d{2}:\d{2})?)?$")
_NUMBER = re.compile(r"^[-+]?(\d[\d_]*(\.\d*)?|\.\d+)([eE][-+]?\d+)?$")
_SEXAGESIMAL = re.compile(r"^[-+]?\d+(:\d+)+(\.\d*)?$")    # 00:19:37 — YAML 1.1 reads it as an int
_RESERVED_WORDS = {"true", "false", "null", "yes", "no", "on", "off", "~", ""}


def _scalar(v) -> str:
    """One YAML scalar, double-quoted only when a plain scalar would be
    misread (a `: ` inside a title, a sexagesimal duration, a bare word
    YAML takes for a boolean). Dates stay bare."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("\n", " ").strip()
    if _DATE.match(s):
        return s
    needs_quote = (
        s.lower() in _RESERVED_WORDS
        or _NUMBER.match(s) or _SEXAGESIMAL.match(s)
        or ": " in s or " #" in s
        or s.endswith(":")
        or s[0] in "-?:,[]{}#&*!|>'\"%@`"
        or s != str(v).strip()
    )
    if not needs_quote:
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _frontmatter(**kw) -> str:
    """okf.dump_frontmatter's house style: one-line scalars and flat lists
    (`tags: [a, b]`), inline `generated: { by, at }`, block-style `sources`.
    `None` values are skipped."""
    lines = ["---"]
    for k, v in kw.items():
        if v is None:
            continue
        if isinstance(v, dict):
            inner = ", ".join(f"{ik}: {_scalar(iv)}" for ik, iv in v.items() if iv is not None)
            lines.append(f"{k}: {{ {inner} }}")
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            lines.append(f"{k}:")
            for item in v:
                first = True
                for ik, iv in item.items():
                    if iv is None:
                        continue
                    lines.append(f"  {'- ' if first else '  '}{ik}: {_scalar(iv)}")
                    first = False
        elif isinstance(v, list):
            lines.append(f"{k}: [{', '.join(_scalar(x) for x in v)}]")
        else:
            lines.append(f"{k}: {_scalar(v)}")
    lines.append("---\n")
    return "\n".join(lines)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def title_from_filename(name: str) -> str:
    stem = Path(name).stem
    title = re.sub(r"[\s_]+", " ", stem).strip()
    return title or "Voice memo"


def human_duration(seconds: int) -> str:
    h, rest = divmod(max(seconds, 0), 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def vault_has_sha(vault_dir: Path, topic: str, sha: str) -> str | None:
    """FR-22 backstop: the note id already carrying this audio, or None.
    Scans frontmatter of the topic's notes — survives a lost ledger."""
    topic_dir = vault_dir / "topics" / topic
    if not topic_dir.is_dir():
        return None
    needle = f"source_sha256: {sha}"
    for path in sorted(topic_dir.glob("*.md")):
        if path.name in ("index.md", "_topic.md"):
            continue
        head = path.read_text()[:2500]
        if needle in head:
            m = re.search(r"^id: (.+)$", head, flags=re.M)
            return m.group(1).strip() if m else path.stem
    return None


def write_note(vault_dir: Path, topic: str, *, audio_name: str, sha: str,
               recorded: str, today: str, tr: Transcript) -> tuple[str, str]:
    """Write the note. Returns (note_id, rel_path). No registry is written —
    the VPS derives index.md from this file's frontmatter. The caller owns
    rebase-before / commit-after (FR-30/31)."""
    topic_dir = vault_dir / "topics" / topic
    topic_dir.mkdir(parents=True, exist_ok=True)

    title = title_from_filename(audio_name)
    note_id, path = _unique_id(topic_dir, _slugify(title), recorded)
    lang = LANGUAGE_NAMES.get(tr.language, tr.language or "unknown language")
    needs_review = tr.language_confidence < 0.8
    summary = (f"Voice memo ({human_duration(tr.duration_seconds)}, {lang}), "
               f"transcribed on-device {today}.")

    fm = _frontmatter(
        type=TYPE_VOICE,
        id=note_id, title=title, description=summary,
        tags=["voice", "transcript"],
        created=recorded, updated=today,
        source=f"voice:{audio_name}",
        source_sha256=sha,
        duration=tr.duration,
        language=tr.language or "unknown",
        related=[],
        needs_review=needs_review,
        status="draft" if needs_review else None,
        generated={"by": ACTOR_VOICE, "at": _now_iso()},
        # the audio never enters the vault; OKF allows a scope descriptor
        # where a path cannot be followed
        sources=[{"id": "audio", "resource": f"on-device audio: {audio_name}",
                  "title": audio_name}],
    )
    path.write_text(f"{fm}\n# {title}\n\n> Summary: {summary}\n\n{tr.body}\n")

    rel_path = str(path.relative_to(vault_dir))
    log.info("wrote %s -> %s", note_id, rel_path)
    return note_id, rel_path


# -- youtube source (Amendment 1 §7.1) ----------------------------------

def sanitize_title(s: str) -> str:
    """One line, collapsed whitespace. (Bracketed titles used to be unwrapped
    because the line parser read them as lists; the YAML writer quotes them
    now, but the rule stays so ids and commit messages do not change.)"""
    t = re.sub(r"\s+", " ", s or "").strip()
    if t.startswith("[") and t.endswith("]"):
        t = t[1:-1].strip()
    return t or "Untitled video"


def duration_hms(seconds: int) -> str:
    h, rest = divmod(max(int(seconds), 0), 3600)
    m, s = divmod(rest, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _published(upload_date: str) -> str:
    d = (upload_date or "").strip()
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else ""


def _language_name(code: str) -> str:
    base = (code or "").split("-", 1)[0].lower()
    return LANGUAGE_NAMES.get(base, code or "unknown language")


def vault_has_video(vault_dir: Path, topic: str, video_id: str
                    ) -> tuple[str, str, Path] | None:
    """FR-02 backstop: (note_id, learn status, path) of the note already
    carrying this video, or None. Frontmatter scan — survives a lost
    ledger, and tells the caller whether the note is a `skipped` stub that
    a retry may rewrite in place."""
    topic_dir = vault_dir / "topics" / topic
    if not topic_dir.is_dir():
        return None
    needle = re.compile(rf"^video_id: {re.escape(video_id)}\s*$", re.M)
    for path in sorted(topic_dir.glob("*.md")):
        if path.name in ("index.md", "_topic.md"):
            continue
        head = path.read_text()[:3000]
        if not needle.search(head):
            continue
        # a "Learning:" companion also carries video_id; only the transcript
        # note (tag `transcript`) counts as "already captured"
        if "tags: [learning, youtube, transcript]" not in head:
            continue
        nid = re.search(r"^id: (.+)$", head, flags=re.M)
        status = re.search(r"^learn: (.+)$", head, flags=re.M)
        return ((nid.group(1).strip() if nid else path.stem),
                (status.group(1).strip() if status else ""), path)
    return None


def write_youtube_note(vault_dir: Path, topic: str, *, meta: dict,
                       transcript: str, today: str, captions: str,
                       learn: str = "requested", skip_reason: str = "",
                       replace: Path | None = None) -> tuple[str, str]:
    """Transcript note per Amendment 1 §7.1 (or its `learn: skipped`
    stub). `meta` is a VideoInfo.as_meta() dict — also what the FR-05
    sidecar carries. With `replace`, an existing skipped stub is rewritten
    in place (same id, same path) — the retry gesture after a raised cap or
    a repaired fetch. Returns (note_id, rel_path); the caller owns
    rebase-before / commit-after. No registry is written."""
    topic_dir = vault_dir / "topics" / topic
    topic_dir.mkdir(parents=True, exist_ok=True)

    video_id = str(meta["video_id"])
    title = sanitize_title(str(meta.get("title", "")))
    channel = sanitize_title(str(meta.get("channel", ""))) if meta.get("channel") else "unknown channel"
    duration = int(meta.get("duration") or 0)
    dur_h = human_duration(duration)
    language = str(meta.get("language") or "")
    url = str(meta.get("url") or f"https://www.youtube.com/watch?v={video_id}")

    if replace is not None:
        path = replace
        head = path.read_text()[:3000]
        m = re.search(r"^id: (.+)$", head, flags=re.M)
        note_id = m.group(1).strip() if m else path.stem
        m = re.search(r"^created: (.+)$", head, flags=re.M)
        created = m.group(1).strip() if m else today
    else:
        note_id, path = _unique_id(topic_dir, _slugify(title), today)
        created = today

    if learn == "skipped":
        summary = f"YouTube — {channel}, {dur_h}. Not read: {skip_reason}."
        body = (f"{url}\n\n{channel} · {dur_h} · not read: {skip_reason}. "
                f"Delete this video's ledger row on the NAS to retry.")
    elif captions == "whisper":
        summary = (f"YouTube — {channel}, {dur_h}, no captions; transcribed "
                   f"on-device {today}. Learning brief pending.")
        body = f"{url}\n\n{transcript.strip()}"
    else:
        summary = (f"YouTube — {channel}, {dur_h}, "
                   f"{_language_name(language)} {captions} captions, "
                   f"fetched {today}. Learning brief pending.")
        body = f"{url}\n\n{transcript.strip()}"

    published = _published(str(meta.get("upload_date", "")))
    source = {"id": "video", "resource": url, "title": title}
    if published:
        source["last_modified"] = f"{published}T00:00:00Z"

    fields: dict = dict(
        type=TYPE_TRANSCRIPT,
        id=note_id, title=title, description=summary,
        tags=["learning", "youtube", "transcript"],
        created=created, updated=today,
        source=f"youtube:{video_id}",
        video_id=video_id, url=url, channel=channel,
        duration=duration_hms(duration),
        published=published or "unknown",
        language=language or "unknown",
        captions=captions if learn != "skipped" else "none",
        learn=learn,
    )
    if learn == "skipped":
        fields["skip_reason"] = skip_reason
    fields.update(
        related=[], needs_review=False,
        generated={"by": ACTOR_YOUTUBE, "at": _now_iso()},
        sources=[source],
    )

    fm = _frontmatter(**fields)
    path.write_text(f"{fm}\n# {title}\n\n> Summary: {summary}\n\n{body}\n")

    rel_path = str(path.relative_to(vault_dir))
    log.info("wrote %s -> %s (learn: %s)", note_id, rel_path, learn)
    return note_id, rel_path
