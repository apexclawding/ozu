"""§8.1 endpoint tests against a REAL uvicorn server on a random loopback
port, driven with stdlib urllib — no test-client dependencies. The worker is
the real spawned child process with the fake engine."""

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

from whisper_notes.config import Config
from whisper_notes.store.db import JobStore
from whisper_notes.web.app import create_app
from whisper_notes.web.queue import TranscriptionQueue

FAKE_FACTORY = "tests.fake_worker_engine:FakeWorkerEngine"
BOUNDARY = "testboundary1234567890"


def multipart(filename: str, content: bytes, fields: dict | None = None):
    parts = []
    for key, value in (fields or {}).items():
        parts.append(
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{key}"'
            f"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\nContent-Type: application/octet-stream'
        f"\r\n\r\n".encode()
    )
    body = b"".join(parts) + content + f"\r\n--{BOUNDARY}--\r\n".encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}


def wait_for(predicate, timeout=30.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class ApiTestBase(unittest.TestCase):
    delay = "0.3"

    @classmethod
    def setUpClass(cls):
        cls.fixture_dir = tempfile.TemporaryDirectory()
        cls.wav = Path(cls.fixture_dir.name) / "meeting-notes.wav"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=2", str(cls.wav)],
            check=True,
            capture_output=True,
        )
        cls.wav_bytes = cls.wav.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()

    def setUp(self):
        os.environ["FAKE_ENGINE_DELAY"] = self.delay
        self.addCleanup(os.environ.pop, "FAKE_ENGINE_DELAY", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(data_dir=Path(self.tmp.name))
        self.store = JobStore(self.config.db_path)
        self.queue = TranscriptionQueue(
            self.store,
            self.config,
            engine_factory=getattr(self, "factory_override", FAKE_FACTORY),
        )
        self.addCleanup(self.queue.shutdown)

        app = create_app(self.config, self.store, self.queue)
        server_config = uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="error"
        )
        self.server = uvicorn.Server(server_config)
        thread = threading.Thread(target=self.server.run, daemon=True)
        thread.start()
        self.assertTrue(wait_for(lambda: self.server.started, timeout=10))
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"

        def stop_server():
            self.server.should_exit = True
            thread.join(timeout=10)

        self.addCleanup(stop_server)

    def request(self, method, path, body=None, headers=None, timeout=15):
        req = urllib.request.Request(
            self.base + path, data=body, headers=headers or {}, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.headers, resp.read()  # case-insensitive
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def get_json(self, path):
        status, _, data = self.request("GET", path)
        return status, json.loads(data)

    def submit(self, filename="meeting-notes.wav", content=None, fields=None):
        if content is None:
            content = self.wav_bytes
        body, headers = multipart(filename, content, fields)
        status, _, data = self.request("POST", "/api/jobs", body, headers)
        return status, json.loads(data)

    def wait_done(self, job_id):
        self.assertTrue(
            wait_for(
                lambda: self.get_json(f"/api/jobs/{job_id}")[1]["status"] == "done"
            ),
            "job did not complete",
        )


class TestCoreEndpoints(ApiTestBase):
    def test_health(self):
        status, payload = self.get_json("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["ffmpeg_ok"])
        self.assertIn("mlx-whisper", payload["engine"])
        self.assertEqual(payload["model"], self.config.model_repo)
        self.assertIn("model_loaded", payload)
        self.assertEqual(payload["version"], "0.1.0")

    def test_config(self):
        status, payload = self.get_json("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(payload["port"], 8420)
        self.assertIn(".m4a", payload["allowed_extensions"])

    def test_index_serves_html(self):
        status, headers, data = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])

    def test_submit_poll_download_preview(self):
        status, payload = self.submit()
        self.assertEqual(status, 200)
        self.assertFalse(payload["duplicate"])
        self.assertEqual(payload["queue_position"], 1)
        job_id = payload["job_id"]

        self.wait_done(job_id)
        _, job = self.get_json(f"/api/jobs/{job_id}")
        self.assertEqual(job["phase"], "done")
        self.assertEqual(job["percent"], 100)
        self.assertEqual(job["language"], "en")
        self.assertTrue(job["has_markdown"])
        self.assertIsNotNone(job["elapsed"])
        self.assertEqual(job["source_name"], "meeting-notes.wav")

        status, headers, data = self.request("GET", f"/api/jobs/{job_id}/markdown")
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", headers["Content-Type"])
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("meeting-notes.md", headers["Content-Disposition"])
        self.assertTrue(data.startswith(b"---"))
        self.assertIn(b"Fake transcription output.", data)

        status, payload = self.get_json(f"/api/jobs/{job_id}/preview?n=2000")
        self.assertEqual(status, 200)
        self.assertTrue(payload["preview"].startswith("# "))
        self.assertIn("Fake transcription output.", payload["preview"])

        status, listing = self.get_json("/api/jobs")
        self.assertEqual(status, 200)
        self.assertIn(job_id, [j["job_id"] for j in listing])

    def test_duplicate_offers_cached_result(self):
        _, first = self.submit()
        self.wait_done(first["job_id"])
        status, second = self.submit()
        self.assertEqual(status, 200)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["job_id"], first["job_id"])
        # force=true bypasses the cache (FR-06 is an offer, not a wall)
        _, forced = self.submit(fields={"force": "true"})
        self.assertFalse(forced["duplicate"])
        self.assertNotEqual(forced["job_id"], first["job_id"])

    def test_unsupported_extension_rejected(self):
        status, payload = self.submit(filename="notes.txt")
        self.assertEqual(status, 415)
        self.assertIn("not a supported format", payload["detail"])

    def test_empty_file_rejected(self):
        status, payload = self.submit(content=b"")
        self.assertEqual(status, 422)

    def test_bad_language_rejected(self):
        status, payload = self.submit(fields={"language": "english!"})
        self.assertEqual(status, 422)

    def test_unknown_job_404(self):
        for path in ("/api/jobs/nope", "/api/jobs/nope/markdown",
                     "/api/jobs/nope/preview", "/api/jobs/nope/events"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(status, 404, path)

    def test_markdown_conflict_before_done(self):
        job = self.store.create(
            source_name="x.wav", upload_path=None, sha256=None, model="m"
        )
        status, _, _ = self.request("GET", f"/api/jobs/{job.id}/markdown")
        self.assertEqual(status, 409)

    def test_corrupt_file_fails_with_plain_language_error(self):
        # error matrix: "Unsupported / corrupt file" — accepted extension,
        # garbage bytes; the job must fail with the ffprobe reason, no traceback
        status, payload = self.submit(filename="broken.wav", content=b"not audio")
        self.assertEqual(status, 200)
        job_id = payload["job_id"]
        self.assertTrue(
            wait_for(
                lambda: self.get_json(f"/api/jobs/{job_id}")[1]["status"] == "failed"
            )
        )
        _, job = self.get_json(f"/api/jobs/{job_id}")
        self.assertIn("could not be read", job["error"])
        self.assertNotIn("Traceback", job["error"])

    def test_delete_done_job_removes_artifacts(self):
        _, payload = self.submit()
        job_id = payload["job_id"]
        self.wait_done(job_id)
        job = self.store.get(job_id)
        upload = Path(job.upload_path)
        markdown = Path(job.markdown_path)
        self.assertTrue(upload.exists())
        self.assertTrue(markdown.exists())

        status, _, _ = self.request("DELETE", f"/api/jobs/{job_id}")
        self.assertEqual(status, 200)
        self.assertFalse(upload.exists())
        self.assertFalse(markdown.exists())
        status, _, _ = self.request("GET", f"/api/jobs/{job_id}")
        self.assertEqual(status, 404)


class TestModelEndpoints(ApiTestBase):
    def test_cached_model_reports_ready(self):
        status, payload = self.get_json("/api/model")
        self.assertEqual(status, 200)
        self.assertTrue(payload["cached"])
        self.assertFalse(payload["downloading"])

    def test_prefetch_on_cached_model_is_a_noop(self):
        status, _, data = self.request("POST", "/api/model/prefetch", b"")
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertFalse(payload["started"])
        self.assertTrue(payload["cached"])


class TestTranslateCapabilityGate(ApiTestBase):
    """FR-13 on a transcription-only model: reject at submit, expose in config."""

    def setUp(self):
        self.factory_override = "tests.fake_worker_engine:FakeNoTranslateEngine"
        super().setUp()

    def test_config_reports_capability(self):
        _, payload = self.get_json("/api/config")
        self.assertFalse(payload["supports_translation"])

    def test_translate_submission_is_rejected_with_remedy(self):
        status, payload = self.submit(fields={"translate": "true"})
        self.assertEqual(status, 422)
        self.assertIn("cannot translate", payload["detail"])
        self.assertIn("whisper-medium-mlx", payload["detail"])

    def test_plain_submission_still_works(self):
        status, payload = self.submit()
        self.assertEqual(status, 200)
        self.wait_done(payload["job_id"])


class TestModelConsentFlow(ApiTestBase):
    """First run: the model is absent until the user explicitly consents (R2)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from tests.fake_worker_engine import FakeUncachedEngine

        FakeUncachedEngine._prefetched = False

    def setUp(self):
        self.factory_override = "tests.fake_worker_engine:FakeUncachedEngine"
        super().setUp()

    def test_uncached_then_prefetch_then_cached(self):
        status, payload = self.get_json("/api/model")
        self.assertFalse(payload["cached"])
        self.assertFalse(payload["downloading"])

        status, _, data = self.request("POST", "/api/model/prefetch", b"")
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertTrue(payload["started"] or payload["cached"])

        self.assertTrue(
            wait_for(lambda: self.get_json("/api/model")[1]["cached"], timeout=10)
        )


class TestSseAndCancel(ApiTestBase):
    delay = "3"

    def test_sse_streams_progress_to_terminal_state(self):
        _, payload = self.submit()
        job_id = payload["job_id"]

        req = urllib.request.Request(self.base + f"/api/jobs/{job_id}/events")
        events = []
        with urllib.request.urlopen(req, timeout=30) as resp:
            self.assertIn("text/event-stream", resp.headers["Content-Type"])
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))
                    if events[-1]["status"] in ("done", "failed", "cancelled"):
                        break

        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[-1]["status"], "done")
        phases = [e["phase"] for e in events]
        self.assertIn("transcribing", phases)
        percents = [e["percent"] for e in events if e["phase"] == "transcribing"
                    and e["percent"] is not None]
        self.assertEqual(percents, sorted(percents))

    def test_delete_running_job_cancels_and_keeps_history(self):
        _, payload = self.submit()
        job_id = payload["job_id"]
        self.assertTrue(
            wait_for(
                lambda: self.get_json(f"/api/jobs/{job_id}")[1]["status"] == "running",
                timeout=15,
            )
        )
        upload = Path(self.store.get(job_id).upload_path)

        started = time.monotonic()
        status, _, data = self.request("DELETE", f"/api/jobs/{job_id}")
        elapsed = time.monotonic() - started
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["cancelled"])
        self.assertLess(elapsed, 5.0)  # FR-18

        _, job = self.get_json(f"/api/jobs/{job_id}")
        self.assertEqual(job["status"], "cancelled")
        self.assertFalse(upload.exists())


if __name__ == "__main__":
    unittest.main()
