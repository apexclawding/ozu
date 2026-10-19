"""A deterministic engine importable inside the spawned worker child process.
Delay is controlled by FAKE_ENGINE_DELAY (seconds), inherited via the
environment, so queue tests can simulate long-running jobs."""

import os
import time

from whisper_notes.core.engine import Segment, TranscriptionEngine, TranscriptionResult


class FakeWorkerEngine(TranscriptionEngine):
    name = "fake-worker"
    version = "0.0"

    def __init__(self, model_repo):
        self.model_repo = model_repo

    def language_name(self, code):
        return "English"

    def is_cached(self):
        return True

    def prefetch(self):
        pass

    def load(self):
        pass

    def unload(self):
        pass

    def detect_language(self, audio):
        return "en", 0.9

    def transcribe(self, audio, *, language=None, translate=False, progress=None):
        delay = float(os.environ.get("FAKE_ENGINE_DELAY", "0.2"))
        steps = 10
        for i in range(steps):
            time.sleep(delay / steps)
            if progress:
                progress((i + 1) / steps)
        return TranscriptionResult(
            language=language or "en",
            language_confidence=None,
            segments=(Segment(0.0, 2.0, "Fake transcription output."),),
        )


class FakeNoTranslateEngine(FakeWorkerEngine):
    """Simulates a transcription-only model (turbo-like)."""

    @property
    def supports_translation(self):
        return False


class FakeUncachedEngine(FakeWorkerEngine):
    """Simulates the first run: model absent until prefetch() is called."""

    _prefetched = False

    def is_cached(self):
        return type(self)._prefetched

    def prefetch(self):
        type(self)._prefetched = True

    def cache_progress_bytes(self):
        return 123_000_000
