"""Learning briefs from pinned YouTube videos, and the evening "<brand> Learn" sheet.

A video the owner saves to a YouTube playlist arrives here as a transcript
note under `topics/learning/` with frontmatter `learn: requested` — written by
a capture bridge watching a playlist, and folded into the vault by
`ozu run`'s sync. Each requested note gets one headless Claude
Code run that reads the transcript and the vault and returns a brief:
takeaways, techniques and tools, where it touches their projects (cited by note
id), one exercise, one question. The pipeline — never the agent — writes the
outcome as a companion "Learning:" note.

The run is confined harder than @research. A transcript is text nobody here
wrote, so the allowlist is `Read` pinned to the vault and nothing else: no
web tools, no Bash, no Edit. A prompt injected through the captions can
produce a bad brief, which the owner reads with their own eyes; it cannot reach
the network, the secrets, or the walled vault. Runs go through
`agent._run_claude`, which strips the API key, so they use the `claude`
CLI's own login rather than the pipeline's API key.

The sheet is the return trip. `gather()` collects whatever is new plus
whatever is due again on a fixed spacing (+2, +7, +30 days after the first
sheet); it rides as the Learn section of the evening edition (`edition.py`),
and `mark_sent()` writes the `learn_sheet` ledger only after that edition
reached the Scribe — so a failed send means the same items go out the next
evening, never that they are lost. Nothing new and nothing due means no
section, and on its own no evening edition at all.

Ledger and caps mirror research.py: `learn:` frontmatter is the truth, one
run per note ever, OZU_MAX_LEARN_RUNS per cycle.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

from .config import get_config
from .indexer import _parse_frontmatter
from . import agent, db, vault

log = logging.getLogger("ozu.learn")

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "learn_prompt.md"

# Spacing: a note first sent on day D comes back on D+2, D+7 (two days plus
# seven) and D+30 after that — four meetings with each concept, then silence.
# Keyed by the stage being *entered*: stage s is due STAGES[s] days after the
# sheet that carried stage s-1.
STAGES = {1: 2, 2: 7, 3: 30}
LAST_STAGE = max(STAGES)
NEW_PER_SHEET = 3
DUE_PER_SHEET = 2
SKIPPED_STAGE = -1     # ledger marker: a skipped transcript was reported once

# Read-only, pinned to the vault, and that is the whole allowlist. research.py
# adds WebSearch/WebFetch because a research question needs the web; a
# learning brief needs only the transcript and the vault, and giving a run
# that reads third-party text any network tool is the one thing not to do.
def learn_tools(vault_dir: Path) -> str:
    return ",".join(agent.scoped(("Read",), vault_dir))


def _frontmatter_of(path: Path) -> tuple[dict, str]:
    try:
        return _parse_frontmatter(path.read_text())
    except OSError:
        return {}, ""


def _rel(path: Path, root: Path) -> str:
    """Vault-relative path for the prompt. relpath rather than relative_to:
    the note path in the db may spell the vault through a symlink."""
    return os.path.relpath(os.path.abspath(path), os.path.abspath(root))


def _work_note(note) -> None:
    cfg = get_config()
    path = Path(note["path"])
    fm, _ = _frontmatter_of(path)
    prompt = PROMPT_PATH.read_text().format(
        note_id=note["id"], title=note["title"],
        channel=fm.get("channel", "") or "unknown channel",
        duration=str(fm.get("duration", "")).strip('"') or "unknown length",
        url=fm.get("url", "") or "",
        transcript_path=_rel(path, cfg.vault_dir),
        date=dt.date.today().isoformat(),
    )
    report = agent._run_claude(prompt, cfg.vault_dir,
                               allowed_tools=learn_tools(cfg.vault_dir),
                               model=cfg.run_models.get("learn") or None)
    vault.write_learning_note(source=dict(note), report=report, video=fm)


def work_pending() -> int:
    """Run queued transcripts, oldest first, capped per cycle."""
    cfg = get_config()
    con = db.connect(cfg.db_path)
    rows = con.execute(
        "SELECT * FROM notes WHERE learn='requested' AND vault='personal'"
        " ORDER BY created, id").fetchall()
    ran = 0
    for note in rows:
        if ran >= cfg.max_learn_runs:
            log.info("learn cap (%d) reached; %s waits for the next run",
                     cfg.max_learn_runs, note["id"])
            break
        ran += 1
        status = "done"
        try:
            _work_note(note)
        except agent.AuthError as e:
            log.error("claude auth is broken (%s); leaving %s and the rest "
                      "queued — re-authenticate on the VPS", e, note["id"])
            break
        except Exception:
            log.exception("learn run for %s failed", note["id"])
            status = "error"
        vault.set_directive_status(Path(note["path"]), status, key="learn")
        con.execute("UPDATE notes SET learn=? WHERE id=?", (status, note["id"]))
        con.commit()
        vault._git_commit(cfg.vault_dir, f"learn run: {note['id']} ({status})")
    if ran:
        log.info("learn runs this cycle: %d", ran)
    return ran


# ---------- the evening sheet ----------

def _brief_of(body: str) -> str:
    """The brief part of a learning note: everything from its first `##`
    heading on. Above it sits the provenance line and the URL."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## "):
            return "\n".join(lines[i:]).strip()
    return body.strip()


