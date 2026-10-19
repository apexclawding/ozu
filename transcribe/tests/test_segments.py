import unittest

import numpy as np

from whisper_notes.core.engine import Segment
from whisper_notes.core.segments import drop_silent_segments, merge_segments


def seg(start, end, text):
    return Segment(start=start, end=end, text=text)


class TestMergeSegments(unittest.TestCase):
    def test_close_segments_merge_into_one_paragraph(self):
        paragraphs = merge_segments(
            [seg(0.0, 2.0, " Hello there."), seg(2.5, 4.0, "How are you?")]
        )
        self.assertEqual(len(paragraphs), 1)
        self.assertEqual(paragraphs[0].start, 0.0)
        self.assertEqual(paragraphs[0].text, "Hello there. How are you?")

    def test_long_pause_breaks_paragraph(self):
        paragraphs = merge_segments(
            [seg(0.0, 2.0, "First thought."), seg(4.5, 6.0, "New thought.")],
            pause_seconds=2.0,
        )
        self.assertEqual(len(paragraphs), 2)
        self.assertEqual(paragraphs[1].start, 4.5)

    def test_pause_at_threshold_does_not_break(self):
        paragraphs = merge_segments(
            [seg(0.0, 2.0, "A."), seg(4.0, 5.0, "B.")], pause_seconds=2.0
        )
        self.assertEqual(len(paragraphs), 1)

    def test_max_chars_breaks_at_sentence_boundary(self):
        # Third segment pushes past the limit; the last sentence end is after
        # segment two, so the break lands there, not at the overflow point.
        paragraphs = merge_segments(
            [
                seg(0.0, 2.0, "One two three four five six."),
                seg(2.1, 4.0, "Seven eight nine ten eleven twelve."),
                seg(4.1, 6.0, "and it keeps going without a stop"),
            ],
            max_chars=70,
        )
        self.assertEqual(len(paragraphs), 2)
        self.assertTrue(paragraphs[0].text.endswith("twelve."))
        self.assertEqual(paragraphs[1].start, 4.1)

    def test_max_chars_without_sentence_boundary_flushes_whole_buffer(self):
        paragraphs = merge_segments(
            [seg(0.0, 2.0, "no punctuation here at all"), seg(2.1, 4.0, "x" * 60)],
            max_chars=70,
        )
        self.assertEqual(len(paragraphs), 2)

    def test_empty_and_whitespace_segments_are_dropped(self):
        paragraphs = merge_segments(
            [seg(0.0, 1.0, "  "), seg(1.0, 2.0, ""), seg(2.0, 3.0, "Real text.")]
        )
        self.assertEqual(len(paragraphs), 1)
        self.assertEqual(paragraphs[0].start, 2.0)

    def test_no_segments_yields_no_paragraphs(self):
        self.assertEqual(merge_segments([]), [])

    def test_internal_whitespace_is_normalized(self):
        paragraphs = merge_segments([seg(0.0, 1.0, "two\n words   here")])
        self.assertEqual(paragraphs[0].text, "two words here")

    def test_paragraph_starts_are_monotonic(self):
        segments = [seg(i * 3.0, i * 3.0 + 1.0, f"Sentence {i}.") for i in range(20)]
        paragraphs = merge_segments(segments, pause_seconds=1.5)
        starts = [p.start for p in paragraphs]
        self.assertEqual(starts, sorted(starts))


class TestDropSilentSegments(unittest.TestCase):
    SR = 16_000

    def _samples(self, seconds=4.0):
        # First half: digital silence. Second half: a clearly audible tone.
        n = int(seconds * self.SR)
        samples = np.zeros(n, dtype=np.float32)
        t = np.arange(n // 2) / self.SR
        samples[n // 2 :] = 0.1 * np.sin(2 * np.pi * 440 * t).astype(np.float32)
        return samples

    def test_hallucination_over_silence_is_dropped(self):
        kept = drop_silent_segments(
            [seg(0.0, 2.0, "Thank you.")], self._samples(), sample_rate=self.SR
        )
        self.assertEqual(kept, [])

    def test_voiced_segment_is_kept(self):
        segments = [seg(2.0, 4.0, "Real speech.")]
        kept = drop_silent_segments(segments, self._samples(), sample_rate=self.SR)
        self.assertEqual(kept, segments)

    def test_mixed_segments_filtered_correctly(self):
        silent = seg(0.0, 1.9, "Hallucinated.")
        voiced = seg(2.1, 3.9, "Real.")
        kept = drop_silent_segments(
            [silent, voiced], self._samples(), sample_rate=self.SR
        )
        self.assertEqual(kept, [voiced])

    def test_segment_beyond_audio_end_is_dropped(self):
        kept = drop_silent_segments(
            [seg(10.0, 12.0, "Ghost.")], self._samples(), sample_rate=self.SR
        )
        self.assertEqual(kept, [])


if __name__ == "__main__":
    unittest.main()
