"""Segment → paragraph merging (FR-22, D-10).

Whisper emits ~sentence-sized segments. They are merged into readable
paragraphs, breaking on a long pause or on length, preferring a sentence
boundary. Splits happen only between segments, never inside one, so paragraph
start times are always real segment start times (AC 5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from whisper_notes.core.engine import Segment

# End-of-sentence punctuation (Latin + CJK), allowing trailing quotes/brackets.
_SENTENCE_END = re.compile(r"[.!?…。！？]['\"’”)\]]*$")

# FR-15 energy gate. Measured on this machine: whisper-large-v3-turbo reports
# no_speech_prob=0.000 on pure digital silence while confidently emitting
# "Thank you." — the model's own no-speech signal cannot be trusted, so
# silence suppression is enforced here from the audio itself. Speech RMS sits
# orders of magnitude above -60 dBFS; digital silence is exactly 0.
SILENCE_FLOOR_DBFS = -60.0


def drop_silent_segments(
    segments: tuple[Segment, ...] | list[Segment],
    samples: np.ndarray,
    *,
    sample_rate: int = 16_000,
    floor_dbfs: float = SILENCE_FLOOR_DBFS,
) -> list[Segment]:
    """Drop segments whose underlying audio is effectively silent (FR-15, R3)."""
    floor_amplitude = 10 ** (floor_dbfs / 20)
    kept = []
    for segment in segments:
        lo = max(int(segment.start * sample_rate), 0)
        hi = min(int(segment.end * sample_rate), len(samples))
        if hi <= lo:
            continue
        rms = float(np.sqrt(np.mean(np.square(samples[lo:hi], dtype=np.float64))))
        if rms >= floor_amplitude:
            kept.append(segment)
    return kept


@dataclass(frozen=True)
class Paragraph:
    start: float
    text: str


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _ends_sentence(text: str) -> bool:
    return bool(_SENTENCE_END.search(text))


def merge_segments(
    segments: list[Segment] | tuple[Segment, ...],
    *,
    pause_seconds: float = 2.0,
    max_chars: int = 750,
) -> list[Paragraph]:
    paragraphs: list[Paragraph] = []
    buffer: list[Segment] = []
    buffer_chars = 0

    def flush(upto: int | None = None) -> None:
        nonlocal buffer, buffer_chars
        head, tail = (buffer[:upto], buffer[upto:]) if upto else (buffer, [])
        if head:
            paragraphs.append(
                Paragraph(
                    start=head[0].start,
                    text=" ".join(_clean(s.text) for s in head),
                )
            )
        buffer = tail
        buffer_chars = sum(len(_clean(s.text)) + 1 for s in buffer)

    for segment in segments:
        text = _clean(segment.text)
        if not text:
            continue  # FR-15: suppressed/empty segments never become output

        if buffer and segment.start - buffer[-1].end > pause_seconds:
            flush()

        if buffer and buffer_chars + len(text) > max_chars:
            # Prefer breaking after the last segment that ends a sentence.
            split_at = next(
                (
                    i + 1
                    for i in range(len(buffer) - 1, -1, -1)
                    if _ends_sentence(_clean(buffer[i].text))
                ),
                None,
            )
            flush(split_at)

        buffer.append(Segment(segment.start, segment.end, text))
        buffer_chars += len(text) + 1

    flush()
    return paragraphs
