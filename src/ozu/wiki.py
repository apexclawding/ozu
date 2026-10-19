"""The wiki: concept pages compiled from everything the vault takes in.

A learning brief says what one video said; a concept page says what you
know about a subject across every video, page of ink, voice memo and
research brief that touched it. Incremental: a little every cycle, steered
from paper on Sundays.

Each `ozu run` cycle offers the oldest few notes the wiki has not read yet
(`wiki_fed` is the ledger) to one headless Claude Code run. The run reads
them and the pages they bear on, and returns whole pages as JSON; the
pipeline — never the agent — writes `topics/wiki/wiki-<slug>.md`, keeps each
page's `created`, appends a dated line to its `## Changelog`, turns every
citation into an edge, and drops the brackets from any id that does not
exist rather than minting a broken link.

The run is confined like a learning run (DESIGN.md, invariant 11e): `Read`
pinned to the vault and nothing else, and read-only (8b). What it reads
includes briefs of third-party captions, so a prompt injected upstream can at
worst produce a bad page — which the Sunday sheet puts in front of you, and
which your ink corrects.

The return trip is the "Wiki this week" section of Sunday's evening edition:
pages new or changed since the last sheet, with their changelog and their
claims. A margin note on a claim, or a line struck through it, comes back
through the annotation pass as an ordinary note; that note is new material,
so the next cycle feeds it to the run, whose prompt ranks your ink above any
brief. No `☐` is printed (invariant 5): a correction is not an action.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from pathlib import Path

from .config import get_config
from .indexer import _parse_frontmatter
from . import agent, db, indexer, okf, vault

log = logging.getLogger("ozu.wiki")

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "wiki_prompt.md"
TOPIC = "wiki"
SOURCE = "wiki:compiled"
# What the wiki is compiled from, by the producer in `source:` — your ink
# (notebook pages and your annotations of printed ones), your voice, the
# learning briefs and the research briefs. Not the raw transcripts (the brief
# is their distillation), not Builds and their reports (records of work, not
# knowledge).
FEED_SOURCES = ("scribe", "voice", "learn", "research")
PAGE_ID = re.compile(r"^wiki-[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_ID = 64
CHANGELOG = "## Changelog"
CHANGELOG_KEEP = 30
SHEET_PAGES = 8
SHEET_CLAIMS = 8
SHEET_META = "wiki_sheet_through"
FAILED_META = "wiki_failed_on"


def wiki_tools(vault_dir: Path) -> str:
    return ",".join(agent.scoped(("Read",), vault_dir))


def wiki_dir() -> Path:
    return get_config().vault_dir / "topics" / TOPIC


def _frontmatter_of(path: Path) -> tuple[dict, str]:
    try:
        return _parse_frontmatter(path.read_text())
    except OSError:
        return {}, ""


def _rel(path: Path, root: Path) -> str:
    return os.path.relpath(os.path.abspath(path), os.path.abspath(root))


# ---------- what the wiki has not read yet ----------

def pending(con) -> list:
    """Personal notes from a feeding producer that no run has been offered,
    oldest first. Walled notes never reach here: they are not `personal`."""
    rows = con.execute(
        "SELECT * FROM notes WHERE vault='personal' AND topic != ?"
        " AND id NOT IN (SELECT note_id FROM wiki_fed) ORDER BY created, id",
        (TOPIC,)).fetchall()
    out = []
    for r in rows:
        fm, _ = _frontmatter_of(Path(r["path"]))
        if okf.source_kind(str(fm.get("source", ""))) in FEED_SOURCES:
            out.append(r)
    return out


def pages() -> list[dict]:
    """The concept pages as they stand: id, title, description, path."""
    out = []
    d = wiki_dir()
    for p in sorted(d.glob("wiki-*.md")) if d.is_dir() else []:
        fm, body = _frontmatter_of(p)
        out.append({"id": str(fm.get("id") or p.stem),
                    "title": str(fm.get("title") or p.stem),
                    "description": str(fm.get("description") or ""),
                    "path": p, "fm": fm, "body": body})
    return out


# ---------- one run ----------

def _prompt(batch: list, existing: list[dict], vault_dir: Path) -> str:
    cfg = get_config()
    lines = []
    for r in batch:
        fm, _ = _frontmatter_of(Path(r["path"]))
        lines.append(f"- {r['id']} — {okf.type_for(str(fm.get('source', '')))}"
                     f" — `{_rel(Path(r['path']), vault_dir)}` — {r['title']}")
    listing = "\n".join(f"- {p['id']} — {p['title']} — {p['description']}"
                        for p in existing) or "(none yet — this run starts the wiki)"
    return PROMPT_PATH.read_text().format(
        date=dt.date.today().isoformat(), count=len(batch),
        batch="\n".join(lines), page_count=len(existing), pages=listing,
        max_pages=cfg.max_wiki_pages)


def parse_report(report: str) -> dict:
    """The run's JSON, tolerant of a fence or a sentence around it."""
    s = (report or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j < i:
        raise ValueError("the wiki run returned no JSON object")
    data = json.loads(s[i:j + 1])
    if not isinstance(data, dict) or not isinstance(data.get("pages", []), list):
        raise ValueError("the wiki run's JSON has no `pages` list")
    ignored = data.get("ignored") or {}
    data["ignored"] = ignored if isinstance(ignored, dict) else {}
    data["pages"] = data.get("pages") or []
    return data


def _clean_body(body: str, known: set[str]) -> tuple[str, list[str]]:
    """The page body as the pipeline keeps it: no H1 (the title is ours), no
    changelog (ours too), and no link to an id that does not exist — its
    brackets are dropped so the claim survives without a broken edge.
    Returns the body and the ids it cites, in first-cited order."""
    body = re.sub(r"(?m)^# [^\n]*\n?", "", body or "")
    body = body.split(CHANGELOG)[0].strip()
    cited: list[str] = []

    def keep(m: re.Match) -> str:
        nid = m.group(1)
        if nid in known:
            if nid not in cited:
                cited.append(nid)
            return m.group(0)
        log.warning("wiki: dropping link to unknown id %s", nid)
        return nid

    return okf.WIKILINK.sub(keep, body), cited


def _changelog(old_body: str, today: str, change: str) -> list[str]:
    old = old_body.split(CHANGELOG, 1)[1] if CHANGELOG in old_body else ""
    lines = [s.strip() for s in old.strip().splitlines() if s.strip().startswith("- ")]
    lines.append(f"- {today}: {change.strip() or 'updated'}")
    return lines[-CHANGELOG_KEEP:]


def _write_page(con, page: dict, known: set[str], titles: dict[str, tuple[str, str]],
                today: str) -> tuple[str, list[str]] | None:
    """Validate and write one returned page. Returns (id, cited ids) or None
    for a page the pipeline refuses — a bad id, or no body."""
    cfg = get_config()
    pid = str(page.get("id") or "").strip().lower()
    if not PAGE_ID.match(pid) or len(pid) > MAX_ID:
        log.warning("wiki: refusing page with id %r", pid)
        return None
    body, cited = _clean_body(str(page.get("body") or ""), known)
    if not body:
        log.warning("wiki: refusing %s — empty body", pid)
        return None
    title = str(page.get("title") or pid).strip()[:120]
    desc = (str(page.get("description") or "").strip() or title)[:200]
    tags = ["wiki"] + [t for t in (str(x).strip().lower() for x in page.get("tags") or [])
                       if re.fullmatch(r"[a-z0-9][a-z0-9-]*", t) and t != "wiki"][:8]

    d = wiki_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{pid}.md"
    old_fm, old_body = _frontmatter_of(path) if path.exists() else ({}, "")
    created = str(old_fm.get("created") or today)
    log_lines = _changelog(old_body, today, str(page.get("change") or ""))
    full = f"{body}\n\n{CHANGELOG}\n\n" + "\n".join(log_lines)

    notes_cited = [c for c in cited if not c.startswith("wiki-")]
    sources = [{"id": c, "resource": "/" + titles[c][1], "title": titles[c][0]}
               for c in notes_cited if c in titles]
    vault._emit(cfg.vault_dir, path, body=full, generated_by="ozu/claude-code",
                fields=dict(id=pid, title=title, description=desc, tags=tags,
                            created=created, updated=today, source=SOURCE,
                            related=cited, related_why="cited on wiki page"),
                sources=sources, regenerate=False)
    indexer.index_file(con, path)    # the page is rewritten whole, so are its edges
    con.commit()
    return pid, cited


def _record(con, batch: list, outcome: dict[str, str], today: str) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO wiki_fed (note_id, fed_on, outcome) VALUES (?,?,?)",
        [(r["id"], today, outcome.get(r["id"], "unplaced")) for r in batch])
    con.commit()


