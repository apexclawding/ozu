"""TranscriptionEngine ABC (§5.1, §8): the seam that lets pywhispercpp
replace mlx-whisper without touching anything above it (R4)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

# Fraction of the audio transcribed so far, 0.0..1.0.
ProgressCallback = Callable[[float], None]


class EngineError(Exception):
    """Engine failure. Message is user-facing (FR-38)."""


class ModelNotCachedError(EngineError):
    """Weights are not in the local cache and downloading was not consented."""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptionResult:
    language: str                       # ISO 639-1 code, e.g. "en"
    language_confidence: float | None   # None when the language was forced (FR-12)
    segments: tuple[Segment, ...]


class TranscriptionEngine(ABC):
    """One loaded model, one job at a time. Callers own concurrency (FR-16)."""

    name: str
    version: str
    model_repo: str

    def language_name(self, code: str) -> str:
        """Display name for a language code, e.g. "en" -> "English"."""
        return code

    def cache_progress_bytes(self) -> int | None:
        """Bytes of model data currently on disk, for download progress (R2).
        None when the engine cannot tell."""
        return None

    @property
    def supports_translation(self) -> bool:
        """Whether this model can perform translate-to-English (FR-13).
        Measured: the turbo distillations were trained on transcription only
        and silently ignore the translate task token."""
        return True

    @abstractmethod
    def is_cached(self) -> bool:
        """True if the model weights are fully present in the local cache."""

    @abstractmethod
    def prefetch(self) -> None:
        """Download the model. The ONLY permitted network operation (C3, R2).
        Callers must obtain explicit user consent before invoking."""

    @abstractmethod
    def load(self) -> None:
        """Load weights from local cache (FR-17). Raises ModelNotCachedError
        rather than downloading."""

    @abstractmethod
    def unload(self) -> None:
        """Release the model and return memory to the system (FR-17)."""

    @abstractmethod
    def detect_language(self, audio: np.ndarray) -> tuple[str, float]:
        """Detect language from the first 30 s (FR-11): (code, confidence)."""

    @abstractmethod
    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str | None = None,
        translate: bool = False,
        progress: ProgressCallback | None = None,
    ) -> TranscriptionResult:
        """Transcribe 16 kHz mono float32 audio with zero network calls (FR-10).

        language=None auto-detects (FR-11); a code forces it (FR-12).
        translate=True targets English (FR-13). Silent windows are suppressed,
        not emitted as text (FR-15).
        """
