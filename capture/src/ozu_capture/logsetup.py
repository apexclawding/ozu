"""Rotating file log plus stderr (FR-40). Quiet ticks log nothing."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


def setup(log_path: Path) -> None:
    root = logging.getLogger("ozu-capture")
    if root.handlers:          # idempotent across tests / repeated calls
        return
    root.setLevel(logging.INFO)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=1_000_000, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
