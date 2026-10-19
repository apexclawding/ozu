"""FR-13 end to end with a REAL translation-capable model.

Uses mlx-community/whisper-tiny (~75 MB) — quality is poor but the output
language is what's under test. Skipped when tiny is not in the local cache
(fetch it once with: uv run whisper-notes prefetch-model --model
mlx-community/whisper-tiny).
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TINY = "mlx-community/whisper-tiny"


def _tiny_cached() -> bool:
    try:
        from whisper_notes.core.engine_mlx import MlxWhisperEngine

        return MlxWhisperEngine(TINY).is_cached()
    except Exception:
        return False


@unittest.skipUnless(
    sys.platform == "darwin" and shutil.which("say"), "needs macOS 'say'"
)
class TestTranslateToEnglish(unittest.TestCase):
    def test_spanish_audio_translates_to_english(self):
        if not _tiny_cached():
            self.skipTest(f"{TINY} not cached")
        from whisper_notes.config import Config
        from whisper_notes.core.engine_mlx import MlxWhisperEngine
        from whisper_notes.core.pipeline import run_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "memo-es.aiff"
            subprocess.run(
                ["say", "-v", "Eddy (Spanish (Spain))", "-o", str(source),
                 "Buenos días a todos. El primer punto del día es la revisión "
                 "de resultados del trimestre."],
                check=True, capture_output=True,
            )
            engine = MlxWhisperEngine(TINY)
            result = run_pipeline(
                source,
                engine=engine,
                config=Config(data_dir=Path(tmp)),
                translate=True,
            )
            self.assertEqual(result.language, "es")  # detected source language
            self.assertIn("translated: true", result.markdown)
            body = result.markdown.split("## Transcript")[1].lower()
            # translated output must not still be Spanish
            for spanish_word in ("buenos", "días", "resultados", "trimestre"):
                self.assertNotIn(spanish_word, body)


if __name__ == "__main__":
    unittest.main()