def run_batch(con, batch: list) -> int:
    """One run over `batch`. Returns the number of pages written. Raises
    agent.AuthError untouched so the caller can leave the batch queued."""
    cfg = get_config()
    today = dt.date.today().isoformat()
    existing = pages()
    report = agent._run_claude(_prompt(batch, existing, cfg.vault_dir), cfg.vault_dir,
                               allowed_tools=wiki_tools(cfg.vault_dir),
                               model=cfg.run_models.get("wiki") or None)
    data = parse_report(report)

    rows = con.execute("SELECT id, title, path FROM notes WHERE vault='personal'").fetchall()
    titles = {r["id"]: (r["title"], _rel(Path(r["path"]), cfg.vault_dir)) for r in rows}
    returned = {str(p.get("id") or "").strip().lower() for p in data["pages"]
                if isinstance(p, dict)}
    known = set(titles) | {p["id"] for p in existing} | {i for i in returned if PAGE_ID.match(i)}

    batch_ids = {r["id"] for r in batch}
    placed: dict[str, list[str]] = {}
    written = 0
    for page in data["pages"][:cfg.max_wiki_pages]:
        if not isinstance(page, dict):
            continue
        done = _write_page(con, page, known, titles, today)
        if not done:
            continue
        written += 1
        pid, cited = done
        for c in cited:
            if c in batch_ids:
                placed.setdefault(c, []).append(pid)

    outcome = {nid: ",".join(pids) for nid, pids in placed.items()}
    for nid, why in data["ignored"].items():
        if nid in batch_ids and nid not in outcome:
            outcome[nid] = f"ignored: {str(why).strip()[:120]}"
    _record(con, batch, outcome, today)
    okf.regenerate(cfg.vault_dir)
    vault._git_commit(cfg.vault_dir,
                      f"wiki: {written} page(s) from {len(batch)} note(s)")
    log.info("wiki run: %d note(s) read, %d page(s) written, %d placed",
             len(batch), written, len(outcome))
    return written


