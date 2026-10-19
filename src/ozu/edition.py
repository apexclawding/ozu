"""The editions: two documents a day instead of one per event.

The first design mailed a PDF per event — an "<brand> Mail" whenever a tick
produced anything (up to twelve a day), a brief every morning, a sheet every
evening, a Build result the moment it executed, the digest on Sunday.
Fifty-seven documents in nineteen days, three-quarters of them read
once and never annotated, in a library that has no folders a sender can
target and no bulk delete on the device. The fix is on this side: fewer,
better-shaped documents, and a pipeline that says which ones are dead.

**Morning** (07:00) carries what the ink became since last evening, any
Build result that landed, the research briefs that arrived overnight (named;
they travel as their own documents), and the delete list. **Evening**
(20:00) carries the day's ink, results, the Learn sheet, the weekly digest
and the wiki sheet on Sundays, and the delete list. **Extra** is the same shape on demand (`ozu edition extra`)
for a day one cannot wait. Build *proposals* keep their five-minute latency
and their own documents: that latency is the feature of the paper loop.

Processing is unchanged — the tick still reads ink, closes actions and moves
Builds every five minutes; only the *paper* is batched. Each edition is
named `<brand> Morning <date>` (and so on), `<brand>` being OZU_BRAND. The
ledgers are the ones that already existed (`mailed`, `learn_sheet`, `receipts`,
`build_events`), so an edition consumes exactly what an event-document would
have, plus one new one: `delivered`, every document that reached the device
by the name the library shows. That table is what the delete list is
computed from, by rules a reader can check against the page:

  - a Build revision is dead once a later revision exists, or once the Build
    is done or dropped (an `error` Build is kept — its page re-arms it);
  - an edition is dead once a newer edition has been delivered, so the two
    most recent are the ones on the device;
  - a research brief is dead after thirty days (it is in the vault and over
    MCP for good);
  - every document of a kind that no longer exists as its own document (Ozu
    Mail, Digest, a Build result, a simulated proposal) is dead
    on sight — that is the backlog the first edition names in full.

A document is *listed* for a week from the day it became dead, then counted
("and 12 older ones") rather than named: the monthly sweep on Amazon's
Manage Your Content page has checkboxes, the device does not, and a list that
only grows is a list nobody reads. Deletion itself stays a gesture on the
device; nothing here can, or should, reach into the Kindle library.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path

from .config import brand, get_config, incoming_prefix_re
from . import db, digest_pdf, send_kindle

log = logging.getLogger("ozu.edition")

KINDS = ("morning", "evening", "extra")
RESEARCH_KEEP_DAYS = 30
DEAD_LIST_DAYS = 7

# Documents delivered by the pre-edition pipeline, by the filenames it used.
# Seeded into `delivered` once so the first edition's delete list covers the
# backlog already on the device.
_LEGACY = (
    ("mail_dir", "mail", "{brand} Mail *.pdf"),
    ("digest_dir", "digest", "{brand} Digest *.pdf"),
    ("research_dir", "research", "{brand} Research *.pdf"),
    ("builds_dir", "build", "{brand} Build *.pdf"),
)
_ID_PART = r"(B-\d{6}-[0-9a-f]{4})"


def _legacy_build(name: str) -> re.Match | None:
    """A pre-edition Build file of our own: `<brand> Build B-… r2|result`."""
    return re.match(rf"^{re.escape(brand())} Build {_ID_PART} (?:r(\d+)|(result))$",
                    name)


def _build_name() -> re.Pattern:
    """The post-edition Build filename, `<brand> Build r2 - <title> (B-…)`,
    under any name in OZU_DIGEST_PREFIXES and in any case: the device keeps
    the name a document was sent under."""
    return re.compile(rf"^{incoming_prefix_re()}[ \t]+Build[ \t]+r(\d+) - .* \({_ID_PART}\)$",
                      re.IGNORECASE)


def _tilde(path: Path) -> str:
    """A path as printed on paper: the user's home abbreviated to `~`."""
    home = Path.home()
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


