"""Single-slot transcription worker: FIFO queue, cancellation, model lifecycle
(FR-16, FR-17, FR-18).

Inference runs in ONE child process that stays alive between jobs, keeping the
model warm (FR-17). Cancelling a running job and releasing an idle model are
both process kills: with a blocking Metal inference loop, killing the process
is the only way to guarantee memory is actually returned within FR-18's
5-second bound. After a kill, the next job simply respawns the worker (a few
seconds of model reload — the rare path, not the hot path).

The parent manager thread is the single writer of job state transitions.
"""

from __future__ import annotations

import importlib
import multiprocessing
import queue as stdlib_queue
import threading
import traceback
from collections import deque
from collections.abc import Callable
from pathlib import Path

from whisper_notes.config import Config
from whisper_notes.logs import get_logger
from whisper_notes.store.db import JobStore

_CTX = multiprocessing.get_context("spawn")
_log = get_logger("queue")

DEFAULT_ENGINE_FACTORY = "whisper_notes.core.engine_mlx:MlxWhisperEngine"

# How long to wait for a terminate() before escalating to kill() — the total
# stays well inside FR-18's 5-second budget.
_TERMINATE_GRACE = 2.0

# A listener receives a job id whenever that job's state changed; it reads
# current state from the store. Called on the manager thread — must not block.
Listener = Callable[[str], None]


def _config_to_spec(config: Config) -> dict:
    return {
        "port": config.port,
        "model_repo": config.model_repo,
        "model_idle_timeout_seconds": config.model_idle_timeout_seconds,
        "timestamps": config.timestamps,
        "summary_line": config.summary_line,
        "paragraph_pause_seconds": config.paragraph_pause_seconds,
        "paragraph_max_chars": config.paragraph_max_chars,
        "max_file_gb": config.max_file_gb,
        "max_duration_hours": config.max_duration_hours,
        "data_dir": str(config.data_dir),
    }


def _spec_to_config(spec: dict) -> Config:
    values = dict(spec)
    values["data_dir"] = Path(values["data_dir"])
    return Config(**values)


def _peak_rss_bytes() -> int:
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # bytes on macOS


def _worker_main(job_queue, event_queue) -> None:
    """Child process body: run jobs sequentially, engine kept warm between."""
    while True:
        spec = job_queue.get()
        if spec is None:  # graceful shutdown (idle release / server stop)
            return
        job_id = spec["job_id"]
        try:
            config = _spec_to_config(spec["config"])
            from whisper_notes.logs import setup_logging

            setup_logging(config.data_dir)
            module_name, _, attr = spec["engine_factory"].partition(":")
            engine_cls = getattr(importlib.import_module(module_name), attr)
            engine = engine_cls(spec["model"])

            last = {"phase": None, "percent": -2.0}

            def progress(phase: str, percent: float | None) -> None:
                changed_phase = phase != last["phase"]
                stepped = percent is not None and percent - last["percent"] >= 1.0
                if changed_phase or stepped:
                    last["phase"], last["percent"] = phase, percent or 0.0
                    event_queue.put(("progress", job_id, phase, percent))

            from whisper_notes.core.pipeline import run_pipeline

            result = run_pipeline(
                Path(spec["source"]),
                engine=engine,
                config=config,
                language=spec["language"],
                translate=spec["translate"],
                progress=progress,
            )
            event_queue.put(
                (
                    "done",
                    job_id,
                    {
                        "language": result.language,
                        "duration_seconds": result.duration_seconds,
                        "processing_seconds": result.processing_seconds,
                        "word_count": result.word_count,
                        "markdown_path": str(result.markdown_path),
                        "sha256": result.sha256,
                    },
                    {"peak_rss_bytes": _peak_rss_bytes(),
                     "realtime_factor": result.realtime_factor},
                )
            )
        except MemoryError:
            event_queue.put(("failed", job_id, _OOM_MESSAGE, None))
        except Exception as e:  # noqa: BLE001 — the job must fail cleanly (FR-38)
            from whisper_notes.core.audio import AudioError
            from whisper_notes.core.engine import EngineError
            from whisper_notes.core.pipeline import PipelineError

            if isinstance(e, (AudioError, EngineError, PipelineError)):
                message = str(e)  # already user-facing
            elif "memory" in str(e).lower():
                message = _OOM_MESSAGE
            else:
                message = "Transcription failed unexpectedly. See the log for details."
            event_queue.put(("failed", job_id, message, traceback.format_exc()))