def work_pending() -> int:
    """The cycle's wiki step: one run over the oldest unread notes."""
    cfg = get_config()
    con = db.connect(cfg.db_path)
    batch = pending(con)[:cfg.max_wiki_notes]
    if not batch:
        return 0
    # After a failed run the wiki rests until tomorrow: a failure that is not
    # the batch's fault (a prompt the model cannot satisfy) would otherwise
    # mark the whole backlog `error`, six notes every two hours.
    today = dt.date.today().isoformat()
    if db.get_meta(con, FAILED_META) == today:
        return 0
    try:
        return run_batch(con, batch)
    except agent.AuthError as e:
        log.error("claude auth is broken (%s); the wiki batch stays queued", e)
        return 0
    except Exception:
        log.exception("wiki run failed; %d note(s) marked error "
                      "(`ozu wiki requeue` offers them again)", len(batch))
        _record(con, batch, {r["id"]: "error" for r in batch}, today)
        db.set_meta(con, FAILED_META, today)
        return 0


def requeue(errors_only: bool = True) -> int:
    """Offer notes to the wiki again: the failed ones, or (errors_only=False)
    everything — a full recompile, which the backlog then drains."""
    con = db.connect(get_config().db_path)
    n = con.execute("DELETE FROM wiki_fed" + (" WHERE outcome='error'" if errors_only else "")
                    ).rowcount
    con.commit()
    return n


