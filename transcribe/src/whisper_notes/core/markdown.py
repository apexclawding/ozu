"""Markdown output writer — implements the §7 schema exactly (FR-20..FR-25).

Frontmatter is emitted by hand: keys are a fixed, ordered, machine-parseable
set of plain scalars (§7 "stable and machine-parseable"), which stdlib string
formatting covers without a YAML dependency (C6).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from whisper_notes.core.segments import Paragraph

NO_SPEECH_BODY = "_No speech detected._"


@dataclass(frozen=True)
class TranscriptMeta:
    title: str
    source_file: str
    source_sha256: str
    duration_seconds: float
    language: str            # ISO code, e.g. "en"
    language_name: str       # display name, e.g. "English"
    language_confidence: float | None
    translated: bool
    model: str
    engine: str              # e.g. "mlx-whisper 0.4.3"
    transcribed_at: datetime  # timezone-aware
    processing_seconds: float


def format_hms(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _human_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, minutes = total // 3600, total % 3600 // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {total % 60:02d}s"
    return f"{total}s"


def _yaml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _model_display(model: str) -> str:
    # "mlx-community/whisper-large-v3-turbo" -> "Whisper large-v3-turbo"
    tail = model.rsplit("/", 1)[-1]
    return "Whisper " + tail.removeprefix("whisper-").removesuffix("-mlx")


def word_count(paragraphs: list[Paragraph]) -> int:
    return sum(len(p.text.split()) for p in paragraphs)


def render_markdown(
    meta: TranscriptMeta,
    paragraphs: list[Paragraph],
    *,
    timestamps: bool = True,
    summary_line: bool = True,
) -> str:
    processing = max(int(round(meta.processing_seconds)), 1)
    realtime_factor = round(meta.duration_seconds / processing, 1)
    confidence = (
        "null"
        if meta.language_confidence is None
        else str(round(meta.language_confidence, 2))
    )

    lines = [
        "---",
        f"title: {_yaml_str(meta.title)}",
        f"source_file: {_yaml_str(meta.source_file)}",
        f"source_sha256: {_yaml_str(meta.source_sha256)}",
        f"duration: {_yaml_str(format_hms(meta.duration_seconds))}",
        f"duration_seconds: {int(round(meta.duration_seconds))}",
        f"language: {_yaml_str(meta.language)}",
        f"language_confidence: {confidence}",
        f"translated: {'true' if meta.translated else 'false'}",
        f"model: {_yaml_str(meta.model)}",
        f"engine: {_yaml_str(meta.engine)}",
        f"transcribed_at: {_yaml_str(meta.transcribed_at.isoformat(timespec='seconds'))}",
        f"processing_seconds: {processing}",
        f"realtime_factor: {realtime_factor}",
        f"word_count: {word_count(paragraphs)}",
        "tags: [transcript]",
        "---",
        "",
        f"# {meta.title}",
        "",
    ]

    if summary_line:
        date = meta.transcribed_at.date().isoformat()
        lines += [
            f"> Transcribed locally with {_model_display(meta.model)} · "
            f"{_human_duration(meta.duration_seconds)} · {meta.language_name} · {date}",
            "",
        ]

    lines += ["## Transcript", ""]

    if not paragraphs:
        lines += [NO_SPEECH_BODY, ""]
    else:
        for paragraph in paragraphs:
            text = _escape_body(paragraph.text)
            if timestamps:
                lines.append(f"**[{format_hms(paragraph.start)}]** {text}")
            else:
                lines.append(text)
            lines.append("")

    return "\n".join(lines)


def _escape_body(text: str) -> str:
    # FR-25: a paragraph starting with --- would parse as a thematic break /
    # frontmatter delimiter; a leading backslash renders it as literal text.
    return "\\" + text if text.startswith("---") else text
