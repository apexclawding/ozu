"""Weekly digest, written to ~/ozu-data/digests/ as Markdown.

Contents: new notes this week, newly discovered connections, open red-ink
actions, transcriptions needing review, and one "forgotten gem" — an older
note that shares tags with this week's thinking. Resurfacing is the point.

Delivery is deliberately local: a file on disk and a log line, no outbound
call to any messaging gateway. The Scribe gets the same content as the
digest section of Sunday's evening edition (`edition.py`), typeset from the
very `gather()` dict this module wrote the Markdown from.
"""
from __future__ import annotations

import datetime as dt
import logging
import random
from pathlib import Path

from .config import brand, canonical_topic, get_config
from . import db, learn

log = logging.getLogger("ozu.digest")


def _deliver(text: str) -> Path:
    """Write the digest to ~/ozu-data/digests/YYYY-MM-DD.md and return the path."""
    cfg = get_config()
    cfg.digest_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.digest_dir / f"{dt.date.today().isoformat()}.md"
    path.write_text(text + "\n")
    log.info("digest written to %s", path)
    print(text)
    return path


def gather(days: int = 7) -> dict:
    """Everything a digest needs, as rows — shared by the Markdown and the
    PDF renderers so the two can never drift apart on content."""
    cfg = get_config()
    con = db.connect(cfg.db_path)
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()

    new = con.execute(
        "SELECT id, title, topic, summary FROM notes"
        " WHERE vault='personal' AND created >= ? ORDER BY created", (since,)
    ).fetchall()
    review = con.execute(
        "SELECT id, title, path FROM notes WHERE vault='personal' AND needs_review=1"
    ).fetchall()
    built = con.execute(
        "SELECT title, summary FROM notes WHERE vault='personal' AND created >= ?"
        " AND (','||tags||',') LIKE '%,claude-work,%'", (since,)
    ).fetchall()
    claude_queued = con.execute(
        "SELECT title, topic FROM notes WHERE vault='personal' AND claude='requested'"
    ).fetchall()
    claude_failed = con.execute(
        "SELECT title FROM notes WHERE vault='personal' AND claude='error'"
    ).fetchall()
    actions = con.execute(
        "SELECT a.text, n.title FROM actions a JOIN notes n ON n.id=a.note_id"
        " WHERE a.done=0 ORDER BY a.created DESC LIMIT 10"
    ).fetchall()

    recent_ids = {r["id"] for r in new}
    edges = [e for nid in recent_ids for e in db.neighbors(con, nid, depth=1)]
    new_links = [e for e in edges if not (e["src"] in recent_ids and e["dst"] in recent_ids)][:5]

    research_built = con.execute(
        "SELECT title, summary FROM notes WHERE vault='personal' AND created >= ?"
        " AND (','||tags||',') LIKE '%,claude-research,%'", (since,)
    ).fetchall()
    research_queued = con.execute(
        "SELECT title FROM notes WHERE vault='personal' AND research='requested'"
    ).fetchall()
    research_failed = con.execute(
        "SELECT title FROM notes WHERE vault='personal' AND research='error'"
    ).fetchall()
    learned, coming_back = learn.week_counts(con, days=days)
    learn_failed = con.execute(
        "SELECT title FROM notes WHERE vault='personal' AND learn='error'"
    ).fetchall()

    return {
        "date": dt.date.today().isoformat(),
        "new": new, "review": review, "built": built,
        "r_built": research_built, "r_queued": research_queued,
        "r_failed": research_failed,
        "learned": learned, "coming_back": coming_back, "l_failed": learn_failed,
        "queued": claude_queued, "failed": claude_failed,
        "actions": actions, "links": new_links,
        "gem": _forgotten_gem(con, new),
        "repos": cfg.claude_repos,
    }