_REASON = {
    "mail": "an old {brand} Mail — everything on it is in the vault",
    "digest": "an old digest — the Markdown is in {digest_dir}",
    "result": "the Build is done — its report is in the Build note",
    "simulate": "a simulated proposal — nothing behind it",
}


# ---------------------------------------------------------------------------
# ledgers

def ensure_ledgers(con) -> None:
    """The `mailed` ledger (mail.py owns it), plus a one-time seed of
    `delivered` from the documents the old pipeline left on disk, and of
    `mailed` with every Build whose result already went out as its own
    PDF — otherwise the first edition would report every finished Build
    again."""
    from . import mail
    mail._ensure_ledger(con)
    if db.get_meta(con, "delivered_seeded") == "1":
        return
    cfg = get_config()
    for attr, kind, pattern in _LEGACY:
        pattern = pattern.format(brand=brand())
        folder = getattr(cfg, attr)
        if not folder.is_dir():
            continue
        for pdf in sorted(folder.glob(pattern)):
            sent_on = dt.date.fromtimestamp(pdf.stat().st_mtime).isoformat()
            build_id = rev = None
            row_kind = kind
            if kind == "build":
                m = _legacy_build(pdf.stem)
                if not m:
                    continue
                build_id = m.group(1)
                if m.group(3):
                    row_kind = "result"
                else:
                    rev = int(m.group(2))
            con.execute(
                "INSERT OR IGNORE INTO delivered (name, kind, sent_on, build_id, rev)"
                " VALUES (?,?,?,?,?)", (pdf.stem, row_kind, sent_on, build_id, rev))
    con.execute("INSERT OR IGNORE INTO mailed (note_id)"
                " SELECT id FROM builds WHERE status='done'")
    db.set_meta(con, "delivered_seeded", "1")
    con.commit()


def record(con, name: str, kind: str, *, build_id: str | None = None,
           rev: int | None = None, sent_on: str | None = None) -> None:
    """One delivered document. Called only after Send-to-Kindle accepted
    it — a document that never reached the device must never be listed
    as safe to delete from it. Any delivery initialises the ledgers, so
    the backlog is seeded by whichever path runs first after a deploy."""
    ensure_ledgers(con)
    con.execute(
        "INSERT OR REPLACE INTO delivered (name, kind, sent_on, build_id, rev)"
        " VALUES (?,?,?,?,?)",
        (name, kind, sent_on or dt.date.today().isoformat(), build_id, rev))
    con.commit()


def dead_documents(con, today: dt.date) -> list[dict]:
    """Every delivered document that is finished with, by the rules in the
    module docstring, as {name, kind, reason, dead_on}."""
    # rowid is delivery order: INSERT OR REPLACE gives a resent name a fresh
    # one, and two editions on one day (morning, evening) share `sent_on`
    rows = con.execute("SELECT rowid AS seq, * FROM delivered").fetchall()
    status = {r["id"]: r["status"] for r in
              con.execute("SELECT id, status FROM builds")}
    latest_rev: dict[str, int] = {}
    for r in rows:
        if r["kind"] == "build" and r["build_id"] and r["rev"]:
            latest_rev[r["build_id"]] = max(latest_rev.get(r["build_id"], 0),
                                            r["rev"])
    editions = sorted((r for r in rows if r["kind"] in KINDS),
                      key=lambda r: r["seq"], reverse=True)
    newest = editions[0]["name"] if editions else None
    keep_research_after = (today - dt.timedelta(days=RESEARCH_KEEP_DAYS)).isoformat()

    out = []
    for r in rows:
        kind, reason = r["kind"], None
        if kind in KINDS:
            if r["name"] != newest:
                reason = "an earlier edition — its ink, if any, has been read"
        elif kind == "build":
            st = status.get(r["build_id"] or "")
            if st == "done":
                reason = "built — the result is in the edition that reported it"
            elif st == "dropped":
                reason = "dropped — nothing was executed"
            elif r["rev"] and r["rev"] < latest_rev.get(r["build_id"], 0):
                reason = f"superseded by r{latest_rev[r['build_id']]}"
        elif kind == "research":
            if r["sent_on"] <= keep_research_after:
                reason = (f"a brief older than {RESEARCH_KEEP_DAYS} days — "
                          f"still in the vault and over MCP")
        else:
            reason = _REASON.get(kind, "no longer needed on the device").format(
                brand=brand(), digest_dir=_tilde(get_config().digest_dir))
        if reason:
            out.append({"name": r["name"], "kind": kind, "reason": reason,
                        "dead_on": r["dead_on"]})
    return out


