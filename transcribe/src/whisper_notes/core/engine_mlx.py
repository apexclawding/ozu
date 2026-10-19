"""mlx-whisper implementation of TranscriptionEngine (D-05).

Grounded in mlx_whisper 0.4.3 source:
- ModelHolder caches one model at class level -> load/unload (FR-17).
- transcribe() discards detection probabilities, so language detection is done
  here explicitly to capture the confidence score (FR-11).
- transcribe() drives a module-level `tqdm` reference for progress; a shim
  swapped in around the call routes frame counts to our callback.
- Window-level silence suppression comes from no_speech_threshold +
  logprob_threshold inside transcribe() (FR-15, R3).
"""

from __future__ import annotations

import gc
import os
from pathlib import Path

import numpy as np

from whisper_notes.core.engine import (
    ModelNotCachedError,
    ProgressCallback,
    Segment,
    TranscriptionEngine,
    TranscriptionResult,
)

# Anti-hallucination decode settings (FR-15, guardrail: silence must never
# produce text). Thresholds are Whisper's published defaults; conditioning on
# previous text is disabled because it is the classic trigger for repetition
# loops and timestamp drift on long files (R5).
NO_SPEECH_THRESHOLD = 0.6
LOGPROB_THRESHOLD = -1.0
CONDITION_ON_PREVIOUS_TEXT = False

TRANSLATION_UNSUPPORTED_MESSAGE = (
    "{} cannot translate — the turbo models were distilled for transcription "
    "only and ignore the translate task. To translate to English, use a "
    "translation-capable model, e.g. in config.toml:\n\n"
    '    [model]\n    repo = "mlx-community/whisper-medium-mlx"'
)


def _offline() -> bool:
    return os.environ.get("WHISPER_OFFLINE") == "1"


class _ProgressBar:
    """Stand-in for tqdm.tqdm: forwards update() fractions to a callback."""

    def __init__(self, callback: ProgressCallback, total: int):
        self._callback = callback
        self._total = max(total, 1)
        self._n = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def update(self, n: int) -> None:
        self._n += n
        self._callback(min(self._n / self._total, 1.0))


class _ProgressShim:
    """Replaces the `tqdm` module reference inside mlx_whisper.transcribe."""

    def __init__(self, callback: ProgressCallback):
        self._callback = callback

    def tqdm(self, *, total: int, **_ignored) -> _ProgressBar:
        return _ProgressBar(self._callback, total)


