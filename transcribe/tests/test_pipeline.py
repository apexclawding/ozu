"""Pipeline orchestration tests with a deterministic fake engine — no model
download, no MLX. The real-engine path is exercised by the P1 integration run."""

import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from whisper_notes.config import Config
from whisper_notes.core.engine import Segment, TranscriptionEngine, TranscriptionResult
from whisper_notes.core.pipeline import PipelineError, run_pipeline


class FakeEngine(TranscriptionEngine):
    name = "fake-engine"
    version = "0.0"
    model_repo = "fake/model"

    def __init__(self, segments, language="en", confidence=0.97):
        self._segments = tuple(segments)
        self._language = language
        self._confidence = confidence
        self.transcribe_calls = []

    def language_name(self, code):
        return {"en": "English", "pt": "Portuguese"}.get(code, code)

    def is_cached(self):
        return True

    def prefetch(self):
        pass

    def load(self):
        pass

    def unload(self):
        pass

    def detect_language(self, audio):
        return self._language, self._confidence

    def transcribe(self, audio, *, language=None, translate=False, progress=None):
        self.transcribe_calls.append({"language": language, "translate": translate})
        assert isinstance(audio, np.ndarray) and audio.dtype == np.float32
        if progress:
            progress(0.5)
            progress(1.0)
        return TranscriptionResult(
            language=language or self._language,
            language_confidence=None,
            segments=self._segments,
        )


SEGMENTS = [
    Segment(0.0, 1.5, "Hello world."),
    Segment(1.6, 3.0, "This is a test."),
]


class TestPipeline(unittest.TestCase):
    tmp: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.wav = Path(cls.tmp.name) / "team-standup.wav"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=3", str(cls.wav)],
            check=True,
            capture_output=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.data_dir = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=Path(self.data_dir.name))
        self.addCleanup(self.data_dir.cleanup)

    def test_happy_path_writes_schema_compliant_markdown(self):
        result = run_pipeline(
            self.wav, engine=FakeEngine(SEGMENTS), config=self.config
        )
        self.assertEqual(result.markdown_path.name, "team-standup.md")
        self.assertTrue(result.markdown_path.exists())
        content = result.markdown_path.read_text(encoding="utf-8")
        self.assertEqual(content, result.markdown)
        self.assertIn('language: "en"', content)
        self.assertIn("language_confidence: 0.97", content)
        self.assertIn('source_file: "team-standup.wav"', content)
        self.assertIn("duration_seconds: 3", content)
        self.assertIn("## Transcript", content)
        self.assertIn("**[00:00:00]** Hello world. This is a test.", content)
        self.assertIn("# Team Standup ", content)
        self.assertEqual(result.language, "en")
        self.assertEqual(result.word_count, 6)
        self.assertFalse(result.no_speech)
        self.assertEqual(len(result.sha256), 64)

    def test_phases_are_reported_in_order(self):
        phases = []
        run_pipeline(
            self.wav,
            engine=FakeEngine(SEGMENTS),
            config=self.config,
            progress=lambda phase, pct: phases.append(phase),
        )
        deduped = [p for i, p in enumerate(phases) if i == 0 or phases[i - 1] != p]
        self.assertEqual(
            deduped, ["decoding", "detecting language", "transcribing", "writing", "done"]
        )

    def test_forced_language_skips_detection(self):
        engine = FakeEngine(SEGMENTS)
        phases = []
        result = run_pipeline(
            self.wav,
            engine=engine,
            config=self.config,
            language="pt",
            progress=lambda phase, pct: phases.append(phase),
        )
        self.assertNotIn("detecting language", phases)
        self.assertEqual(engine.transcribe_calls[0]["language"], "pt")
        self.assertIsNone(result.language_confidence)
        self.assertIn("language_confidence: null", result.markdown)

    def test_collision_gets_suffix(self):
        first = run_pipeline(self.wav, engine=FakeEngine(SEGMENTS), config=self.config)
        second = run_pipeline(self.wav, engine=FakeEngine(SEGMENTS), config=self.config)
        self.assertEqual(first.markdown_path.name, "team-standup.md")
        self.assertEqual(second.markdown_path.name, "team-standup-2.md")

    def test_duration_limit_rejects(self):
        config = Config(data_dir=self.config.data_dir, max_duration_hours=0.0001)
        with self.assertRaises(PipelineError) as ctx:
            run_pipeline(self.wav, engine=FakeEngine(SEGMENTS), config=config)
        self.assertIn("limit", str(ctx.exception))

    def test_no_speech_produces_explicit_body(self):
        result = run_pipeline(self.wav, engine=FakeEngine([]), config=self.config)
        self.assertTrue(result.no_speech)
        self.assertEqual(result.word_count, 0)
        self.assertIn("_No speech detected._", result.markdown)

    def test_explicit_out_path_is_used(self):
        out = Path(self.data_dir.name) / "elsewhere" / "notes.md"
        result = run_pipeline(
            self.wav, engine=FakeEngine(SEGMENTS), config=self.config, out_path=out
        )
        self.assertEqual(result.markdown_path, out)
        self.assertTrue(out.exists())

    def test_translate_flag_reaches_engine_and_frontmatter(self):
        engine = FakeEngine(SEGMENTS)
        result = run_pipeline(
            self.wav, engine=engine, config=self.config, translate=True
        )
        self.assertTrue(engine.transcribe_calls[0]["translate"])
        self.assertIn("translated: true", result.markdown)


if __name__ == "__main__":
    unittest.main()