def gather_dead(con, today: dt.date) -> dict:
    """The delete list for one edition: named for a week, counted after."""
    cutoff = (today - dt.timedelta(days=DEAD_LIST_DAYS)).isoformat()
    dead = dead_documents(con, today)
    recent = sorted((d for d in dead if not d["dead_on"] or d["dead_on"] >= cutoff),
                    key=lambda d: (d["kind"], d["name"]))
    older = sum(1 for d in dead if d["dead_on"] and d["dead_on"] < cutoff)
    return {"recent": recent, "older": older,
            "new_names": [d["name"] for d in dead if not d["dead_on"]]}


def mark_dead(con, names: list[str], today: dt.date) -> None:
    con.executemany("UPDATE delivered SET dead_on=? WHERE name=? AND dead_on IS NULL",
                    [(today.isoformat(), n) for n in names])
    con.commit()


# ---------------------------------------------------------------------------
# gathering

def _results(con) -> list[dict]:
    """Executed Builds whose result has not travelled yet. The `mailed`
    ledger holds Build ids as note ids — a Build note's filename is its id."""
    from . import builds
    out = []
    for row in con.execute(
            "SELECT * FROM builds WHERE status='done'"
            " AND id NOT IN (SELECT note_id FROM mailed) ORDER BY updated, id"):
        path = Path(row["path"])
        if not path.exists():
            continue
        build = builds.load_build(path)
        actions = [a["text"] for a in con.execute(
            "SELECT text FROM actions WHERE note_id=? AND done=0", (row["id"],))]
        out.append({
            "id": row["id"], "title": row["title"], "topic": row["topic"],
            "model": row["model"] or "opus", "review_url": row["pr"],
            "report": builds._section(build["body"], "Result")
                      or "(the agent returned no report)",
            "history": builds.history_of(build), "actions": actions,
            "date": row["updated"] or dt.date.today().isoformat(),
        })
    return out


# A page queued longer than this without being read means the reader is
# stuck, not busy: the tick runs every five minutes and the daily cap is a
# day's worth, so anything older than this has been passed over repeatedly.
STALL_HOURS = 6


def gather_trouble(con, today: dt.date) -> dict | None:
    """Pages the pipeline has not managed to read, for the edition to say out
    loud. Silence here is what made the 2026-09-16 credit failure cost four
    days: the editions kept arriving (a section that needs no model call
    still fills one) while the reading half was dead, so the library looked healthy.

    Walled notebooks are counted but never named — invariant 2 reaches
    outbound documents, and a work notebook's name is employer content."""
    cfg = get_config()
    cutoff = dt.datetime.now().timestamp() - STALL_HOURS * 3600
    rows = con.execute(
        "SELECT notebook, status, error, COUNT(*) n, MIN(created_at) first"
        " FROM pages WHERE status='error' OR (status='new' AND created_at < ?)"
        " GROUP BY notebook, status, substr(COALESCE(error,''), 1, 60)"
        " ORDER BY first", (cutoff,)).fetchall()
    if not rows:
        return None
    items, walled = [], 0
    for r in rows:
        if str(r["notebook"]).lower().startswith(cfg.work_notebook_prefixes):
            walled += r["n"]
            continue
        items.append({
            "notebook": r["notebook"], "pages": r["n"],
            "stuck": r["status"] == "new",
            "since": dt.date.fromtimestamp(r["first"]).isoformat(),
            "why": _why(r["error"], r["status"]),
        })
    return {"items": items, "walled": walled,
            "total": sum(i["pages"] for i in items) + walled} if (items or walled) else None


_CREDIT = "credit balance"


