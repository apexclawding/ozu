"""The transcription pipeline — single source of truth (§8, FR-42).

Knows nothing about HTTP. The web queue worker and the CLI both call
run_pipeline() with a progress callback; nothing else duplicates this flow.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from whisper_notes.config import Config
from whisper_notes.core.audio import decode, probe
from whisper_notes.core.engine import TranscriptionEngine
from whisper_notes.core.markdown import TranscriptMeta, render_markdown, word_count
from whisper_notes.core.segments import drop_silent_segments, merge_segments
from whisper_notes.logs import get_logger
from whisper_notes.store.files import allocate_md_path

_log = get_logger("pipeline")

# (phase, percent 0..100 or None). Phases per FR-32; "queued" belongs to the
# queue layer (P2), not the pipeline.
ProgressFn = Callable[[str, float | None], None]


class PipelineError(Exception):
    """Rejection or failure. Message is user-facing (FR-38)."""


@dataclass(frozen=True)
class PipelineResult:
    markdown_path: Path
    markdown: str
    language: str
    language_confidence: float | None
    duration_seconds: float
    processing_seconds: int
    realtime_factor: float
    word_count: int
    sha256: str
    no_speech: bool


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def make_title(source_name: str, date: str) -> str:
    stem = Path(source_name).stem
    words = re.sub(r"[-_]+", " ", stem).split()
    pretty = " ".join(w.capitalize() if w.islower() else w for w in words)
    return f"{pretty or 'Transcript'} {date}"


def run_pipeline(
    source: Path,
    *,
    engine: TranscriptionEngine,
    config: Config,
    language: str | None = None,
    translate: bool = False,
    timestamps: bool | None = None,
    out_path: Path | None = None,
    progress: ProgressFn | None = None,
) -> PipelineResult:
    last_phase: list[str | None] = [None]

    def report(phase: str, percent: float | None = None) -> None:
        if phase != last_phase[0]:  # NFR-09: log phase transitions once each
            last_phase[0] = phase
            _log.info("%s: phase %s", source.name, phase)
        if progress is not None:
            progress(phase, percent)

    started = time.monotonic()
    _log.info(
        "%s: start model=%s language=%s translate=%s",
        source.name, engine.model_repo, language or "auto", translate,
    )

    # -- validate (FR-03) ---------------------------------------------------
    report("decoding")
    info = probe(source)
    if info.size_bytes > config.max_file_bytes:
        raise PipelineError(
            f"{source.name} is {info.size_bytes / 1024**3:.1f} GB; "
            f"the limit is {config.max_file_gb:g} GB."
        )
    if info.duration_seconds > config.max_duration_seconds:
        raise PipelineError(
            f"{source.name} is {info.duration_seconds / 3600:.1f} hours long; "
            f"the limit is {config.max_duration_hours:g} hours."
        )

    sha256 = compute_sha256(source)  # FR-05
    samples = decode(source)  # FR-04

    # -- language (FR-11 / FR-12) -------------------------------------------
    confidence: float | None = None
    if language is None:
        report("detecting language")
        language, confidence = engine.detect_language(samples)

    # -- transcribe (FR-10, FR-15) ------------------------------------------
    report("transcribing", 0.0)
    result = engine.transcribe(
        samples,
        language=language,
        translate=translate,
        progress=lambda fraction: report("transcribing", fraction * 100),
    )
    paragraphs = merge_segments(
        drop_silent_segments(result.segments, samples),
        pause_seconds=config.paragraph_pause_seconds,
        max_chars=config.paragraph_max_chars,
    )
    processing_seconds = max(int(round(time.monotonic() - started)), 1)

    # -- render + write (FR-20..FR-26) --------------------------------------
    report("writing")
    transcribed_at = datetime.now().astimezone()
    meta = TranscriptMeta(
        title=make_title(source.name, transcribed_at.date().isoformat()),
        source_file=source.name,
        source_sha256=sha256,
        duration_seconds=info.duration_seconds,
        language=result.language,
        language_name=engine.language_name(result.language),
        language_confidence=confidence,
        translated=translate,
        model=engine.model_repo,
        engine=f"{engine.name} {engine.version}",
        transcribed_at=transcribed_at,
        processing_seconds=processing_seconds,
    )
    markdown = render_markdown(
        meta,
        paragraphs,
        timestamps=config.timestamps if timestamps is None else timestamps,
        summary_line=config.summary_line,
    )

    if out_path is None:
        config.transcripts_dir.mkdir(parents=True, exist_ok=True)
        out_path = allocate_md_path(config.transcripts_dir, source.name)
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(out_path, markdown)

    report("done", 100.0)
    import resource

    _log.info(
        "%s: done duration=%.1fs processing=%ds realtime=%.1fx words=%d "
        "language=%s peak_rss=%.2fGB -> %s",
        source.name, info.duration_seconds, processing_seconds,
        round(info.duration_seconds / processing_seconds, 1),
        word_count(paragraphs), result.language,
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**3, out_path,
    )
    return PipelineResult(
        markdown_path=out_path,
        markdown=markdown,
        language=result.language,
        language_confidence=confidence,
        duration_seconds=info.duration_seconds,
        processing_seconds=processing_seconds,
        realtime_factor=round(info.duration_seconds / processing_seconds, 1),
        word_count=word_count(paragraphs),
        sha256=sha256,
        no_speech=not paragraphs,
    )


def _write_atomic(path: Path, content: str) -> None:
    """Write via temp file + rename so a failure leaves no partial .md."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
