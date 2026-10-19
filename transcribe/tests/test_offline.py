"""NFR-01 / NFR-06 / AC 10: the full pipeline succeeds with network access
poisoned. Every HTTP client in the stack honors proxy environment variables,
so pointing them at a dead loopback port makes any network attempt fail fast —
if the transcription still succeeds, nothing needed the network.

Skipped automatically when the model is not in the local cache (fresh CI
machine); it runs the REAL engine, so it needs the one-time download done.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def _model_cached() -> bool:
    try:
        from whisper_notes.config import load_config
        from whisper_notes.core.engine_mlx import MlxWhisperEngine

        config = load_config(PROJECT / "config.toml")
        return MlxWhisperEngine(config.model_repo).is_cached()
    except Exception:
        return False


@unittest.skipUnless(
    sys.platform == "darwin" and shutil.which("say"), "needs macOS 'say'"
)
class TestOfflinePipeline(unittest.TestCase):
    def test_transcription_succeeds_with_network_poisoned(self):
        if not _model_cached():
            self.skipTest("model not cached; run: uv run whisper-notes prefetch-model")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "offline-check.aiff"
            subprocess.run(
                ["say", "-o", str(source),
                 "This transcription ran with networking completely disabled."],
                check=True, capture_output=True,
            )
            out = Path(tmp) / "offline-check.md"
            env = {
                **os.environ,
                "WHISPER_OFFLINE": "1",
                "HTTP_PROXY": "http://127.0.0.1:9",
                "HTTPS_PROXY": "http://127.0.0.1:9",
                "http_proxy": "http://127.0.0.1:9",
                "https_proxy": "http://127.0.0.1:9",
                "NO_PROXY": "",
                "no_proxy": "",
            }
            result = subprocess.run(
                [str(PROJECT / ".venv/bin/whisper-notes"),
                 "transcribe", str(source), "--out", str(out)],
                env=env, cwd=PROJECT, capture_output=True, timeout=300,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            markdown = out.read_text(encoding="utf-8")
            self.assertIn("## Transcript", markdown)
            self.assertIn("networking", markdown.lower())


if __name__ == "__main__":
    unittest.main()
