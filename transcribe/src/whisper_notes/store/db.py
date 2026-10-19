"""Job state in SQLite via stdlib sqlite3 (D-08). No ORM.

One connection per operation with WAL journaling: callers are the web thread,
the queue manager thread, and timers — low contention, maximum simplicity.
"""

from __future__ import annotations

import contextlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    source_name         TEXT NOT NULL,
    upload_path         TEXT,
    sha256              TEXT,
    status              TEXT NOT NULL CHECK (status IN
                            ('queued','running','done','failed','cancelled')),
    phase               TEXT,
    percent             REAL,
    error               TEXT,
    language            TEXT,
    duration_seconds    REAL,
    processing_seconds  INTEGER,
    word_count          INTEGER,
    markdown_path       TEXT,
    model               TEXT NOT NULL,
    translate           INTEGER NOT NULL DEFAULT 0,
    language_override   TEXT,
    started_at          TEXT,
    finished_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_jobs_sha256 ON jobs (sha256);
"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Job:
    id: str
    created_at: str
    source_name: str
    upload_path: str | None
    sha256: str | None
    status: str
    phase: str | None
    percent: float | None
    error: str | None
    language: str | None
    duration_seconds: float | None
    processing_seconds: int | None
    word_count: int | None
    markdown_path: str | None
    model: str
    translate: bool
    language_override: str | None
    started_at: str | None
    finished_at: str | None


def _job(row: sqlite3.Row) -> Job:
    values = dict(row)
    values["translate"] = bool(values["translate"])
    return Job(**values)


class JobStore:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            with conn:  # transaction
                yield conn
        finally:
            conn.close()

    # -- lifecycle ----------------------------------------------------------

    def create(
        self,
        *,
        source_name: str,
        upload_path: str | None,
        sha256: str | None,
        model: str,
        translate: bool = False,
        language_override: str | None = None,
    ) -> Job:
        job_id = uuid.uuid4().hex
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO jobs (id, created_at, source_name, upload_path, "
                "sha256, status, model, translate, language_override) "
                "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
                (job_id, _now(), source_name, upload_path, sha256,
                 model, int(translate), language_override),
            )
        return self.get(job_id)  # type: ignore[return-value]

    def get(self, job_id: str) -> Job | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job(row) if row else None

    def list(self, limit: int = 200) -> list[Job]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_job(r) for r in rows]

    def delete(self, job_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    # -- transitions ---------------------------------------------------------

    def mark_running(self, job_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status='running', started_at=?, phase=NULL, "
                "percent=NULL WHERE id=?",
                (_now(), job_id),
            )

    def update_progress(self, job_id: str, phase: str, percent: float | None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET phase=?, percent=? WHERE id=? AND status='running'",
                (phase, percent, job_id),
            )

    def mark_done(
        self,
        job_id: str,
        *,
        language: str,
        duration_seconds: float,
        processing_seconds: int,
        word_count: int,
        markdown_path: str,
        sha256: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status='done', phase='done', percent=100, "
                "language=?, duration_seconds=?, processing_seconds=?, "
                "word_count=?, markdown_path=?, sha256=?, finished_at=? WHERE id=?",
                (language, duration_seconds, processing_seconds, word_count,
                 markdown_path, sha256, _now(), job_id),
            )

    def mark_failed(self, job_id: str, error: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                (error, _now(), job_id),
            )

    def mark_cancelled(self, job_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status='cancelled', finished_at=? WHERE id=?",
                (_now(), job_id),
            )

    # -- recovery & dedup ----------------------------------------------------

    def fail_interrupted(self) -> int:
        """NFR-05: jobs left 'running'/'queued' by a crash become 'failed'."""
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status='failed', finished_at=?, "
                "error='Interrupted by a server restart. Submit the file again.' "
                "WHERE status IN ('running', 'queued')",
                (_now(),),
            )
        return cursor.rowcount

    def find_duplicate(
        self,
        *,
        sha256: str,
        model: str,
        translate: bool,
        language_override: str | None,
    ) -> Job | None:
        """FR-06: a completed job for the same content and settings."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status='done' AND sha256=? AND model=? "
                "AND translate=? AND COALESCE(language_override,'')=? "
                "ORDER BY finished_at DESC LIMIT 1",
                (sha256, model, int(translate), language_override or ""),
            ).fetchone()
        return _job(row) if row else None