_OOM_MESSAGE = (
    "The transcription ran out of memory. Close other applications, or switch "
    'config.toml to the smaller model: repo = '
    '"mlx-community/whisper-large-v3-turbo-q4"'
)

_WORKER_DIED_MESSAGE = (
    "The transcription worker stopped unexpectedly — most likely the system "
    "ran out of memory. Close other applications, or switch config.toml to "
    'the smaller model: repo = "mlx-community/whisper-large-v3-turbo-q4"'
)


class TranscriptionQueue:
    def __init__(
        self,
        store: JobStore,
        config: Config,
        *,
        engine_factory: str = DEFAULT_ENGINE_FACTORY,
    ):
        self._store = store
        self._config = config
        self._engine_factory = engine_factory

        interrupted = store.fail_interrupted()  # NFR-05
        if interrupted:
            _log.warning("marked %d interrupted job(s) as failed on startup", interrupted)

        self._lock = threading.RLock()
        self._pending: deque[dict] = deque()
        self._running_id: str | None = None
        self._child: multiprocessing.process.BaseProcess | None = None
        self._job_queue = None
        self._event_queue = None
        self._killed_running = False
        self._listeners: list[Listener] = []
        self._idle_timer: threading.Timer | None = None
        self._wake = threading.Event()
        self._stopped = False
        self._manager = threading.Thread(
            target=self._run, name="queue-manager", daemon=True
        )
        self._manager.start()

    # -- public API ----------------------------------------------------------

    @property
    def engine_factory(self) -> str:
        return self._engine_factory

    def submit(
        self,
        *,
        source: Path,
        source_name: str,
        sha256: str | None,
        language: str | None = None,
        translate: bool = False,
    ):
        """Queue a job (FR-16). Returns (job, queue_position)."""
        job = self._store.create(
            source_name=source_name,
            upload_path=str(source),
            sha256=sha256,
            model=self._config.model_repo,
            translate=translate,
            language_override=language,
        )
        spec = {
            "job_id": job.id,
            "source": str(source),
            "model": self._config.model_repo,
            "language": language,
            "translate": translate,
            "config": _config_to_spec(self._config),
            "engine_factory": self._engine_factory,
        }
        with self._lock:
            self._pending.append(spec)
            position = self.position(job.id)
        self._wake.set()
        self._notify(job.id)
        return job, position

    def position(self, job_id: str) -> int | None:
        """0 = running now; 1.. = place in line; None = not active (FR-16)."""
        with self._lock:
            if job_id == self._running_id:
                return 0
            for i, spec in enumerate(self._pending):
                if spec["job_id"] == job_id:
                    return i + 1
        return None

    def cancel(self, job_id: str) -> bool:
        """FR-18: cancel a queued or running job; frees resources within 5 s."""
        with self._lock:
            for spec in self._pending:
                if spec["job_id"] == job_id:
                    self._pending.remove(spec)
                    self._store.mark_cancelled(job_id)
                    self._notify(job_id)
                    self._notify_pending()
                    return True
            if job_id == self._running_id:
                self._killed_running = True
                self._kill_child()  # memory returned to the OS immediately
                self._store.mark_cancelled(job_id)
                self._notify(job_id)
                return True
        return False

    def add_listener(self, listener: Listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def model_loaded(self) -> bool:
        """Whether the warm worker process currently exists (for /api/health)."""
        with self._lock:
            return self._child is not None and self._child.is_alive()

    def shutdown(self) -> None:
        self._stopped = True
        self._wake.set()
        with self._lock:
            self._cancel_idle_timer()
            self._kill_child()
        self._manager.join(timeout=5)

    # -- manager thread -------------------------------------------------------

    def _run(self) -> None:
        while not self._stopped:
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            while not self._stopped:
                with self._lock:
                    if self._running_id is not None or not self._pending:
                        break
                    spec = self._pending.popleft()
                    self._running_id = spec["job_id"]
                    self._killed_running = False
                    self._cancel_idle_timer()
                self._store.mark_running(spec["job_id"])
                _log.info("job %s started: %s", spec["job_id"], spec["source"])
                self._notify(spec["job_id"])
                self._notify_pending()
                try:
                    self._run_one(spec)
                finally:
                    with self._lock:
                        self._running_id = None
                        if not self._pending:
                            self._start_idle_timer()
                    self._wake.set()  # pick up the next queued job

    def _run_one(self, spec: dict) -> None:
        job_id = spec["job_id"]
        last_phase = None
        with self._lock:
            self._ensure_child()
            event_queue = self._event_queue
        self._job_queue.put(spec)

        while True:
            try:
                event = event_queue.get(timeout=0.5)
            except stdlib_queue.Empty:
                with self._lock:
                    child_alive = self._child is not None and self._child.is_alive()
                    killed = self._killed_running
                if killed:
                    _log.info("job %s cancelled while running; worker killed", job_id)
                    return  # cancel() already marked the job
                if not child_alive:
                    message = (
                        "Interrupted by server shutdown."
                        if self._stopped
                        else _WORKER_DIED_MESSAGE
                    )
                    _log.error("job %s: worker process died unexpectedly", job_id)
                    self._store.mark_failed(job_id, message)
                    self._notify(job_id)
                    return
                continue

            kind = event[0]
            if event[1] != job_id:
                continue  # stale event from a previous job's queue
            if kind == "progress":
                _, _, phase, percent = event
                if phase != last_phase:
                    last_phase = phase
                    _log.info("job %s phase: %s", job_id, phase)
                self._store.update_progress(job_id, phase, percent)
                self._notify(job_id)
            elif kind == "done":
                self._store.mark_done(job_id, **event[2])
                stats = event[3] if len(event) > 3 else {}
                _log.info(
                    "job %s done: duration=%.1fs processing=%ds realtime=%.1fx "
                    "words=%d peak_rss=%.2fGB",
                    job_id,
                    event[2]["duration_seconds"],
                    event[2]["processing_seconds"],
                    stats.get("realtime_factor", 0.0),
                    event[2]["word_count"],
                    stats.get("peak_rss_bytes", 0) / 1024**3,
                )
                self._notify(job_id)
                return
            elif kind == "failed":
                _, _, message, trace = event
                _log.error("job %s failed: %s%s", job_id, message,
                           f"\n{trace}" if trace else "")
                self._store.mark_failed(job_id, message)
                self._notify(job_id)
                return

    # -- child process lifecycle ----------------------------------------------

    def _ensure_child(self) -> None:
        if self._child is not None and self._child.is_alive():
            return
        self._job_queue = _CTX.Queue()
        self._event_queue = _CTX.Queue()
        self._child = _CTX.Process(
            target=_worker_main,
            args=(self._job_queue, self._event_queue),
            daemon=True,
            name="transcription-worker",
        )
        self._child.start()

    def _kill_child(self) -> None:
        child = self._child
        self._child = None
        if child is None or not child.is_alive():
            return
        child.terminate()
        child.join(timeout=_TERMINATE_GRACE)
        if child.is_alive():
            child.kill()
            child.join(timeout=_TERMINATE_GRACE)

    def _release_idle(self) -> None:
        """FR-17: drop the warm model after the idle timeout."""
        with self._lock:
            if self._running_id is not None or self._pending:
                return
            child = self._child
            if child is None or not child.is_alive():
                return
            self._job_queue.put(None)  # graceful exit
            self._child = None
        child.join(timeout=5)
        if child.is_alive():
            child.terminate()

    def _start_idle_timer(self) -> None:
        self._cancel_idle_timer()
        timeout = self._config.model_idle_timeout_seconds
        if timeout <= 0:
            self._release_idle()
            return
        self._idle_timer = threading.Timer(timeout, self._release_idle)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    # -- listeners ------------------------------------------------------------

    def _notify(self, job_id: str) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(job_id)
            except Exception:  # noqa: BLE001 — a bad listener must not stall jobs
                pass

    def _notify_pending(self) -> None:
        with self._lock:
            pending_ids = [spec["job_id"] for spec in self._pending]
        for job_id in pending_ids:
            self._notify(job_id)
