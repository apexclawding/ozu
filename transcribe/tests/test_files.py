import tempfile
import unittest
from pathlib import Path

from whisper_notes.store.files import allocate_md_path, sanitize_filename


class TestSanitizeFilename(unittest.TestCase):
    def test_path_traversal_is_stripped(self):
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("/absolute/path.m4a"), "path.m4a")

    def test_unsafe_characters_replaced(self):
        self.assertEqual(sanitize_filename('a:b<c>"d|e?.wav'), "a_b_c__d_e_.wav")

    def test_dotfiles_and_empty_get_fallback(self):
        self.assertEqual(sanitize_filename("..."), "transcript")
        self.assertEqual(sanitize_filename(""), "transcript")

    def test_normal_name_unchanged(self):
        self.assertEqual(sanitize_filename("board-meeting.m4a"), "board-meeting.m4a")


class TestAllocateMdPath(unittest.TestCase):
    def test_collision_gets_numeric_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = allocate_md_path(directory, "meeting.m4a")
            self.assertEqual(first.name, "meeting.md")
            first.write_text("x")
            second = allocate_md_path(directory, "meeting.m4a")
            self.assertEqual(second.name, "meeting-2.md")
            second.write_text("x")
            third = allocate_md_path(directory, "meeting.m4a")
            self.assertEqual(third.name, "meeting-3.md")


if __name__ == "__main__":
    unittest.main()