def _why(error: str | None, status: str) -> str:
    """One readable line per failure, and the one thing that would fix it."""
    if status == "new":
        return ("queued but never read — the vision pass is not getting "
                "through; check the journal for ozu-tick")
    text = (error or "").strip()
    if _CREDIT in text:
        return ("the Anthropic API had no credit left — top up at "
                "console.anthropic.com, then: ozu requeue error")
    if "declined this page" in text:
        return "the model declined the page; look at the page itself"
    if "max_tokens" in text:
        return "the page was too dense to finish reading in one pass"
    return (text[:120] or "no reason recorded") + " — ozu requeue error"


def gather(con, kind: str, today: dt.date) -> dict | None:
    """Everything one edition carries. None when there is nothing worth a
    document — an edition with no ink, no result, no sheet, no trouble and no
    digest sends nothing (a delete list alone is not a reason to wake the
    device)."""
    from . import mail
    data = {
        "kind": kind, "date": today.isoformat(),
        "weekday": today.strftime("%A %-d %B %Y"),
        "stamp": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "mail": mail.gather(con), "results": _results(con),
        "learn": None, "digest": None, "wiki": None,
    }
    if kind == "evening":
        from . import learn
        data["learn"] = learn.gather(con, today)
        if today.weekday() == 6:                       # Sunday: the digest
            from . import digest
            data["digest"] = digest.gather()
            digest.run(data["digest"])                 # the Markdown copy
            from . import wiki                         # …and the wiki sheet
            data["wiki"] = wiki.gather_sheet(con, today)
    data["trouble"] = gather_trouble(con, today)
    if not any((data["mail"], data["results"], data["learn"],
                data["digest"], data["wiki"], data["trouble"])):
        return None
    data["dead"] = gather_dead(con, today)
    return data


# ---------------------------------------------------------------------------
# sending

def edition_name(kind: str, today: dt.date, now: dt.datetime | None = None) -> str:
    if kind == "extra":
        return f"{brand()} Extra {today.isoformat()} {(now or dt.datetime.now()):%H%M}"
    return f"{brand()} {kind.capitalize()} {today.isoformat()}"


def send(kind: str, today: dt.date | None = None, *, force: bool = False) -> bool:
    """Build and send one edition. True only when it reached the device —
    only then are the ledgers written, so a failed send carries the same
    items to the next edition instead of losing them. One morning and one
    evening per day: a second run is a no-op unless the first send failed
    (no row carries today's date) or `force` says otherwise."""
    if kind not in KINDS:
        raise ValueError(f"unknown edition {kind!r}; have {KINDS}")
    cfg = get_config()
    today = today or dt.date.today()
    con = db.connect(cfg.db_path)
    ensure_ledgers(con)
    if kind != "extra" and not force and con.execute(
            "SELECT 1 FROM delivered WHERE kind=? AND sent_on=? LIMIT 1",
            (kind, today.isoformat())).fetchone():
        log.info("the %s edition already went out on %s", kind, today)
        return False
    data = gather(con, kind, today)
    if data is None:
        log.info("nothing for a %s edition on %s; none sent", kind, today)
        return False
    name = edition_name(kind, today)
    cfg.editions_dir.mkdir(parents=True, exist_ok=True)
    pdf = cfg.editions_dir / f"{name}.pdf"
    digest_pdf.build_edition_pdf(data, pdf)
    if not send_kindle.send_pdf(pdf):
        log.info("%s built at %s but not sent; ledgers untouched", name, pdf)
        return False

    from . import learn, mail
    if data["mail"]:
        mail.mark_sent(con, data["mail"])
    con.executemany("INSERT OR IGNORE INTO mailed (note_id) VALUES (?)",
                    [(r["id"],) for r in data["results"]])
    if data["learn"]:
        learn.mark_sent(con, data["learn"], today.isoformat())
    if data.get("wiki"):
        from . import wiki
        wiki.mark_sheet_sent(con)
    con.commit()
    record(con, name, kind, sent_on=today.isoformat())
    mark_dead(con, data["dead"]["new_names"], today)
    log.info("%s sent: %d result(s), learn=%s, digest=%s, wiki=%s, "
             "%d document(s) newly listed as safe to delete", name,
             len(data["results"]), bool(data["learn"]), bool(data["digest"]),
             bool(data.get("wiki")),
             len(data["dead"]["new_names"]))
    return True
