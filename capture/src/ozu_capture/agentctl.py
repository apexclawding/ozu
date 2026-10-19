"""launchd LaunchAgent install/uninstall (FR-42, C1/C2).

A short-lived `run-once` per StartInterval tick: launchd itself guarantees
one live instance per label, the flock is the belt-and-braces, and a crash
costs one tick, not a daemon. Installed per-login (gui domain), no root.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import Config

AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
# launchd's xpcproxy opens StandardOut/ErrorPath before exec, with none of
# the Full Disk Access granted to the venv's python. Under ~/Documents that
# open is TCC-denied, posix_spawn fails, and launchctl reports only
# "78: EX_CONFIG" — every tick, silently, with nothing written to any log.
LOG_DIR = Path.home() / "Library" / "Logs" / "ozu-capture"


def render(cfg: Config) -> dict:
    bin_path = Path(sys.executable).parent / "ozu-capture"
    return {
        "Label": cfg.agent_label,
        "ProgramArguments": [str(bin_path), "run-once"],
        "StartInterval": cfg.start_interval,
        "RunAtLoad": True,
        "StandardOutPath": str(LOG_DIR / "launchd.out"),
        "StandardErrorPath": str(LOG_DIR / "launchd.err"),
    }


def plist_path(cfg: Config) -> Path:
    return AGENTS_DIR / f"{cfg.agent_label}.plist"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(cfg: Config) -> int:
    bin_path = Path(sys.executable).parent / "ozu-capture"
    if not bin_path.exists():
        print(f"console script missing: {bin_path} — run `uv sync` first",
              file=sys.stderr)
        return 2
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    p = plist_path(cfg)
    with p.open("wb") as f:
        plistlib.dump(render(cfg), f)
    uid = os.getuid()
    _launchctl("bootout", f"gui/{uid}/{cfg.agent_label}")     # idempotent
    r = _launchctl("bootstrap", f"gui/{uid}", str(p))
    if r.returncode != 0:                                     # older launchctl
        r = _launchctl("load", "-w", str(p))
    if r.returncode != 0:
        print(f"launchctl failed: {r.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"loaded {cfg.agent_label}: every {cfg.start_interval}s "
          f"while logged in ({p})")
    return 0


def uninstall(cfg: Config) -> int:
    _launchctl("bootout", f"gui/{os.getuid()}/{cfg.agent_label}")
    p = plist_path(cfg)
    if p.exists():
        p.unlink()
    print(f"unloaded and removed {cfg.agent_label}")
    return 0
