"""The weekly project board: `MASTER.md`, rewritten from what actually happened.

Once a week (when the board is BOARD_DAYS old) the cycle rewrites
`MASTER.md` at the root of the personal vault: what is being built, what
went quiet, what is next, citing notes and Builds by id. The run reads the
vault — last week's board first, so each edition continues the one before —
and is handed, inline, what the vault cannot show it: a development brief,
the open actions and the Builds.

The development brief is facts, built with no model call:
- `OZU_BOARD_REPOS` names the repositories that count, and where their plans
  live (`name=path`, or `name=path#ROADMAP.md|docs/plan.md`; plan files
  default to ROADMAP.md, PLAN.md and TODO.md, whichever exist). For each:
  the week's commits, whether a plan file moved, and the plan's open items.
- or `OZU_BOARD_BRIEF_CMD`, a command (run without a shell, like the grant
  hook) whose standard output is the brief, for a deployment with a brief of
  its own.
List only repositories whose work belongs in the personal vault: the board
is a personal note, printed on the Sunday sheet, and the wall (invariant 2)
is not crossed by naming a work repository here.

The run is confined like the wiki run (invariant 11f): `Read` pinned to the
vault and nothing else, and read-only. It returns the board as JSON; the
pipeline writes it under the board's id (kept from the existing file),
keeps `created`, and links only ids that exist. The Sunday wiki sheet prints
the new edition's one-line summary, and ink on that line comes back as a
note the next edition reads.

Off unless OZU_BOARD=1.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import shlex
import subprocess
from pathlib import Path

from . import agent, db, indexer, okf, vault, wiki
from .config import get_config
from .indexer import _parse_frontmatter

log = logging.getLogger("ozu.board")

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "board_prompt.md"
DEFAULT_ID = "master-project-board"
TITLE = "Master project board"
SOURCE = "board:weekly"
BOARD_DAYS = 7
TRIED_META = "board_tried_on"
MAX_NOTES = 80
DEFAULT_PLANS = ("ROADMAP.md", "PLAN.md", "TODO.md")
MAX_COMMITS = 15          # per repository, newest first
MAX_PLAN_LINES = 40       # per repository, open items across its plan files
MAX_BRIEF = 24_000        # characters: a brief command's output is cut here
GIT_TIMEOUT = 20
BRIEF_TIMEOUT = 120
_OPEN_ITEM = re.compile(r"^\s*[-*] \[ \] ")
_STATUS = re.compile(r"^\*\*Status:\*\*", re.IGNORECASE)


def enabled() -> bool:
    return get_config().board == "1"


def board_path() -> Path:
    return get_config().vault_dir / "MASTER.md"


def _fm() -> dict:
    p = board_path()
    return _parse_frontmatter(p.read_text())[0] if p.exists() else {}


def board_id() -> str:
    """The id the board already has (a vault that kept its own), else the
    default. The file sits at the vault root, so its name need not be it."""
    return str(_fm().get("id") or DEFAULT_ID)


def last_edition() -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(_fm().get("updated") or "")[:10])
    except ValueError:
        return None


def due(today: dt.date) -> bool:
    last = last_edition()
    return last is None or (today - last).days >= BOARD_DAYS


# ---------- the development brief ----------

def parse_repos(spec: str) -> list[tuple[str, Path, tuple[str, ...]]]:
    """`name=path[#plan|plan],…` → (name, path, plan files). Entries without
    a name or a path are skipped; `~` is expanded."""
    out = []
    for entry in spec.split(","):
        name, _, rest = entry.partition("=")
        path, _, plans = rest.partition("#")
        name, path = name.strip(), path.strip()
        if not name or not path:
            continue
        files = tuple(p.strip() for p in plans.split("|") if p.strip()) or DEFAULT_PLANS
        out.append((name, Path(os.path.expanduser(path)), files))
    return out


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       timeout=GIT_TIMEOUT, check=False, stdin=subprocess.DEVNULL)
    if r.returncode:
        raise RuntimeError(f"git {args[0]} failed")
    return r.stdout


def _plan_items(text: str) -> list[str]:
    """What a plan file says is still open: unchecked boxes, and each `##`
    item with the status line under it (the shape of ROADMAP.md)."""
    out, heading = [], ""
    for line in text.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
        elif _STATUS.match(line.strip()) and heading:
            out.append(f"{heading} — {line.strip()}")
            heading = ""
        elif _OPEN_ITEM.match(line):
            out.append(line.strip())
    return out


def repo_brief(name: str, repo: Path, plans: tuple[str, ...], since: dt.date) -> str:
    """One repository's week, as facts. Never raises: a repository that cannot
    be read says so in one line."""
    try:
        if not (repo / ".git").exists():
            return f"### {name}\n(not a git repository at the configured path)"
        stamp = since.isoformat()
        commits = _git(repo, "log", f"--since={stamp}", "--no-merges",
                       "--format=%cs %h %s").splitlines()
        lines = [f"### {name}"]
        if commits:
            lines.append(f"{len(commits)} commit(s) since {stamp}:")
            lines += [f"- {c}" for c in commits[:MAX_COMMITS]]
            if len(commits) > MAX_COMMITS:
                lines.append(f"- … and {len(commits) - MAX_COMMITS} more")
        else:
            last = _git(repo, "log", "-1", "--format=%cs").strip()
            lines.append(f"no commits since {stamp}" + (f" (last {last})" if last else ""))
        items: list[str] = []
        for plan in plans:
            path = repo / plan
            if not path.is_file() or not path.resolve().is_relative_to(repo.resolve()):
                continue
            moved = _git(repo, "log", f"--since={stamp}", "--format=%h", "--", plan).split()
            lines.append(f"plan `{plan}`: " + (f"changed in {len(moved)} commit(s) this week"
                                                if moved else "unchanged this week"))
            items += [f"- {plan}: {i}" for i in _plan_items(path.read_text(errors="replace"))]
        if items:
            lines.append("open in the plans:")
            lines += items[:MAX_PLAN_LINES]
            if len(items) > MAX_PLAN_LINES:
                lines.append(f"- … and {len(items) - MAX_PLAN_LINES} more")
        return "\n".join(lines)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError):
        log.warning("board: could not read repository %s", name)
        return f"### {name}\n(could not be read this week)"


def _command_brief(cmd: str) -> str:
    argv = shlex.split(cmd)
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TZ",
                                      "OZU_HOME", "OZU_VAULT_DIR") if k in os.environ}
    r = subprocess.run(argv, capture_output=True, text=True, timeout=BRIEF_TIMEOUT,
                       check=False, stdin=subprocess.DEVNULL, env=env)
    if r.returncode or not r.stdout.strip():
        raise RuntimeError(f"OZU_BOARD_BRIEF_CMD exited {r.returncode}")
    return r.stdout.strip()[:MAX_BRIEF]


def development_brief(since: dt.date) -> str:
    cfg = get_config()
    try:
        if cfg.board_brief_cmd:
            return _command_brief(cfg.board_brief_cmd)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        log.warning("board: OZU_BOARD_BRIEF_CMD failed; going without a brief",
                    exc_info=True)
        return "(the development brief could not be built this week)"
    repos = parse_repos(cfg.board_repos)
    if not repos:
        return "(no repositories configured: OZU_BOARD_REPOS is empty)"
    return "\n\n".join(repo_brief(n, p, plans, since) for n, p, plans in repos)


# ---------- one edition ----------

def _inputs(con, since: dt.date, bid: str) -> dict:
    actions = [
        f"- {r['id']} — {dt.date.fromtimestamp(float(r['created'])).isoformat()}"
        f" — {r['text']} — [[{r['note_id']}]]"
        for r in con.execute("SELECT * FROM actions WHERE done=0 ORDER BY created, id")]
    builds = [
        f"- {r['id']} — {r['status']} — {r['topic']} — {r['title']} — {r['updated']}"
        for r in con.execute("SELECT * FROM builds ORDER BY updated DESC, id DESC")]
    notes = []
    for r in con.execute(
            "SELECT id, title, path FROM notes WHERE vault='personal' AND created >= ?"
            " AND id != ? ORDER BY created DESC, id", (since.isoformat(), bid)):
        fm, _ = _parse_frontmatter(Path(r["path"]).read_text()) \
            if Path(r["path"]).exists() else ({}, "")
        notes.append(f"- {r['id']} — {okf.type_for(str(fm.get('source', '')))} — {r['title']}")
    more = max(0, len(notes) - MAX_NOTES)
    notes = notes[:MAX_NOTES] + ([f"- … and {more} more (see log.md)"] if more else [])
    return {"brief": development_brief(since),
            "actions": "\n".join(actions) or "(none open)",
            "builds": "\n".join(builds) or "(none)", "notes": "\n".join(notes) or "(none)"}


def run(today: dt.date | None = None, *, force: bool = False) -> bool:
    """Rewrite the board if it is due (or `force`). True when written."""
    cfg = get_config()
    today = today or dt.date.today()
    if not force and not due(today):
        return False
    con = db.connect(cfg.db_path)
    # One attempt a day: a run that fails (a bad reply, a timeout) must not
    # spend a slice of the usage window every two hours until someone looks.
    if not force and db.get_meta(con, TRIED_META) == today.isoformat():
        return False
    db.set_meta(con, TRIED_META, today.isoformat())
    bid = board_id()
    last = last_edition() or (today - dt.timedelta(days=BOARD_DAYS))
    prompt = PROMPT_PATH.read_text().format(
        date=today.isoformat(), last=last.isoformat(), **_inputs(con, last, bid))
    report = agent._run_claude(prompt, cfg.vault_dir,
                               allowed_tools=wiki.wiki_tools(cfg.vault_dir),
                               model=cfg.run_models.get("board") or None)
    data = wiki.parse_report(report)
    known = {r["id"] for r in con.execute("SELECT id FROM notes WHERE vault='personal'")}
    known |= {r["id"] for r in con.execute("SELECT id FROM builds")}
    body, cited = wiki._clean_body(str(data.get("body") or ""), known)
    if "## " not in body:
        raise ValueError("the board run returned no board")
    desc = wiki.index_line(str(data.get("description") or "").strip()
                           or f"The board as of {today}")
    old = _fm()
    vault._emit(cfg.vault_dir, board_path(), body=body,
                generated_by="ozu/claude-code",
                fields=dict(id=bid, title=str(old.get("title") or TITLE), description=desc,
                            tags=["master", "projects", "review"],
                            created=str(old.get("created") or today.isoformat()),
                            updated=today.isoformat(), source=SOURCE,
                            related=[c for c in cited if c != bid],
                            related_why="on the board"))
    indexer.index_file(con, board_path())   # rewritten whole, so are its edges
    con.commit()
    vault._git_commit(cfg.vault_dir, f"board: weekly edition {today.isoformat()}")
    log.info("board rewritten: %d id(s) cited", len(cited))
    return True


def run_if_due() -> bool:
    """The cycle's board step: a no-op unless OZU_BOARD=1 and a week has
    passed. Never raises."""
    if not enabled():
        return False
    try:
        return run()
    except agent.AuthError as e:
        log.error("claude auth is broken (%s); the board waits for the next cycle", e)
    except Exception:
        log.exception("board run failed; the next cycle tries again")
    return False


def sheet_entry(through: str) -> dict | None:
    """The board's line on the Sunday wiki sheet: its new edition's summary,
    when one was written since the last sheet."""
    if not board_path().exists():
        return None
    fm = _fm()
    gen = fm.get("generated") or {}
    at = str(gen.get("at") or "") if isinstance(gen, dict) else ""
    if at <= through:
        return None
    return {"id": board_id(), "description": str(fm.get("description") or ""),
            "updated": str(fm.get("updated") or "")}
