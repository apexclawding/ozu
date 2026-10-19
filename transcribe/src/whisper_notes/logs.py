"""Structured logging to ./data/whisper.log with rotation (NFR-09)."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging(data_dir: Path, *, level: int = logging.INFO) -> None:
    """Attach a rotating file handler for the whisper_notes namespace.

    Idempotent: calling twice (CLI + worker child both boot through entry
    points) does not duplicate handlers.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("whisper_notes")
    if any(
        isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers
    ):
        return
    handler = logging.handlers.RotatingFileHandler(
        data_dir / "whisper.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"whisper_notes.{name}")
