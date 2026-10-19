"""FastAPI routes — the §8.1 API contract, nothing more.

This layer is a thin client of core/pipeline.py via the queue (FR-42): it
validates uploads, persists them, and translates queue/store state into JSON
and SSE. It never runs inference itself. Bound to 127.0.0.1 only (FR-30) by
the serve entry point.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import tempfile
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from importlib import metadata
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from whisper_notes import __version__
from whisper_notes.config import Config
from whisper_notes.core.audio import ALLOWED_EXTENSIONS
from whisper_notes.preflight import PreflightError, check_ffmpeg
from whisper_notes.store.db import Job, JobStore
from whisper_notes.store.files import allocate_upload_path, sanitize_filename
from whisper_notes.web.queue import TranscriptionQueue

STATIC_DIR = Path(__file__).parent / "static"
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})
_UPLOAD_CHUNK = 1 << 20
PREVIEW_DEFAULT_CHARS = 500


def _job_payload(job: Job, position: int | None) -> dict:
    """The GET /api/jobs/{id} shape: status, phase, percent, elapsed, eta,
    error (§8.1) plus the fields the history list needs (FR-36)."""
    elapsed = eta = None
    if job.started_at:
        start = datetime.fromisoformat(job.started_at)
        end = (
            datetime.fromisoformat(job.finished_at)
            if job.finished_at
            else datetime.now().astimezone()
        )
        elapsed = max(round((end - start).total_seconds(), 1), 0.0)
        if job.status == "running" and job.percent:
            eta = round(elapsed * (100 - job.percent) / job.percent, 1)  # FR-34
    return {
        "job_id": job.id,
        "status": job.status,
        "phase": job.phase,
        "percent": job.percent,
        "elapsed": elapsed,
        "eta": eta,
        "error": job.error,
        "queue_position": position,
        "source_name": job.source_name,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "language": job.language,
        "duration_seconds": job.duration_seconds,
        "word_count": job.word_count,
        "has_markdown": bool(job.markdown_path and Path(job.markdown_path).exists()),
    }


def create_app(config: Config, store: JobStore, queue: TranscriptionQueue) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        queue.shutdown()

    app = FastAPI(title="whisper-notes", version=__version__, lifespan=lifespan)

    def _get_job_or_404(job_id: str) -> Job:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job.")
        return job

    # -- UI -------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index():
        page = STATIC_DIR / "index.html"
        if page.exists():
            return FileResponse(page, media_type="text/html")
        return HTMLResponse("<h1>whisper-notes</h1><p>UI arrives in P4.</p>")

    # -- health & config ------------------------------------------------------

    @app.get("/api/health")
    async def health():
        try:
            check_ffmpeg()
            ffmpeg_ok = True
        except PreflightError:
            ffmpeg_ok = False
        return {
            "ok": ffmpeg_ok,
            "engine": f"mlx-whisper {metadata.version('mlx-whisper')}",
            "model": config.model_repo,
            "model_loaded": queue.model_loaded(),
            "ffmpeg_ok": ffmpeg_ok,
            "version": __version__,
        }

    @app.get("/api/config")
    async def effective_config():
        return {
            "port": config.port,
            "model": config.model_repo,
            "supports_translation": _engine().supports_translation,
            "model_idle_timeout_seconds": config.model_idle_timeout_seconds,
            "timestamps": config.timestamps,
            "summary_line": config.summary_line,
            "paragraph_pause_seconds": config.paragraph_pause_seconds,
            "paragraph_max_chars": config.paragraph_max_chars,
            "max_file_gb": config.max_file_gb,
            "max_duration_hours": config.max_duration_hours,
            "data_dir": str(config.data_dir),
            "allowed_extensions": sorted(ALLOWED_EXTENSIONS),
        }

    # -- model lifecycle (R2: the one-time download is explicit + consented) --

    model_state: dict = {"engine": None, "downloading": False, "error": None}
    model_lock = threading.Lock()

    def _engine():
        with model_lock:
            if model_state["engine"] is None:
                module_name, _, attr = queue.engine_factory.partition(":")
                engine_cls = getattr(importlib.import_module(module_name), attr)
                model_state["engine"] = engine_cls(config.model_repo)
            return model_state["engine"]

    @app.get("/api/model")
    async def model_status():
        engine = _engine()
        downloading = model_state["downloading"]
        return {
            "repo": config.model_repo,
            "cached": False if downloading else engine.is_cached(),
            "downloading": downloading,
            "received_bytes": engine.cache_progress_bytes() if downloading else None,
            "error": model_state["error"],
        }

    @app.post("/api/model/prefetch")
    async def model_prefetch():
        """Start the one-time model download. Only ever called from an
        explicit user action in the UI — never implicitly (R2, C3)."""
        engine = _engine()
        if engine.is_cached():
            return {"started": False, "cached": True}
        with model_lock:
            if model_state["downloading"]:
                return {"started": False, "cached": False}
            model_state["downloading"] = True
            model_state["error"] = None

        def download():
            try:
                engine.prefetch()
            except Exception as e:  # noqa: BLE001 — surfaced to the UI (FR-38)
                model_state["error"] = (
                    f"The model download failed ({e}). "
                    "Check your connection and try again."
                )
            finally:
                model_state["downloading"] = False

        threading.Thread(target=download, daemon=True, name="model-prefetch").start()
        return {"started": True, "cached": False}

    # -- jobs -----------------------------------------------------------------

    @app.post("/api/jobs")
    async def submit_job(
        file: UploadFile,
        language: str | None = Form(None),
        translate: bool = Form(False),
        force: bool = Form(False),
    ):
        source_name = sanitize_filename(file.filename or "recording")
        extension = Path(source_name).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                415,
                f"'{extension or source_name}' is not a supported format. "
                f"Supported: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
            )
        if language is not None:
            language = language.strip().lower() or None
        if language is not None and not (2 <= len(language) <= 3 and language.isalpha()):
            raise HTTPException(422, "Language must be an ISO code like 'en' or 'pt'.")
        if translate and not _engine().supports_translation:
            from whisper_notes.core.engine_mlx import TRANSLATION_UNSUPPORTED_MESSAGE

            raise HTTPException(
                422, TRANSLATION_UNSUPPORTED_MESSAGE.format(config.model_repo)
            )

        # Stream to a temp file, hashing (FR-05) and enforcing the size cap
        # (FR-03) as it arrives — never buffering the upload in memory.
        config.uploads_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        total = 0
        fd, tmp = tempfile.mkstemp(dir=config.uploads_dir, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := await file.read(_UPLOAD_CHUNK):
                    total += len(chunk)
                    if total > config.max_file_bytes:
                        raise HTTPException(
                            413,
                            f"File exceeds the {config.max_file_gb:g} GB limit.",
                        )
                    digest.update(chunk)
                    out.write(chunk)
            if total == 0:
                raise HTTPException(422, "The uploaded file is empty.")
            sha256 = digest.hexdigest()

            # FR-06: same content, model and settings already transcribed ->
            # offer the cached result instead of re-transcribing.
            if not force:
                duplicate = store.find_duplicate(
                    sha256=sha256,
                    model=config.model_repo,
                    translate=translate,
                    language_override=language,
                )
                if duplicate and duplicate.markdown_path and Path(duplicate.markdown_path).exists():
                    os.unlink(tmp)
                    return {
                        "job_id": duplicate.id,
                        "queue_position": None,
                        "duplicate": True,
                    }

            destination = allocate_upload_path(config.uploads_dir, source_name)
            os.replace(tmp, destination)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

        job, position = queue.submit(
            source=destination,
            source_name=source_name,
            sha256=sha256,
            language=language,
            translate=translate,
        )
        return {"job_id": job.id, "queue_position": position, "duplicate": False}

    @app.get("/api/jobs")
    async def list_jobs():
        return [_job_payload(job, queue.position(job.id)) for job in store.list()]

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str):
        return _job_payload(_get_job_or_404(job_id), queue.position(job_id))

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str):
        _get_job_or_404(job_id)
        loop = asyncio.get_running_loop()
        signals: asyncio.Queue = asyncio.Queue()

        def listener(changed_id: str) -> None:
            if changed_id == job_id:
                loop.call_soon_threadsafe(signals.put_nowait, True)

        async def stream():
            queue.add_listener(listener)
            try:
                yield "retry: 2000\n\n"
                last = None
                while True:
                    job = store.get(job_id)
                    if job is None:
                        break
                    payload = _job_payload(job, queue.position(job_id))
                    if payload != last:
                        last = payload
                        yield f"data: {json.dumps(payload)}\n\n"
                    if job.status in TERMINAL_STATUSES:
                        break
                    try:
                        await asyncio.wait_for(signals.get(), timeout=2.0)
                    except TimeoutError:
                        # periodic re-check keeps elapsed/ETA fresh and doubles
                        # as the SSE keepalive
                        yield ": keepalive\n\n"
            finally:
                queue.remove_listener(listener)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/jobs/{job_id}/markdown")
    async def download_markdown(job_id: str):
        job = _get_job_or_404(job_id)
        if job.status != "done" or not job.markdown_path:
            raise HTTPException(409, "This job has no transcript yet.")
        path = Path(job.markdown_path)
        if not path.exists():
            raise HTTPException(410, "The transcript file has been deleted.")
        return FileResponse(  # filename= sets Content-Disposition: attachment
            path, media_type="text/markdown", filename=path.name
        )

    @app.get("/api/jobs/{job_id}/preview")
    async def preview(job_id: str, n: int = PREVIEW_DEFAULT_CHARS):
        job = _get_job_or_404(job_id)
        if job.status != "done" or not job.markdown_path:
            raise HTTPException(409, "This job has no transcript yet.")
        path = Path(job.markdown_path)
        if not path.exists():
            raise HTTPException(410, "The transcript file has been deleted.")
        n = max(1, min(n, 10_000))
        text = path.read_text(encoding="utf-8")
        # preview the transcript body, not the frontmatter
        body_start = text.find("\n# ")
        body = text[body_start + 1 :] if body_start != -1 else text
        return {"preview": body[:n], "truncated": len(body) > n}

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(job_id: str):
        """Cancel if active (FR-18); delete artifacts (FR-07).

        Active job: cancelled, upload removed, the record kept as history.
        Finished job: record and all artifacts removed.
        """
        job = _get_job_or_404(job_id)
        cancelled = False
        if job.status in ("queued", "running"):
            cancelled = queue.cancel(job_id)
        for file_path in (job.upload_path, job.markdown_path):
            if file_path and Path(file_path).exists():
                os.unlink(file_path)
        if not cancelled:
            store.delete(job_id)
        return {"ok": True, "cancelled": cancelled}

    return app