def build_digest(days: int = 7, data: dict | None = None) -> str:
    cfg = get_config()
    data = data or gather(days)
    new, review, built = data["new"], data["review"], data["built"]
    claude_queued, claude_failed = data["queued"], data["failed"]
    r_built, r_queued, r_failed = data["r_built"], data["r_queued"], data["r_failed"]
    actions, new_links, gem = data["actions"], data["links"], data["gem"]

    lines = [f"🧠 *{brand()} weekly digest* — {dt.date.today().isoformat()}", ""]
    lines.append(f"*New notes ({len(new)}):*")
    for r in new[:12]:
        lines.append(f"• [{r['topic']}] {r['title']} — {r['summary']}")
    if new_links:
        lines.append("\n*New connections:*")
        for e in new_links:
            reason = f" ({e['reason']})" if e.get("reason") else ""
            lines.append(f"• {e['src']} ↔ {e['dst']}{reason}")
    if actions:
        lines.append("\n*Open actions (red ink):*")
        for a in actions:
            lines.append(f"☐ {a['text']}  ·  _{a['title']}_")
    if built:
        lines.append("\n*Claude built (@claude):*")
        for b in built:
            lines.append(f"• {b['title']} — {b['summary']}")
    if claude_queued:
        lines.append("\n*@claude queued:*")
        for qn in claude_queued:
            where = ("runs tonight" if canonical_topic(qn["topic"]) in cfg.claude_repos
                     else f"waiting — no repo mapped for '{qn['topic']}'")
            lines.append(f"• {qn['title']} ({where})")
    if claude_failed:
        lines.append("\n*@claude failed:* " + ", ".join(f["title"] for f in claude_failed[:5])
                     + " — see journalctl --user -u ozu-nightly")
    if r_built:
        lines.append("\n*Research briefs (@research):*")
        for b in r_built:
            lines.append(f"• {b['title']} — {b['summary']}")
    if r_queued:
        lines.append("\n*@research queued:* " + ", ".join(qn["title"] for qn in r_queued[:5]))
    if r_failed:
        lines.append("\n*@research failed:* " + ", ".join(f["title"] for f in r_failed[:5])
                     + " — see journalctl --user -u ozu-nightly")
    if data["learned"] or data["coming_back"]:
        n, m = data["learned"], data["coming_back"]
        lines.append(f"\n📺 *Learned this week:* {n} video{'' if n == 1 else 's'}"
                     f" · {m} coming back next week")
    if data["l_failed"]:
        lines.append("\n*@learn failed:* " + ", ".join(f["title"] for f in data["l_failed"][:5])
                     + " — see journalctl --user -u ozu-nightly")
    if review:
        lines.append(f"\n*Needs review ({len(review)}):* " + ", ".join(r["title"] for r in review[:5]))
    if gem:
        lines.append(f"\n💎 *Forgotten gem:* {gem['title']} — {gem['summary']} (from {gem['created']})")
    if not new and not actions:
        lines.append("Quiet week — the Scribe misses you.")
    return "\n".join(lines)


def _forgotten_gem(con, new_rows) -> dict | None:
    cutoff = (dt.date.today() - dt.timedelta(days=90)).isoformat()
    recent_tag_set = set()
    for r in new_rows:
        row = con.execute("SELECT tags FROM notes WHERE id=?", (r["id"],)).fetchone()
        if row and row["tags"]:
            recent_tag_set |= set(row["tags"].split(","))
    old = con.execute(
        "SELECT id, title, summary, tags, created FROM notes"
        " WHERE vault='personal' AND created < ?", (cutoff,)
    ).fetchall()
    scored = [o for o in old if recent_tag_set & set((o["tags"] or "").split(","))]
    pool = scored or list(old)
    return dict(random.choice(pool)) if pool else None


def run(data: dict | None = None) -> Path:
    """Write the Markdown digest. The typeset copy no longer leaves here:
    Sunday's evening edition carries it, from the same `data`."""
    return _deliver(build_digest(data=data))
