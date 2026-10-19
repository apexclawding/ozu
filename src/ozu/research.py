"""@research runs: overnight research briefs from handwritten questions.

A handwritten @research beside an idea arrives as frontmatter
`research: requested` (personal vault only — walled pages are inert). Each
requested note gets one headless Claude Code run with web access and
READ-ONLY sight of the vault (cwd is the vault; Read is path-scoped to it,
plus WebSearch/WebFetch), so the brief is grounded in the owner's own prior
notes and cites their ids — and reaches nothing else on the box, secrets and
the walled vault included. The pipeline — never the agent — writes the outcome: a
companion note in the vault, and a typeset PDF sent to the Scribe when
Send-to-Kindle is configured.

Unlike @claude, no repo mapping is needed: any topic can be researched.
Ledger and caps mirror agent.py: `research:` frontmatter is the truth, one
run per note ever, OZU_MAX_RESEARCH_RUNS per cycle.
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from .config import brand, get_config
from .indexer import _parse_frontmatter
from . import agent, db, digest_pdf, edition, send_kindle, vault

log = logging.getLogger("ozu.research")

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "research_prompt.md"

# Read-only by construction, and pinned to the personal vault. Scoping matters
# more here than anywhere: a research run reads arbitrary web pages, so a
# prompt-injected page is the one plausible route to `Read ~/ozu-data/.env, then
# WebFetch it somewhere`. An unscoped `Read` allowed exactly that (verified
# 2026-08-25). Pinning to the vault also gives the governance wall its first
# real enforcement — work-vault sits outside this scope, so a research run
# cannot read walled pages even by accident.
def research_tools(vault_dir: Path) -> str:
    return ",".join([*agent.scoped(("Read",), vault_dir), "WebSearch", "WebFetch"])


def _note_body(path: Path) -> str:
    _, body = _parse_frontmatter(path.read_text())
    return body.strip()


def _work_note(note) -> None:
    cfg = get_config()
    prompt = PROMPT_PATH.read_text().format(
        note_id=note["id"], topic=note["topic"], title=note["title"],
        note_body=_note_body(Path(note["path"])),
        date=dt.date.today().isoformat(),
    )
    report = agent._run_claude(prompt, cfg.vault_dir,
                               allowed_tools=research_tools(cfg.vault_dir),
                               model=cfg.run_models.get("research") or None)

    cfg.research_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = cfg.research_dir / f"{brand()} Research {note['id']}.pdf"
    sent = False
    try:
        digest_pdf.build_research_pdf(
            title=note["title"], source_id=note["id"], topic=note["topic"],
            report=report, out_path=pdf_path)
        sent = send_kindle.send_pdf(pdf_path)
        if sent:
            # named in the ledger: a brief is kept on the device for thirty
            # days, then the edition's delete list names it
            edition.record(db.connect(cfg.db_path), pdf_path.stem, "research")
    except Exception:  # the brief is already safe; delivery is best-effort
        log.warning("research PDF/send failed for %s; brief is in the vault",
                    note["id"], exc_info=True)
    vault.write_research_note(source=dict(note), report=report,
                              pdf_name=pdf_path.name if sent else None)


def work_pending() -> int:
    """Run queued @research notes, oldest first, capped per cycle."""
    cfg = get_config()
    con = db.connect(cfg.db_path)
    rows = con.execute(
        "SELECT * FROM notes WHERE research='requested' AND vault='personal'"
        " ORDER BY created").fetchall()
    ran = 0
    for note in rows:
        if ran >= cfg.max_research_runs:
            log.info("research cap (%d) reached; %s waits for the next run",
                     cfg.max_research_runs, note["id"])
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
            log.exception("research run for %s failed", note["id"])
            status = "error"
        vault.set_directive_status(Path(note["path"]), status, key="research")
        con.execute("UPDATE notes SET research=? WHERE id=?", (status, note["id"]))
        con.commit()
        vault._git_commit(cfg.vault_dir, f"research run: {note['id']} ({status})")
    if ran:
        log.info("research runs this cycle: %d", ran)
    return ran