def _sections(brief: str) -> dict[str, list[str]]:
    """Heading → its non-empty lines, headings lower-cased and stripped of
    `#`. Bullets keep their marker so callers can tell a list from prose."""
    out: dict[str, list[str]] = {}
    current = ""
    for line in brief.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            current = s.lstrip("#").strip().lower()
            out.setdefault(current, [])
        else:
            out.setdefault(current, []).append(s)
    return out


def _first_line(sections: dict, key: str) -> str:
    for heading, lines in sections.items():
        if heading.startswith(key) and lines:
            s = lines[0]
            return s[2:].strip() if s.startswith(("- ", "* ")) else s
    return ""


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _learning_rows(con):
    return con.execute(
        "SELECT * FROM notes WHERE vault='personal'"
        " AND (','||tags||',') LIKE '%,claude-learn,%' ORDER BY created, id"
    ).fetchall()


def gather(con, today: dt.date) -> dict | None:
    """Everything one evening's sheet carries: the new briefs, the ones due
    again, the transcripts that could not be read. None when there is
    nothing — the sheet is only sent when it has something to say."""
    rows = _learning_rows(con)
    stages = {}
    for r in con.execute("SELECT note_id, stage, sent_on FROM learn_sheet"):
        stages.setdefault(r["note_id"], {})[r["stage"]] = r["sent_on"]

    new, due = [], []
    for r in rows:
        sent = stages.get(r["id"], {})
        fm, body = _frontmatter_of(Path(r["path"]))
        brief = _brief_of(body)
        if not sent:
            if len(new) < NEW_PER_SHEET:
                new.append({
                    "id": r["id"], "title": r["title"].removeprefix("Learning: "),
                    "channel": fm.get("channel", ""),
                    "duration": str(fm.get("duration", "")).strip('"'),
                    "pinned": fm.get("pinned", "") or r["created"], "brief": brief,
                })
            continue
        prev = max(sent)
        if prev >= LAST_STAGE or prev < 0:
            continue
        nxt = prev + 1
        due_on = dt.date.fromisoformat(sent[prev]) + dt.timedelta(days=STAGES[nxt])
        if due_on <= today:
            secs = _sections(brief)
            due.append({
                "id": r["id"], "title": r["title"].removeprefix("Learning: "),
                "stage": nxt, "nth": _ordinal(nxt + 1), "due_on": due_on,
                "learned": sent.get(0, sent[prev]),
                "cue": _first_line(secs, "takeaway"),
                "question": _first_line(secs, "question"),
            })
    due.sort(key=lambda d: (d["due_on"], d["id"]))
    due = due[:DUE_PER_SHEET]

    skipped = []
    for r in con.execute(
            "SELECT id, title, path FROM notes WHERE vault='personal'"
            " AND learn='skipped' AND id NOT IN"
            " (SELECT note_id FROM learn_sheet WHERE stage=?) ORDER BY created, id",
            (SKIPPED_STAGE,)):
        fm, _ = _frontmatter_of(Path(r["path"]))
        skipped.append({"id": r["id"], "title": r["title"],
                        "reason": fm.get("skip_reason", "") or "could not be read"})

    if not new and not due and not skipped:
        return None
    return {
        "date": today.isoformat(),
        "weekday": today.strftime("%A %-d %B %Y"),
        "new": new, "due": due, "skipped": skipped,
    }


def mark_sent(con, data: dict, stamp: str) -> None:
    """The ledger for a sheet that reached the device, as the Learn section
    of the evening edition of `stamp`. One sheet per evening is the
    edition's rule now (one evening per day); a backlog still drains at
    NEW_PER_SHEET a day rather than in one long document."""
    rows = ([(n["id"], 0, stamp) for n in data["new"]]
            + [(d["id"], d["stage"], stamp) for d in data["due"]]
            + [(s["id"], SKIPPED_STAGE, stamp) for s in data["skipped"]])
    con.executemany(
        "INSERT OR IGNORE INTO learn_sheet (note_id, stage, sent_on) VALUES (?,?,?)",
        rows)
    con.commit()
    log.info("sheet %s: %d new, %d due again, %d skipped", stamp,
             len(data["new"]), len(data["due"]), len(data["skipped"]))


def week_counts(con, today: dt.date | None = None, days: int = 7) -> tuple[int, int]:
    """For the digest: briefs written in the last `days`, and notes whose
    next return falls within the next `days`."""
    today = today or dt.date.today()
    since = (today - dt.timedelta(days=days)).isoformat()
    learned = con.execute(
        "SELECT COUNT(*) c FROM notes WHERE vault='personal' AND created >= ?"
        " AND (','||tags||',') LIKE '%,claude-learn,%'", (since,)).fetchone()["c"]
    stages: dict[str, dict[int, str]] = {}
    for r in con.execute("SELECT note_id, stage, sent_on FROM learn_sheet WHERE stage >= 0"):
        stages.setdefault(r["note_id"], {})[r["stage"]] = r["sent_on"]
    horizon = today + dt.timedelta(days=days)
    coming = 0
    for sent in stages.values():
        prev = max(sent)
        if prev >= LAST_STAGE:
            continue
        due_on = dt.date.fromisoformat(sent[prev]) + dt.timedelta(days=STAGES[prev + 1])
        if due_on <= horizon:
            coming += 1
    return learned, coming
