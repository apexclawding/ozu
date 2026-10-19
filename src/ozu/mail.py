"""Return mail: what the ink became, gathered for the next edition.

Everything the runs produced since the last edition, back to the Scribe:
@claude build reports in full (including what needs the owner's judgment), the
notes their ink became, low-confidence transcriptions beside the ink image,
new red-ink actions, and ✓ receipts — every checkmark confirmed closed or
flagged as matching nothing (with the actions it nearly matched), because a
✓ that silently does nothing is invisible everywhere but journalctl.
@research briefs travel as their own documents and are only referenced here.
Learning notes (transcripts and their briefs, tagged `learning`) never
travel here at all: the Learn section of the evening edition is their mail.

Since 2026-09-11 this module only *gathers*: the mail is the first section
of every edition (`edition.py`), not a document of its own — an "<brand> Mail"
per tick was the biggest single reason the library filled up.

The `mailed` table is the ledger: a note is mailed exactly once, and — like
`actions` — the table survives `ozu index` (which only rebuilds notes,
edges and the FTS). On first run every pre-existing note is seeded as
already mailed, so the mail starts at "from now on", not with history.
A failed or unconfigured send marks nothing; those notes go out with the
next successful mail instead of being lost.
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from .indexer import _parse_frontmatter
from . import db

log = logging.getLogger("ozu.mail")


def _ensure_ledger(con) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS mailed (note_id TEXT PRIMARY KEY)")
    if db.get_meta(con, "mail_seeded") != "1":
        # Build notes are not seeded: their ids in `mailed` mean "result
        # reported" (edition._results), and a Build proposed before the
        # first mail has a result still to come.
        con.execute("INSERT OR IGNORE INTO mailed SELECT id FROM notes"
                    " WHERE (','||tags||',') NOT LIKE '%,build,%'")
        db.set_meta(con, "mail_seeded", "1")
    con.commit()


def _note_file(path_str: str) -> tuple[dict, str]:
    path = Path(path_str)
    if not path.exists():
        return {}, ""
    return _parse_frontmatter(path.read_text())


def gather(con) -> dict | None:
    """Unmailed personal notes plus unreported ✓ receipts, categorised for
    the mail layout. Receipts alone are enough to send — a page that only
    checks things off still deserves an answer."""
    rows = con.execute(
        "SELECT * FROM notes WHERE vault='personal'"
        " AND id NOT IN (SELECT note_id FROM mailed)"
        " AND (','||tags||',') NOT LIKE '%,learning,%'"   # the sheet's, not the mail's
        " AND (','||tags||',') NOT LIKE '%,build,%'"      # Builds travel as their own PDFs
        " ORDER BY created, id"
    ).fetchall()
    receipts = db.unreported_receipts(con)
    events = db.unreported_build_events(con)
    if not rows and not receipts and not events:
        return None
    work, research, plain = [], [], []
    for r in rows:
        tags = (r["tags"] or "").split(",")
        item = {"id": r["id"], "title": r["title"], "topic": r["topic"],
                "summary": r["summary"]}
        if "claude-work" in tags:
            _, item["body"] = _note_file(r["path"])
            work.append(item)
        elif "claude-research" in tags:
            research.append(item)
        else:
            item["actions"] = [a["text"] for a in con.execute(
                "SELECT text FROM actions WHERE note_id=?", (r["id"],))]
            if r["needs_review"]:
                fm, body = _note_file(r["path"])
                item["image"] = fm.get("image")
                item["body"] = body
            plain.append(item)
    return {
        "stamp": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "work": work, "research": research, "plain": plain,
        "receipts": [
            {"line": r["line"], "closed": r["closed"], "kind": r["kind"],
             "candidates": r["candidates"].split("\n") if r["candidates"] else []}
            for r in receipts],
        "receipt_ids": [r["id"] for r in receipts],
        "builds": [e["line"] for e in events],
        "build_event_ids": [e["id"] for e in events],
        "ids": [r["id"] for r in rows],
    }


def mark_sent(con, data: dict) -> None:
    """Write the ledgers for a mail that reached the device: notes mailed,
    receipts and build lines reported. The caller (edition.send) calls this
    only after Send-to-Kindle accepted the document."""
    con.executemany("INSERT OR IGNORE INTO mailed (note_id) VALUES (?)",
                    [(i,) for i in data["ids"]])
    con.commit()
    db.mark_receipts_reported(con, data["receipt_ids"])
    db.mark_build_events_reported(con, data["build_event_ids"])
    log.info("mailed %d note(s), %d receipt(s) and %d build line(s)",
             len(data["ids"]), len(data["receipt_ids"]),
             len(data["build_event_ids"]))
