"""Exercises real ffmpeg against generated fixtures (ffmpeg is a hard
prerequisite of the project, so tests may assume it)."""

import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from whisper_notes.core.audio import SAMPLE_RATE, AudioError, decode, probe


def _ffmpeg(*args):
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", *args],
        check=True,
        capture_output=True,
    )


class TestAudio(unittest.TestCase):
    tmp: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.tone_wav = root / "tone.wav"
        _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(cls.tone_wav))
        cls.tone_m4a = root / "tone.m4a"
        _ffmpeg("-i", str(cls.tone_wav), str(cls.tone_m4a))
        cls.video_only = root / "video.mp4"
        _ffmpeg(
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-an", str(cls.video_only)
        )
        cls.garbage = root / "garbage.wav"
        cls.garbage.write_bytes(b"not audio at all")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_probe_reads_duration(self):
        info = probe(self.tone_wav)
        self.assertAlmostEqual(info.duration_seconds, 2.0, delta=0.1)
        self.assertGreater(info.size_bytes, 0)

    def test_probe_m4a_container(self):
        info = probe(self.tone_m4a)
        self.assertAlmostEqual(info.duration_seconds, 2.0, delta=0.1)
        self.assertEqual(info.codec, "aac")

    def test_probe_rejects_video_without_audio(self):
        with self.assertRaises(AudioError) as ctx:
            probe(self.video_only)
        self.assertIn("no audio track", str(ctx.exception))

    def test_probe_rejects_garbage(self):
        with self.assertRaises(AudioError):
            probe(self.garbage)

    def test_probe_rejects_missing_file(self):
        with self.assertRaises(AudioError):
            probe(Path(self.tmp.name) / "nope.wav")

    def test_decode_is_16k_mono_float32(self):
        samples = decode(self.tone_wav)
        self.assertEqual(samples.dtype, np.float32)
        self.assertEqual(samples.ndim, 1)
        self.assertAlmostEqual(len(samples) / SAMPLE_RATE, 2.0, delta=0.1)
        self.assertLessEqual(float(np.abs(samples).max()), 1.0)

    def test_decode_m4a_matches_duration(self):
        samples = decode(self.tone_m4a)
        self.assertAlmostEqual(len(samples) / SAMPLE_RATE, 2.0, delta=0.15)


if __name__ == "__main__":
    unittest.main()