class MlxWhisperEngine(TranscriptionEngine):
    name = "mlx-whisper"

    def __init__(self, model_repo: str):
        self.model_repo = model_repo
        # huggingface_hub reads HF_HUB_OFFLINE at import time; set it before
        # anything imports it so WHISPER_OFFLINE=1 is enforceable (C3).
        if _offline():
            os.environ["HF_HUB_OFFLINE"] = "1"
        from importlib import metadata

        from huggingface_hub.utils import disable_progress_bars

        # Cache-hit resolves (load, is_cached) render a pointless "Fetching 4
        # files" bar; prefetch() re-enables bars for the real download (R2).
        # mlx itself is imported only in load()/transcribe(), so constructing
        # the engine in the web process never pulls Metal into its RSS.
        disable_progress_bars()
        self.version = metadata.version("mlx-whisper")

    def language_name(self, code: str) -> str:
        from mlx_whisper.tokenizer import LANGUAGES

        return LANGUAGES.get(code, code).title()

    @property
    def supports_translation(self) -> bool:
        # Measured on this machine: large-v3-turbo emits the source language
        # unchanged with task=translate (distilled on transcription only),
        # while classic multilingual Whisper models translate correctly.
        import re

        tail = self.model_repo.rsplit("/", 1)[-1].lower()
        english_only = re.search(r"\.en(?:[-_.]|$)", tail) is not None
        return "turbo" not in tail and not english_only

    def cache_progress_bytes(self) -> int | None:
        from huggingface_hub.constants import HF_HUB_CACHE

        repo_dir = Path(HF_HUB_CACHE) / f"models--{self.model_repo.replace('/', '--')}"
        if not repo_dir.exists():
            return 0
        return sum(f.stat().st_size for f in repo_dir.rglob("*") if f.is_file())

    # -- model lifecycle ----------------------------------------------------

    def is_cached(self) -> bool:
        if Path(self.model_repo).exists():  # local directory instead of a repo
            return True
        from huggingface_hub import snapshot_download

        try:
            snapshot_download(repo_id=self.model_repo, local_files_only=True)
            return True
        except Exception:
            return False

    def prefetch(self) -> None:
        if _offline():
            raise ModelNotCachedError(
                f"WHISPER_OFFLINE=1 is set, so the model {self.model_repo} "
                "cannot be downloaded. Unset it to fetch the model once."
            )
        from huggingface_hub import snapshot_download
        from huggingface_hub.utils import disable_progress_bars, enable_progress_bars

        enable_progress_bars()
        try:
            snapshot_download(repo_id=self.model_repo)
        finally:
            disable_progress_bars()

    def _require_cached(self) -> None:
        if not self.is_cached():
            raise ModelNotCachedError(
                f"The model {self.model_repo} is not in the local cache, and "
                "it is only downloaded with explicit consent. Fetch it once "
                "with:\n\n    uv run whisper-notes prefetch-model"
            )

    def load(self) -> None:
        self._require_cached()
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder

        # Bound MLX's Metal buffer cache. Measured on a 63-min file: unbounded,
        # peak memory footprint reaches 5.3 GB; the cap keeps the whole process
        # inside the 8 GB machine's budget (C1, NFR-02) for a small speed cost.
        mx.set_cache_limit(1 << 30)
        ModelHolder.get_model(self.model_repo, mx.float16)

    def unload(self) -> None:
        from mlx_whisper.transcribe import ModelHolder

        ModelHolder.model = None
        ModelHolder.model_path = None
        gc.collect()
        import mlx.core as mx

        mx.clear_cache()

    # -- inference ----------------------------------------------------------

    def detect_language(self, audio: np.ndarray) -> tuple[str, float]:
        self.load()
        import mlx.core as mx
        from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
        from mlx_whisper.transcribe import ModelHolder

        model = ModelHolder.get_model(self.model_repo, mx.float16)
        if not model.is_multilingual:
            return "en", 1.0
        mel = log_mel_spectrogram(audio, n_mels=model.dims.n_mels, padding=N_SAMPLES)
        mel_segment = pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16)
        _, probs = model.detect_language(mel_segment)
        language = max(probs, key=probs.get)
        return language, float(probs[language])

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str | None = None,
        translate: bool = False,
        progress: ProgressCallback | None = None,
    ) -> TranscriptionResult:
        if translate and not self.supports_translation:
            from whisper_notes.core.engine import EngineError

            raise EngineError(TRANSLATION_UNSUPPORTED_MESSAGE.format(self.model_repo))

        self.load()
        import importlib

        import mlx_whisper

        # mlx_whisper.transcribe names both a submodule and a re-exported
        # function; plain `import mlx_whisper.transcribe as m` binds the
        # function, so resolve the module itself for the tqdm patch.
        transcribe_module = importlib.import_module("mlx_whisper.transcribe")

        from mlx_whisper.tokenizer import LANGUAGES

        if language is not None and language not in LANGUAGES:
            from whisper_notes.core.engine import EngineError

            raise EngineError(
                f"Unknown language code '{language}'. "
                "Use ISO 639-1 codes like 'en', 'pt', 'es'."
            )

        confidence: float | None = None
        if language is None:
            language, confidence = self.detect_language(audio)

        original_tqdm = transcribe_module.tqdm
        if progress is not None:
            transcribe_module.tqdm = _ProgressShim(progress)
        try:
            raw = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=self.model_repo,
                language=language,
                task="translate" if translate else "transcribe",
                verbose=None,
                fp16=True,
                condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT,
                no_speech_threshold=NO_SPEECH_THRESHOLD,
                logprob_threshold=LOGPROB_THRESHOLD,
            )
        finally:
            transcribe_module.tqdm = original_tqdm

        segments = tuple(
            Segment(start=float(s["start"]), end=float(s["end"]), text=text)
            for s in raw["segments"]
            if (text := s["text"].strip())
        )
        return TranscriptionResult(
            language=raw["language"],
            language_confidence=confidence,
            segments=segments,
        )
