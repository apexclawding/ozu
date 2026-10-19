"""@claude work runs: headless Claude Code building a flagged idea in its repo.

A handwritten @claude beside an idea arrives here as frontmatter
`claude: requested` (set by vault.write_notes — personal vault only, so a
walled page can never trigger a run). For each requested note whose topic has
a repo in OZU_CLAUDE_REPOS, this module clones that repo fresh under
~/ozu-data/agent/, lets Claude Code work the idea on a `claude/<note-id>` branch,
pushes that branch — never the default branch — and writes a companion note
into the vault linking the diff for review.

The ledger is the Markdown: the source note's `claude:` status and the
companion note both survive `ozu index`, so a rebuilt db cannot re-run a
note. Notes on unmapped topics stay `requested` (they run once a repo is
mapped); failures become `claude: error` and surface in the digest.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

from .config import brand, canonical_topic, get_config
from .indexer import _parse_frontmatter
from . import db, vault

log = logging.getLogger("ozu.agent")


class AuthError(RuntimeError):
    """The CLI's claude.ai session is broken — no run can succeed until a
    human re-authenticates, so callers leave notes queued instead of
    burning their one-run ledger on it."""


class UsageLimitError(AuthError):
    """The claude.ai login's usage window is spent.

    It subclasses AuthError because every existing caller's answer to that —
    stop the phase, leave the work queued, mark nothing as failed — is
    already the right one. Only builds.py needs to tell the two apart, and
    only because it is relaunched by a five-minute timer: a broken login
    waits for a human, a spent window waits for a clock.

    `resets_at` is the moment the CLI named (None when it named none), and
    `spent` says whether this run consumed a window or merely bounced off
    one that was already spent."""

    def __init__(self, message: str, *, resets_at: "dt.datetime | None" = None,
                 spent: bool = True):
        super().__init__(message)
        self.resets_at = resets_at
        self.spent = spent


# The CLI reports a spent usage window the same way it reports a broken
# login: an api_error whose story is in the message — "You've hit your
# session limit · resets 6:30am (UTC)". Matched on that message for the same
# reason understand._unavailable matches the credit-balance 400 on its
# message (invariant 10b): the transport classes it with failures that mean
# something else entirely. Anchored to the refusal's own shapes rather than
# to the words "rate limit", because the result of a *successful* run is a
# report, and a report about rate limiting must never read as one.
_LIMIT_RE = re.compile(
    r"^\s*(?:you'?ve? (?:hit|reached) your [\w -]{0,20}limit"
    r"|claude (?:ai )?usage limit reached"
    r"|[\w-]{1,12} limit reached)\b", re.I)
_RESETS_RE = re.compile(r"resets\s+(\d{1,2}):(\d{2})\s*(am|pm)?\s*(\(UTC\))?", re.I)

# A run that bounces off an already-spent window costs nothing and must not
# count as an attempt. Measured on the 2026-09-20 loop: the 113 refusals ran
# 2-3 seconds and reported no turns; the three runs that really burned a
# window ran 20-40 minutes over hundreds of turns. Anything between the two
# counts as spent — over-counting costs one line of paper, under-counting is
# what let that night burn three windows.
FREE_REFUSAL_MS = 60_000


def _resets_at(message: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """The moment a limit message names, as an aware UTC datetime.

    The CLI prints a wall-clock time and no date ("resets 6:30am (UTC)"), so
    the next occurrence of that time is the answer; "(UTC)" decides whether
    it is read as UTC or as this box's local time. None when there is no
    time to read — the caller then backs off by a fixed step rather than
    inventing one."""
    m = _RESETS_RE.search(message or "")
    if not m:
        return None
    hour, minute, ampm, utc = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
    if ampm:
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(
        dt.timezone.utc if utc else None)
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= now:
        when += dt.timedelta(days=1)
    return when.astimezone(dt.timezone.utc)


PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "work_prompt.md"

# Claude Code confines Bash to the working directory on its own, but it does
# not confine the file tools: with a bare `Read` in the allowlist a run opens
# ~/ozu-data/.env, and with a bare `Write` it creates files anywhere the `ubuntu`
# user can reach. Both were verified against the live box on 2026-08-25 — the
# earlier "edits + git-only Bash" containment was Bash-only in practice.
# Path-scoped rules do hold, so the file tools are pinned to the run's own
# working directory, which is why the allowlist is built per run rather than
# being a constant. In these rules `Read(...)` also governs Glob and Grep, and
# `Edit(...)` also governs Write.
def scoped(tools: tuple[str, ...], workdir: Path) -> list[str]:
    # `//<abs path>` is the absolute-path form. Normalise but do not resolve():
    # the rule has to name the path Claude Code is actually given, and resolving
    # would swap in a symlink's target (on the Mac, /home/... via a firmlink).
    root = os.path.abspath(workdir).lstrip("/")
    return [f"{t}(//{root}/**)" for t in tools]


# A work run reads and edits its clone, searches the web, runs git, and runs
# the tests it writes. It installs nothing and controls no services — this box
# may also run other services — so Bash stays an enumeration of prefixes
# rather than a general shell. `<prefix>:*` is the matching syntax; the older
# `Bash(git *)` matched nothing, and git only worked because Claude Code
# auto-approves read-only commands and in-workdir edits.
# A caveat worth knowing before trusting a "tests pass" report: a run works in
# a *fresh clone*, and it cannot install anything. So a runner is only useful
# where its dependencies already exist — on this box that means stdlib
# unittest always, `make test` when the Makefile needs nothing, and pytest/npm
# only where the repo vendors its deps. There is no system pytest and a fresh
# clone has no node_modules. Installing them is deliberately not offered:
# `npm install` runs arbitrary postinstall code as `ubuntu`, which is the one
# thing this allowlist exists to prevent.
WORK_BASH = ("git",
             "python -m unittest", "python3 -m unittest",
             "pytest", "python -m pytest", "python3 -m pytest",
             ".venv/bin/pytest", "npm test", "npm run test", "npx vitest",
             "yarn test", "make test", "go test", "cargo test")

# Deny beats allow, whatever the allowlist says. The pipeline owns pushing and
# pushes exactly one branch; `git:*` by itself would also permit the agent to
# run `git push origin HEAD:main`, which is the one thing it must never do.
DENIED_TOOLS = "Bash(git push:*)"

# A run that only produces text (learn, research, a Build proposal, the wiki)
# must not be able to write at all. `acceptEdits` approves edits *and*
# filesystem Bash inside the working directory whatever --allowedTools says —
# measured 2026-10-01: a run allowed only a scoped `Read` wrote a file, edited
# one and ran `touch`, and learning runs had already written `<id>-brief.md`
# into the vault, which the post-run commit then pushed. `dontAsk` refuses
# whatever is not allowlisted, and the deny list says so outright (deny beats
# allow); the same probe then created nothing and still read. Read-only is
# the default: a run may write only when its caller asks for it by name.
READ_ONLY_DENIED = DENIED_TOOLS + ",Bash,Edit,Write,MultiEdit,NotebookEdit"


def permission_flags(writes: bool) -> list[str]:
    if writes:
        return ["--permission-mode", "acceptEdits", "--disallowedTools", DENIED_TOOLS]
    return ["--permission-mode", "dontAsk", "--disallowedTools", READ_ONLY_DENIED]


def work_tools(workdir: Path) -> str:
    """Allowlist for a build run, with the file tools pinned to the clone."""
    return ",".join([*scoped(("Read", "Edit"), workdir), "WebSearch", "WebFetch",
                     *(f"Bash({p}:*)" for p in WORK_BASH)])


def _git(args: list[str], cwd: Path | None) -> str:
    proc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def _head(workdir: Path) -> str | None:
    """Current commit, or None on an unborn branch (freshly-founded repo)."""
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workdir,
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _github_repo(url: str) -> str | None:
    m = re.search(r"github[^:/]*[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


RUN_ENV_KEYS = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
                "TZ", "TERM", "TMPDIR", "XDG_RUNTIME_DIR", "CLAUDE_CODE_OAUTH_TOKEN")


def _run_claude(prompt: str, workdir: Path, allowed_tools: str | None = None,
                model: str | None = None, timeout: int | None = None,
                writes: bool = False) -> str:
    """One headless Claude Code run; returns its final report text.

    `model` is a full CLI model id (`claude-fable-5-1`), not a tag — the tag →
    id mapping lives in config.build_models and is resolved by the caller, so
    this function never guesses. No model means the CLI's own default, which
    is what every pre-build caller has always used. `timeout` exists because
    the models differ in how long a real task takes (a Fable run can be
    long); default stays OZU_CLAUDE_TIMEOUT."""
    cfg = get_config()
    # The run gets a shell's basics and the claude.ai login, nothing of
    # ~/ozu-data/.env: a work run executes tests it may have written, a
    # research run reads the web, and either could print os.environ into a
    # file the pipeline then pushes (GitHub token, SMTP password). Without
    # ANTHROPIC_API_KEY the CLI bills the claude.ai login, not the API.
    env = {k: os.environ[k] for k in RUN_ENV_KEYS if k in os.environ}
    # --strict-mcp-config with no --mcp-config: the run loads no MCP server.
    # Every server registered for the user would otherwise come along, and
    # Ozu's own server carries `capture`, a vault write. The allowlist already
    # refuses them; this keeps them out of the toolbox of a run that reads
    # third-party text at all.
    cmd = [cfg.claude_bin, "-p", prompt, *permission_flags(writes),
           "--allowedTools", allowed_tools or work_tools(workdir),
           "--strict-mcp-config",
           "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(
        cmd, cwd=workdir, env=env, capture_output=True, text=True,
        timeout=timeout or cfg.claude_timeout,
    )
    # On failure the CLI puts the story in its stdout JSON, not stderr.
    detail = proc.stderr.strip()[-300:]
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out = None
        if proc.returncode == 0:
            return proc.stdout.strip()
    if out is not None:
        msg = str(out.get("result") or "")
        api_error = (out.get("terminal_reason") == "api_error"
                     or "authenticate" in msg.lower()
                     or proc.returncode != 0 or bool(out.get("is_error")))
        if api_error and _LIMIT_RE.search(msg):
            # The CLI's own numbers are the only honest evidence of whether
            # this run consumed the window, and this is the one place that
            # sees them, so the verdict travels on the exception.
            spent = (int(out.get("num_turns") or 0) > 1
                     or int(out.get("duration_ms") or 0) >= FREE_REFUSAL_MS)
            raise UsageLimitError(msg[:300], resets_at=_resets_at(msg),
                                  spent=spent)
        if "authenticate" in msg.lower() or out.get("terminal_reason") == "api_error":
            raise AuthError(msg[:300] or "api_error")
        detail = msg[-300:] or detail
    if proc.returncode != 0 or (out or {}).get("is_error"):
        raise RuntimeError(f"claude exited {proc.returncode}: {detail}")
    return (out or {}).get("result") or ""


def _open_pr(repo: str, base: str, branch: str, note, report: str) -> str | None:
    """Open a PR for a pushed work branch. Returns its URL, or None when no
    token is configured. Deploy keys cannot call the REST API, so this needs
    OZU_GITHUB_TOKEN — a fine-grained PAT scoped to the mapped repos."""
    cfg = get_config()
    if not cfg.github_token:
        return None
    body = (f"{report.strip()}\n\n---\n"
            f"Opened by {brand()} from ink note `{note['id']}` "
            f"(topic: {note['topic']}). The branch was built by an unattended "
            f"@claude work run — review before merging; nothing merges itself.")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls",
        data=json.dumps({"title": note["title"], "head": branch, "base": base,
                         "body": body[:60000]}).encode(),
        headers={"Authorization": f"Bearer {cfg.github_token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "ozu"},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["html_url"]


def _note_body(path: Path) -> str:
    _, body = _parse_frontmatter(path.read_text())
    return body.strip()


def _work_note(note) -> None:
    cfg = get_config()
    note_id, url = note["id"], cfg.claude_repos[canonical_topic(note["topic"])]
    branch = f"claude/{note_id}"
    workdir = cfg.agent_dir / note_id
    if workdir.exists():
        shutil.rmtree(workdir)  # leftovers from a failed attempt; start clean
    workdir.parent.mkdir(parents=True, exist_ok=True)

    _git(["clone", "--quiet", url, str(workdir)], cwd=None)
    base = _git(["symbolic-ref", "--short", "HEAD"], cwd=workdir)
    before = _head(workdir)
    _git(["checkout", "-q", "-b", branch], cwd=workdir)

    prompt = PROMPT_PATH.read_text().format(
        repo=url, branch=branch, note_id=note_id, topic=note["topic"],
        title=note["title"], note_body=_note_body(Path(note["path"])),
    )
    report = _run_claude(prompt, workdir, writes=True)

    pushed = _head(workdir) not in (None, before)
    if pushed:
        _git(["push", "--quiet", "-u", "origin", branch], cwd=workdir)
    gh = _github_repo(url)
    review_url = None
    if pushed and gh:
        try:
            review_url = _open_pr(gh, base, branch, note, report)
        except Exception as e:  # branch is already up; a failed PR ≠ failed run
            log.warning("PR creation failed for %s, falling back to compare: %s",
                        note["id"], e)
        review_url = review_url or f"https://github.com/{gh}/compare/{base}...{branch}"
    vault.write_agent_note(source=dict(note), report=report,
                           branch=branch if pushed else None,
                           compare_url=review_url)
    log.info("claude run %s: pushed=%s", note_id, pushed)


def work_pending() -> int:
    """Run queued @claude notes, oldest first, bounded by OZU_MAX_CLAUDE_RUNS."""
    cfg = get_config()
    con = db.connect(cfg.db_path)
    rows = con.execute(
        "SELECT * FROM notes WHERE claude='requested' AND vault='personal'"
        " ORDER BY created").fetchall()
    ran = 0
    for note in rows:
        if ran >= cfg.max_claude_runs:
            log.info("claude run cap (%d) reached; %s waits for the next run",
                     cfg.max_claude_runs, note["id"])
            break
        if canonical_topic(note["topic"]) not in cfg.claude_repos:
            log.info("@claude on %s waits: no repo mapped for topic %r",
                     note["id"], note["topic"])
            continue
        ran += 1
        status = "done"
        try:
            _work_note(note)
        except AuthError as e:
            log.error("claude auth is broken (%s); leaving %s and the rest "
                      "queued — re-authenticate on the VPS", e, note["id"])
            break
        except Exception:
            log.exception("claude run for %s failed", note["id"])
            status = "error"
        vault.set_directive_status(Path(note["path"]), status)
        con.execute("UPDATE notes SET claude=? WHERE id=?", (status, note["id"]))
        con.commit()
        vault._git_commit(cfg.vault_dir, f"claude run: {note['id']} ({status})")
    if ran:
        log.info("claude work runs this cycle: %d", ran)
    return ran