def status(con) -> dict:
    fed = {r["k"]: r["n"] for r in con.execute(
        "SELECT CASE WHEN outcome LIKE 'ignored:%' THEN 'ignored'"
        " WHEN outcome IN ('error','unplaced') THEN outcome ELSE 'placed' END k,"
        " COUNT(*) n FROM wiki_fed GROUP BY k")}
    return {"pages": len(pages()), "pending": len(pending(con)), **fed}


# ---------- the Sunday sheet ----------

def _section_lines(body: str, heading: str) -> list[str]:
    out, inside = [], False
    for line in body.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower() == heading
            continue
        if inside and line.strip():
            out.append(line.rstrip())
    return out


_CITES = re.compile(r"\s*[—-]\s*(\[\[[^\]]+\]\][,;\s]*)+$")


def _printable(claim: str, titles: dict[str, str] | None = None) -> str:
    """A line as the page prints it: a claim's trailing citation run goes
    (it is in the vault; on paper it is noise), and an inline id becomes the
    title of the note it names — "from ‘Paste This Into Claude…’", not a
    sixty-character slug."""
    titles = titles or {}
    return okf.WIKILINK.sub(lambda m: f"‘{titles[m.group(1)]}’" if m.group(1) in titles
                            else m.group(1), _CITES.sub("", claim))


def gather_sheet(con, today: dt.date) -> dict | None:
    """Pages written since the last sheet, and how many notes the wiki could
    not read. None when there is neither."""
    through = db.get_meta(con, SHEET_META) or \
        (dt.datetime.combine(today - dt.timedelta(days=7), dt.time()).strftime("%Y-%m-%dT%H:%M:%SZ"))
    since_day = through[:10]

    def at(fm: dict) -> str:
        gen = fm.get("generated") or {}
        return str(gen.get("at") or "") if isinstance(gen, dict) else ""

    titles = {r["id"]: str(r["title"]).removeprefix("Learning: ") for r in
              con.execute("SELECT id, title FROM notes WHERE vault='personal'")}
    changed = []
    for p in pages():
        if at(p["fm"]) <= through:
            continue
        log_lines = [line[2:] for line in _section_lines(p["body"], "changelog")
                     if line.startswith("- ") and line[2:12] >= since_day]
        claims = [_printable(c) for c in _section_lines(p["body"], "what i know")
                  if c.lstrip().startswith(("- ", "* "))][:SHEET_CLAIMS]
        changed.append({
            "id": p["id"], "title": p["title"], "description": p["description"],
            "new": str(p["fm"].get("created") or "") >= since_day,
            "updated": str(p["fm"].get("updated") or ""),
            "changes": [_printable(x, titles) for x in log_lines], "claims": claims,
        })
    changed.sort(key=lambda c: (not c["new"], c["updated"], c["id"]), reverse=False)
    more = max(0, len(changed) - SHEET_PAGES)

    errors = con.execute("SELECT COUNT(*) c FROM wiki_fed WHERE outcome='error'"
                         ).fetchone()["c"]
    if not changed and not errors:
        return None
    return {"date": today.isoformat(), "pages": changed[:SHEET_PAGES], "more": more,
            "errors": errors, "total": len(pages())}


def mark_sheet_sent(con) -> None:
    """Called only after the edition carrying the sheet reached the device."""
    db.set_meta(con, SHEET_META, okf.now_iso())
