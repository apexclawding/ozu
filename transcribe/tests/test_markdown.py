import unittest
from datetime import datetime, timezone

from whisper_notes.core.markdown import (
    NO_SPEECH_BODY,
    TranscriptMeta,
    format_hms,
    render_markdown,
)
from whisper_notes.core.segments import Paragraph

TZ = timezone.utc


def meta(**overrides):
    values = dict(
        title="Board Meeting 2026-08-28",
        source_file="board-meeting.m4a",
        source_sha256="abc123",
        duration_seconds=3862.0,
        language="en",
        language_name="English",
        language_confidence=0.98,
        translated=False,
        model="mlx-community/whisper-large-v3-turbo",
        engine="mlx-whisper 0.4.3",
        transcribed_at=datetime(2026, 8, 28, 17, 42, 11, tzinfo=TZ),
        processing_seconds=287,
    )
    values.update(overrides)
    return TranscriptMeta(**values)


PARAGRAPHS = [
    Paragraph(0.0, "Good morning everyone, thanks for joining."),
    Paragraph(107.0, "Before we do that, I want to flag one thing."),
]


class TestFormatHms(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(format_hms(0), "00:00:00")
        self.assertEqual(format_hms(3862), "01:04:22")
        self.assertEqual(format_hms(59.6), "00:01:00")


class TestFrontmatter(unittest.TestCase):
    def test_schema_keys_exact_order_and_values(self):
        md = render_markdown(meta(), PARAGRAPHS)
        frontmatter = md.split("---\n")[1]
        self.assertEqual(
            frontmatter.splitlines(),
            [
                'title: "Board Meeting 2026-08-28"',
                'source_file: "board-meeting.m4a"',
                'source_sha256: "abc123"',
                'duration: "01:04:22"',
                "duration_seconds: 3862",
                'language: "en"',
                "language_confidence: 0.98",
                "translated: false",
                'model: "mlx-community/whisper-large-v3-turbo"',
                'engine: "mlx-whisper 0.4.3"',
                'transcribed_at: "2026-08-28T17:42:11+00:00"',
                "processing_seconds: 287",
                "realtime_factor: 13.5",
                "word_count: 16",
                "tags: [transcript]",
            ],
        )

    def test_forced_language_yields_null_confidence(self):
        md = render_markdown(meta(language_confidence=None), PARAGRAPHS)
        self.assertIn("language_confidence: null", md)

    def test_quotes_in_title_are_escaped(self):
        md = render_markdown(meta(title='He said "go"'), PARAGRAPHS)
        self.assertIn('title: "He said \\"go\\""', md)


class TestBody(unittest.TestCase):
    def test_heading_summary_line_and_timestamps(self):
        md = render_markdown(meta(), PARAGRAPHS)
        self.assertIn("# Board Meeting 2026-08-28\n", md)
        self.assertIn(
            "> Transcribed locally with Whisper large-v3-turbo · 1h 04m · "
            "English · 2026-08-28\n",
            md,
        )
        self.assertIn("## Transcript\n", md)
        self.assertIn(
            "**[00:00:00]** Good morning everyone, thanks for joining.\n", md
        )
        self.assertIn("**[00:01:47]** Before we do that", md)

    def test_timestamps_toggle_off(self):
        md = render_markdown(meta(), PARAGRAPHS, timestamps=False)
        self.assertNotIn("**[", md)
        self.assertIn("\nGood morning everyone", md)

    def test_summary_line_toggle_off(self):
        md = render_markdown(meta(), PARAGRAPHS, summary_line=False)
        self.assertNotIn("> Transcribed locally", md)

    def test_no_speech_body(self):
        md = render_markdown(meta(), [])
        self.assertIn(NO_SPEECH_BODY, md)
        self.assertIn("word_count: 0", md)

    def test_leading_dashes_escaped_when_timestamps_off(self):
        md = render_markdown(
            meta(), [Paragraph(0.0, "--- looks like a delimiter")], timestamps=False
        )
        self.assertIn("\n\\--- looks like a delimiter\n", md)

    def test_ends_with_newline(self):
        self.assertTrue(render_markdown(meta(), PARAGRAPHS).endswith("\n"))


if __name__ == "__main__":
    unittest.main()
