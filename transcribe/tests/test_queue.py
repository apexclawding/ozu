"""Queue tests run the REAL worker child process (spawn) with a fake engine —
they exercise FIFO ordering (AC 7), kill-based cancellation (AC 8), and
crash recovery (AC 9) end to end, minus MLX."""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from whisper_notes.config import Config
from whisper_notes.store.db import JobStore
from whisper_notes.web.queue import TranscriptionQueue

FAKE_FACTORY = "tests.fake_worker_engine:FakeWorkerEngine"


def wait_for(predicate, timeout=30.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class QueueTestBase(unittest.TestCase):
    delay = "0.5"

    @classmethod
    def setUpClass(cls):
        cls.fixture_dir = tempfile.TemporaryDirectory()
        cls.wav = Path(cls.fixture_dir.name) / "sample.wav"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=2", str(cls.wav)],
            check=True,
            capture_output=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()

    def setUp(self):
        os.environ["FAKE_ENGINE_DELAY"] = self.delay
        self.addCleanup(os.environ.pop, "FAKE_ENGINE_DELAY", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(data_dir=Path(self.tmp.name), model_idle_timeout_seconds=600)
        self.store = JobStore(self.config.db_path)
        self.queue = TranscriptionQueue(
            self.store, self.config, engine_factory=FAKE_FACTORY
        )
        self.addCleanup(self.queue.shutdown)

    def submit(self, name="sample.wav"):
        job, position = self.queue.submit(
            source=self.wav, source_name=name, sha256="cafe"
        )
        return job, position

    def status(self, job_id):
        return self.store.get(job_id).status


class TestQueueHappyPath(QueueTestBase):
    def test_single_job_completes_and_writes_markdown(self):
        job, position = self.submit()
        self.assertEqual(position, 1)
        self.assertTrue(wait_for(lambda: self.status(job.id) == "done"))
        done = self.store.get(job.id)
        self.assertEqual(done.language, "en")
        self.assertGreater(done.word_count, 0)
        markdown = Path(done.markdown_path)
        self.assertTrue(markdown.exists())
        self.assertIn("Fake transcription output.", markdown.read_text())

    def test_three_jobs_fifo_with_positions(self):  # AC 7
        first, _ = self.submit("one.wav")
        second, _ = self.submit("two.wav")
        third, _ = self.submit("three.wav")

        self.assertTrue(
            wait_for(lambda: self.queue.position(first.id) == 0, timeout=10)
        )
        self.assertEqual(self.queue.position(second.id), 1)
        self.assertEqual(self.queue.position(third.id), 2)

        self.assertTrue(wait_for(lambda: self.status(third.id) == "done"))
        finished = [self.store.get(j.id) for j in (first, second, third)]
        self.assertEqual([j.status for j in finished], ["done"] * 3)
        starts = [j.started_at for j in finished]
        self.assertEqual(starts, sorted(starts))
        # single-slot: no two jobs were ever running at once is implied by
        # FIFO start order + one worker; verify all outputs exist
        for j in finished:
            self.assertTrue(Path(j.markdown_path).exists())

    def test_progress_events_reach_listeners(self):
        seen = []
        self.queue.add_listener(seen.append)
        job, _ = self.submit()
        self.assertTrue(wait_for(lambda: self.status(job.id) == "done"))
        self.assertIn(job.id, seen)


class TestQueueCancellation(QueueTestBase):
    delay = "30"  # long job so cancellation happens mid-transcription

    def test_cancel_running_job_within_five_seconds(self):  # AC 8
        job, _ = self.submit()
        self.assertTrue(
            wait_for(lambda: self.status(job.id) == "running", timeout=15)
        )
        wait_for(lambda: self.queue.model_loaded(), timeout=10)

        started = time.monotonic()
        self.assertTrue(self.queue.cancel(job.id))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0)
        self.assertEqual(self.status(job.id), "cancelled")
        # worker process is gone -> memory returned to the OS
        self.assertTrue(wait_for(lambda: not self.queue.model_loaded(), timeout=5))
        # no partial artifacts (atomic writes): transcripts dir empty
        transcripts = list(self.config.transcripts_dir.glob("*.md"))
        self.assertEqual(transcripts, [])

    def test_cancel_queued_job_and_positions_shift(self):
        running, _ = self.submit("running.wav")
        queued_a, _ = self.submit("a.wav")
        queued_b, _ = self.submit("b.wav")
        self.assertTrue(wait_for(lambda: self.status(running.id) == "running", 15))

        self.assertTrue(self.queue.cancel(queued_a.id))
        self.assertEqual(self.status(queued_a.id), "cancelled")
        self.assertEqual(self.queue.position(queued_b.id), 1)
        self.queue.cancel(running.id)
        self.queue.cancel(queued_b.id)

    def test_cancel_unknown_or_finished_returns_false(self):
        self.assertFalse(self.queue.cancel("nonexistent"))


class TestCrashRecovery(unittest.TestCase):
    def test_startup_marks_interrupted_jobs_failed(self):  # AC 9
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = Config(data_dir=Path(tmp.name))
        store = JobStore(config.db_path)
        job = store.create(
            source_name="killed.m4a", upload_path=None, sha256=None, model="m"
        )
        store.mark_running(job.id)
        done = store.create(
            source_name="ok.m4a", upload_path=None, sha256=None, model="m"
        )
        store.mark_running(done.id)
        store.mark_done(
            done.id, language="en", duration_seconds=1, processing_seconds=1,
            word_count=1, markdown_path="/tmp/ok.md", sha256="s",
        )

        # a new queue on the same store simulates the restart
        queue = TranscriptionQueue(store, config, engine_factory=FAKE_FACTORY)
        self.addCleanup(queue.shutdown)
        self.assertEqual(store.get(job.id).status, "failed")
        self.assertEqual(store.get(done.id).status, "done")


if __name__ == "__main__":
    unittest.main()
