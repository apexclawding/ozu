"""Vault clone management: rebase, commit, push — behind the remote guard.

The bridge owns a dedicated clone under data/ so it never disturbs the
human working clone. Discipline mirrors Ozu's sync_from_origin: rebase,
abort on conflict, defer — never force, never push over a failed rebase.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .config import Config

log = logging.getLogger("ozu_capture.vault")


class VaultError(Exception):
    """Hard error — includes FR-33's remote-guard violation."""


class SyncDeferred(Exception):
    """pull --rebase failed; nothing written, retry next tick (FR-30)."""


class PushDeferred(Exception):
    """push failed; the commit is safe locally, retry next tick (FR-32)."""


# Belt and braces, not a fix for anything observed: `git fetch` ends by
# spawning `git maintenance run --auto --detach`, and a detached child
# inherits the pipes `subprocess.run(capture_output=True)` reads until every
# writer closes them. That would block us on work already finished. Cheap to
# rule out, so we do — but see NETWORK_TIMEOUT for what actually bit.
_NO_AUTO_MAINTENANCE = ["-c", "gc.auto=0", "-c", "maintenance.auto=false"]

# The NAS's path to github.com:22 stalls, intermittently and for minutes.
# Measured 2026-09-02 from inside the container: `git ls-remote` took 1.4 s,
# then five consecutive attempts 45 s apart all timed out, then it was 1.4 s
# again — with DNS, TCP and everything else on the box healthy throughout.
# Whether that is GitHub throttling a key that had just pushed six times or
# the host's own networking, the bridge cannot tell and must not care: a git
# that is slow is not a git that failed. Two minutes was short enough to
# abort ticks that were about to succeed; five is long enough to ride a stall
# out, and short enough that a genuinely dead remote still frees the tick.
# Nothing is lost either way — a deferred push is retried next tick (FR-32),
# which is how all six of the first day's transcripts reached GitHub despite
# the log showing timeouts.
NETWORK_TIMEOUT = 300
CLONE_TIMEOUT = 900


def _git(args: list[str], cwd: Path, timeout: int = NETWORK_TIMEOUT
         ) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *_NO_AUTO_MAINTENANCE, *args], cwd=cwd,
                          capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def origin_url(clone: Path) -> str:
    return _git(["remote", "get-url", "origin"], clone).stdout.strip()


def _guard(cfg: Config, clone: Path) -> None:
    url = origin_url(clone)
    if url != cfg.approved_remote:
        raise VaultError(
            f"origin is {url!r}, not the approved remote "
            f"{cfg.approved_remote!r} — refusing to touch it (FR-33)")


def ensure_clone(cfg: Config) -> Path:
    clone = cfg.vault_clone
    if not (clone / ".git").exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        log.info("cloning vault from %s", cfg.approved_remote)
        try:
            p = _git(["clone", "--quiet", cfg.approved_remote, str(clone)],
                     cwd=clone.parent, timeout=CLONE_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise VaultError(f"clone stalled (>{CLONE_TIMEOUT}s); retrying next tick")
        if p.returncode != 0:
            raise VaultError(f"clone failed: {p.stderr.strip()[:300]}")
    _guard(cfg, clone)
    return clone


def reset_if_dirty(clone: Path) -> None:
    """A dirty tree here is our own crash artifact from an interrupted
    write→commit window; the audio is still in the inbox and the ledger is
    below `committed`, so the note will simply be rebuilt. Only this
    dedicated clone is ever reset — never a human working copy."""
    if _git(["status", "--porcelain"], clone).stdout.strip():
        log.warning("vault clone dirty (crash artifact); resetting")
        _git(["reset", "--hard", "HEAD"], clone)
        _git(["clean", "-fd"], clone)


def sync(cfg: Config, clone: Path) -> None:
    try:
        p = _git(["pull", "--rebase", "--quiet", "origin", cfg.branch], clone)
    except subprocess.TimeoutExpired:
        # A stalled remote is a deferral, not a crash: nothing has been
        # written, and the next tick tries again. Abort any half-started
        # rebase so the clone is clean for it — but never let that tidy-up
        # decide the outcome; it can stall too, and `reset_if_dirty` catches
        # whatever it leaves behind.
        try:
            _git(["rebase", "--abort"], clone, timeout=30)
        except subprocess.TimeoutExpired:
            log.warning("rebase --abort also stalled; next tick resets the clone")
        raise SyncDeferred(f"remote stalled (>{NETWORK_TIMEOUT}s)")
    if p.returncode != 0:
        _git(["rebase", "--abort"], clone)
        raise SyncDeferred(p.stderr.strip()[:300])


def commit(cfg: Config, clone: Path, rel_paths: list[str], msg: str) -> None:
    _git(["add", "--", *rel_paths], clone)
    p = _git(["commit", "--quiet", "-m", msg], clone)
    if p.returncode != 0:
        raise VaultError(f"commit failed: {(p.stderr or p.stdout).strip()[:300]}")


def push(cfg: Config, clone: Path) -> None:
    _guard(cfg, clone)
    try:
        p = _git(["push", "--quiet", "origin", cfg.branch], clone)
    except subprocess.TimeoutExpired:
        # The commit is already local and the ledger says `committed`, so the
        # retry is free — and a push that stalled may even have landed, which
        # the next tick's rebase reconciles.
        raise PushDeferred(f"remote stalled (>{NETWORK_TIMEOUT}s)")
    if p.returncode != 0:
        raise PushDeferred(p.stderr.strip()[:300])
