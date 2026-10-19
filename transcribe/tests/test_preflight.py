"""P0 exit criterion: the ffmpeg preflight detects absence correctly (FR-02)."""

import os
import unittest
from unittest import mock

from whisper_notes.preflight import PreflightError, check_ffmpeg, check_platform


class TestCheckFfmpeg(unittest.TestCase):
    def test_missing_ffmpeg_is_detected_with_remediation(self):
        # An empty PATH guarantees ffmpeg is unfindable regardless of the machine.
        with mock.patch.dict(os.environ, {"PATH": ""}):
            with self.assertRaises(PreflightError) as ctx:
                check_ffmpeg()
        self.assertIn("brew install ffmpeg", str(ctx.exception))

    def test_present_ffmpeg_returns_executable_path(self):
        path = check_ffmpeg()
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.access(path, os.X_OK))


class TestCheckPlatform(unittest.TestCase):
    def test_passes_on_this_machine(self):
        check_platform()  # target machine is Darwin/arm64; must not raise

    def test_rejects_non_arm64(self):
        with mock.patch("platform.machine", return_value="x86_64"):
            with self.assertRaises(PreflightError):
                check_platform()


if __name__ == "__main__":
    unittest.main()
