"""P2: §7 parsing and the vault note generation (golden formats, AC 9)."""
import tempfile
import unittest
from pathlib import Path

from ozu_capture import note, transcribe

GOLDEN_MD = '''---
title: "Garden Ideas 2026-08-29"
source_file: "Garden ideas.m4a"
source_sha256: "aabbcc"
duration: "00:07:41"
duration_seconds: 461
language: "es"
language_confidence: 0.97
translated: false
model: "mlx-community/whisper-large-v3-turbo"
engine: "mlx-whisper 0.4.3"
transcribed_at: "2026-08-29T10:00:00+02:00"
processing_seconds: 55
realtime_factor: 8.4
word_count: 812
tags: [transcript]
---

# Garden Ideas 2026-08-29

> Transcribed locally with Whisper large-v3-turbo · 7m 41s · Spanish · 2026-08-29

## Transcript

**[00:00:00]** Primer párrafo del memo con ideas.

**[00:01:30]** Segundo párrafo con más contexto.
'''

NO_SPEECH_MD = '''---
title: "Silence"
language: ""
duration_seconds: 30
---

# Silence

## Transcript

_No speech detected._
'''

# Ozu's INDEX.md scaffold header (vault.py INDEX_HEADER)
INDEX_HEADER = """# Knowledge Base — Index

## How to use this KB (for agents)
1. Read this index first.

## Registry
"""


class TestParse(unittest.TestCase):
    def test_golden_fields_and_body(self):
        tr = transcribe.parse(GOLDEN_MD)
        self.assertEqual(tr.language, "es")
        self.assertAlmostEqual(tr.language_confidence, 0.97)
        self.assertEqual(tr.duration, "00:07:41")
        self.assertEqual(tr.duration_seconds, 461)
        self.assertFalse(tr.no_speech)
        self.assertTrue(tr.body.startswith("**[00:00:00]**"))
        self.assertNotIn("## Transcript", tr.body)
        self.assertNotIn("Transcribed locally", tr.body)

    def test_no_speech_detected(self):
        self.assertTrue(transcribe.parse(NO_SPEECH_MD).no_speech)


class TestWhisperCwd(unittest.TestCase):
    def test_cli_runs_in_its_own_project_dir(self):
        # WHISPER resolves config.toml from the cwd; the bridge must run it
        # at home. The stub writes its cwd where the transcript would go.
        import os
        import sys
        import tempfile
        from tests.test_config_ledger import write_config
        from ozu_capture import config
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "whisper-home"
            home.mkdir()
            stub = root / "stub.py"
            stub.write_text(
                "import sys, pathlib\n"
                "out = pathlib.Path(sys.argv[sys.argv.index('--out')+1])\n"
                "out.write_text(str(pathlib.Path.cwd()))\n")
            os.environ["OZU_CAPTURE_DATA"] = str(root / "data")
            try:
                cfg = config.load(write_config(
                    root, cmd=f'["{sys.executable}", "{stub}"]',
                    whisper_cwd=f'"{home}"'))
                out = root / "out.md"
                transcribe.run_whisper(cfg, root / "a.m4a", out)
                self.assertEqual(out.read_text(), str(home.resolve()))
            finally:
                os.environ.pop("OZU_CAPTURE_DATA", None)

    def test_extra_path_is_prepended_for_the_cli(self):
        # launchd ticks have a minimal PATH; WHISPER needs Homebrew's
        # ffmpeg on it. The stub reports the PATH it was given.
        import os
        import sys
        import tempfile
        from tests.test_config_ledger import write_config
        from ozu_capture import config
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stub = root / "stub.py"
            stub.write_text(
                "import os, sys, pathlib\n"
                "out = pathlib.Path(sys.argv[sys.argv.index('--out')+1])\n"
                "out.write_text(os.environ['PATH'])\n")
            os.environ["OZU_CAPTURE_DATA"] = str(root / "data")
            try:
                cfg = config.load(write_config(
                    root, cmd=f'["{sys.executable}", "{stub}"]',
                    whisper_extra_path='["/fake/homebrew/bin"]'))
                out = root / "out.md"
                transcribe.run_whisper(cfg, root / "a.m4a", out)
                self.assertTrue(
                    out.read_text().startswith("/fake/homebrew/bin:"))
            finally:
                os.environ.pop("OZU_CAPTURE_DATA", None)


