"""Engine checks that need no model weights."""

import unittest

from whisper_notes.core.engine import ModelNotCachedError
from whisper_notes.core.engine_mlx import MlxWhisperEngine


class TestModelNotCached(unittest.TestCase):
    def test_load_uncached_model_names_model_and_remedy(self):
        engine = MlxWhisperEngine("mlx-community/does-not-exist-anywhere")
        self.assertFalse(engine.is_cached())
        with self.assertRaises(ModelNotCachedError) as ctx:
            engine.load()
        message = str(ctx.exception)
        self.assertIn("mlx-community/does-not-exist-anywhere", message)
        self.assertIn("prefetch-model", message)

    def test_language_name(self):
        engine = MlxWhisperEngine("mlx-community/whisper-large-v3-turbo")
        self.assertEqual(engine.language_name("en"), "English")
        self.assertEqual(engine.language_name("pt"), "Portuguese")
        self.assertEqual(engine.language_name("zz"), "Zz")


class TestTranslationCapability(unittest.TestCase):
    """Measured: turbo distillations ignore task=translate (FR-13 guard)."""

    def test_turbo_models_cannot_translate(self):
        for repo in (
            "mlx-community/whisper-large-v3-turbo",
            "mlx-community/whisper-large-v3-turbo-q4",
            "mlx-community/whisper-small.en-mlx",
        ):
            self.assertFalse(MlxWhisperEngine(repo).supports_translation, repo)

    def test_classic_multilingual_models_can_translate(self):
        for repo in (
            "mlx-community/whisper-medium-mlx",
            "mlx-community/whisper-large-v3-mlx",
            "mlx-community/whisper-tiny",
        ):
            self.assertTrue(MlxWhisperEngine(repo).supports_translation, repo)

    def test_translate_on_turbo_fails_fast_with_remedy(self):
        import numpy as np

        engine = MlxWhisperEngine("mlx-community/whisper-large-v3-turbo")
        from whisper_notes.core.engine import EngineError

        with self.assertRaises(EngineError) as ctx:
            engine.transcribe(np.zeros(16000, dtype=np.float32), translate=True)
        message = str(ctx.exception)
        self.assertIn("cannot translate", message)
        self.assertIn("whisper-medium-mlx", message)


if __name__ == "__main__":
    unittest.main()
