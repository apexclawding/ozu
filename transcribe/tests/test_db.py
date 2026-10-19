import tempfile
import unittest
from pathlib import Path

from whisper_notes.store.db import JobStore


class TestJobStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = JobStore(Path(self.tmp.name) / "jobs.db")

    def _create(self, **overrides):
        values = dict(
            source_name="a.m4a",
            upload_path="/tmp/a.m4a",
            sha256="deadbeef",
            model="test-model",
        )
        values.update(overrides)
        return self.store.create(**values)

    def test_create_and_get(self):
        job = self._create()
        fetched = self.store.get(job.id)
        self.assertEqual(fetched.status, "queued")
        self.assertEqual(fetched.source_name, "a.m4a")
        self.assertFalse(fetched.translate)

    def test_get_unknown_returns_none(self):
        self.assertIsNone(self.store.get("nope"))

    def test_list_newest_first(self):
        first = self._create(source_name="first.m4a")
        second = self._create(source_name="second.m4a")
        listed = self.store.list()
        self.assertEqual([j.id for j in listed[:2]], [second.id, first.id])

    def test_status_transitions(self):
        job = self._create()
        self.store.mark_running(job.id)
        self.assertEqual(self.store.get(job.id).status, "running")
        self.store.update_progress(job.id, "transcribing", 42.0)
        fetched = self.store.get(job.id)
        self.assertEqual(fetched.phase, "transcribing")
        self.assertEqual(fetched.percent, 42.0)
        self.store.mark_done(
            job.id,
            language="en",
            duration_seconds=10.0,
            processing_seconds=2,
            word_count=5,
            markdown_path="/tmp/a.md",
            sha256="deadbeef",
        )
        fetched = self.store.get(job.id)
        self.assertEqual(fetched.status, "done")
        self.assertEqual(fetched.percent, 100)
        self.assertIsNotNone(fetched.finished_at)

    def test_progress_ignored_after_terminal_state(self):
        job = self._create()
        self.store.mark_running(job.id)
        self.store.mark_cancelled(job.id)
        self.store.update_progress(job.id, "transcribing", 99.0)
        self.assertEqual(self.store.get(job.id).status, "cancelled")
        self.assertNotEqual(self.store.get(job.id).percent, 99.0)

    def test_fail_interrupted_marks_running_and_queued(self):
        running = self._create()
        self.store.mark_running(running.id)
        queued = self._create()
        done = self._create()
        self.store.mark_running(done.id)
        self.store.mark_done(
            done.id, language="en", duration_seconds=1, processing_seconds=1,
            word_count=1, markdown_path="/tmp/x.md", sha256="d",
        )
        count = self.store.fail_interrupted()
        self.assertEqual(count, 2)
        self.assertEqual(self.store.get(running.id).status, "failed")
        self.assertEqual(self.store.get(queued.id).status, "failed")
        self.assertEqual(self.store.get(done.id).status, "done")
        self.assertIn("restart", self.store.get(running.id).error)

    def test_find_duplicate_matches_settings(self):
        job = self._create()
        self.store.mark_running(job.id)
        self.store.mark_done(
            job.id, language="en", duration_seconds=1, processing_seconds=1,
            word_count=1, markdown_path="/tmp/a.md", sha256="deadbeef",
        )
        hit = self.store.find_duplicate(
            sha256="deadbeef", model="test-model",
            translate=False, language_override=None,
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit.id, job.id)
        self.assertIsNone(
            self.store.find_duplicate(
                sha256="deadbeef", model="other-model",
                translate=False, language_override=None,
            )
        )
        self.assertIsNone(
            self.store.find_duplicate(
                sha256="deadbeef", model="test-model",
                translate=True, language_override=None,
            )
        )

    def test_delete(self):
        job = self._create()
        self.store.delete(job.id)
        self.assertIsNone(self.store.get(job.id))


if __name__ == "__main__":
    unittest.main()