class TestNote(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        (self.vault / "INDEX.md").write_text(INDEX_HEADER)   # a stale legacy file: must stay untouched
        self.tr = transcribe.parse(GOLDEN_MD)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, **kw):
        args = dict(audio_name="Garden ideas.m4a", sha="aabbcc",
                    recorded="2026-08-29", today="2026-08-29", tr=self.tr)
        args.update(kw)
        return note.write_note(self.vault, "voice", **args)

    def test_note_conforms(self):
        note_id, rel = self.write()
        self.assertEqual(note_id, "20260829-garden-ideas")
        self.assertEqual(rel, "topics/voice/20260829-garden-ideas.md")   # filename = id
        text = (self.vault / rel).read_text()
        head = text.split("\n---\n")[0]
        for line in ("type: Voice Memo", "id: 20260829-garden-ideas", "title: Garden ideas",
                     "description: Voice memo (7m 41s, Spanish), transcribed on-device 2026-08-29.",
                     "tags: [voice, transcript]", "created: 2026-08-29",
                     "source: voice:Garden ideas.m4a", "source_sha256: aabbcc",
                     'duration: "00:07:41"', "language: es", "related: []",
                     "needs_review: false", "generated: { by: ozu-capture/whisper, at: 20",
                     "sources:\n  - id: audio\n    resource: \"on-device audio: Garden ideas.m4a\""):
            self.assertIn(line, head, line)
        self.assertNotIn("summary:", head)
        self.assertNotIn("status:", head)
        # single frontmatter block; body is the paragraph stream
        self.assertEqual(text.count("\n---\n"), 1)
        self.assertIn("# Garden ideas\n\n> Summary: Voice memo (7m 41s, "
                      "Spanish), transcribed on-device 2026-08-29.", text)
        self.assertIn("**[00:00:00]**", text)

    def test_no_registry_is_touched(self):
        # OKF (2026-09-02): index.md and log.md are derived by the VPS from the
        # note's frontmatter. The bridge writes the note and nothing else —
        # the single-writer rule the project board asked for.
        self.write()
        self.assertEqual((self.vault / "INDEX.md").read_text(), INDEX_HEADER)
        self.assertFalse((self.vault / "topics/voice/_topic.md").exists())
        self.assertFalse((self.vault / "topics/voice/index.md").exists())
        # case-exact: on APFS, Path("index.md").exists() matches the seeded
        # INDEX.md and this check silently passes-by-collision on Linux only
        self.assertNotIn("index.md", [p.name for p in self.vault.iterdir()])
        self.assertEqual([p.name for p in (self.vault / "topics/voice").iterdir()],
                         ["20260829-garden-ideas.md"])

    def test_frontmatter_is_strict_yaml(self):
        # a title with a colon, a sexagesimal duration: both would break a
        # YAML parser unquoted, and both are what OKF consumers parse with
        _, rel = self.write(audio_name="Plan: casa nueva.m4a")
        head = (self.vault / rel).read_text().split("\n---\n")[0]
        self.assertIn('title: "Plan: casa nueva"', head)
        self.assertIn('source: "voice:Plan: casa nueva.m4a"', head)
        self.assertIn('duration: "00:07:41"', head)

    def test_collision_suffix_matches_vault_py(self):
        id1, rel1 = self.write()
        id2, rel2 = self.write(sha="ddeeff")
        self.assertEqual((id1, id2),
                         ("20260829-garden-ideas", "20260829-garden-ideas-2"))
        self.assertEqual(rel2, "topics/voice/20260829-garden-ideas-2.md")

    def test_low_confidence_sets_needs_review(self):
        tr = transcribe.parse(GOLDEN_MD.replace(
            "language_confidence: 0.97", "language_confidence: 0.55"))
        _, rel = self.write(tr=tr)
        text = (self.vault / rel).read_text()
        self.assertIn("needs_review: true", text)
        self.assertIn("status: draft", text)          # OKF lifecycle: not yet reviewed

    def test_vault_has_sha_backstop(self):
        self.assertIsNone(note.vault_has_sha(self.vault, "voice", "aabbcc"))
        self.write()
        self.assertEqual(note.vault_has_sha(self.vault, "voice", "aabbcc"),
                         "20260829-garden-ideas")
        self.assertIsNone(note.vault_has_sha(self.vault, "voice", "other"))


if __name__ == "__main__":
    unittest.main()
