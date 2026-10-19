"""Typeset the documents the Scribe receives: the two daily editions
(Morning, Evening), a Build proposal, an @research brief, and — as
sections inside an edition or as standalone documents for local preview —
the mail, a Build result, the learning sheet and the weekly digest.

Layout lives in `layout.py` — this module is only the content of each document,
in the order it should be read. Nothing here touches fonts, geometry or colour;
if a change is about how the page *looks*, it belongs next door.

Every document opens with a **cover** (D-06). It is a label, not content:
the day, what kind of document this is, and one line of what is inside. That
is a library problem, not a reading one — on the Scribe's home screen a Ozu
document was a grey thumbnail under a truncated filename, indistinguishable
from every other Ozu document, so the only way to tell yesterday's mail from
last week's digest was to open both. Nothing on a cover appears nowhere else,
so skipping it loses nothing.

Each body is a `_*_body(doc, data)` function, and the standalone builders and
the edition composer both call them — so the section of an edition and the
document it replaced can never drift apart on content, and
`tools/render_sample.py` keeps rendering every kind on its own.

Annotations made on these documents ARE read back: a re-exported "<brand> "
document queues as annotation pages, and the annotation pass turns the ink into
closes, actions and notes. That is why the printed `☐` in front of every action
matters — it is the marker a tick is matched against — and why the last-page
footer invites exactly that.
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from .config import brand, canonical_topic, get_config
from .indexer import _parse_frontmatter
from .layout import AUTHORS, Document

log = logging.getLogger("ozu.digest_pdf")


INVITE = ("Annotate in ink and re-export to Drive: a tick closes its action, "
          "red ink creates one, margin notes join the vault.")


# The learning sheet prints no action boxes, so its footer does not mention
# ticks; it names the two gestures that do work on it.
LEARN_INVITE = ("Annotate in ink and re-export to Drive: red ink creates an "
                "action, margin notes join the vault, @research beside a "
                "technique queues a brief.")



def build_research_pdf(*, title: str, source_id: str, topic: str,
                       report: str, out_path: Path, date=None) -> Path:
    """Typeset one @research brief for the Scribe."""
    doc = Document()
    doc.cover("research", date=date or dt.date.today(), kicker=brand(),
              wordmark="RESEARCH BRIEF", subtitle=title,
              line=f"from your ink · topic {topic}")
    doc.head(f"{brand()} research brief", title, f"from note {source_id} · {topic}")
    doc.markdown(report)
    doc.footer(INVITE)
    doc.render(out_path, author=AUTHORS["research"])
    log.info("research PDF written to %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# Builds

# The Build documents' footer says how to answer — the decision box above it
# says the exact words; this reminds where the answer goes.
BUILD_INVITE = ("Write your decision anywhere on this document in ink and "
                "re-export it to Drive — the printed Build id on every page "
                "is what routes your answer back to this Build.")


def build_propose_pdf(data: dict, out_path: Path) -> Path:
    """One Build proposal (or revision) for the Scribe. The decision box
    prints the exact words to write; none of them sits on a `☐` line —
    directives are not actions, and a `☐` is reserved for things a tick can
    close (invariant 5). The Build id is stamped into the text layer of
    every page (`page_header`) so any annotated page routes home."""
    doc = Document()
    doc.cover("build", date=data.get("date"),
              kicker=f"{brand()} Build · {data['topic']}", wordmark="BUILD",
              subtitle=data["title"],
              # The exact words to write are printed in the decision box on
              # the body page and nowhere else. Printing `@approved` here too
              # would put a directive tag on the emptiest, most-written-on
              # page in the document, and printed-vs-handwritten is the first
              # distinction a vision read collapses (invariant 5b).
              line=f"revision {data['rev']} · your decision goes on paper")
    doc.head(f"{brand()} Build {data['id']} · r{data['rev']} · {data['topic']}",
             data["title"], f"proposal — decide on paper · {data['date']}")
    doc.markdown(data["proposal"])
    if data.get("history"):
        doc.heading("Decision history")
        doc.markdown(data["history"])
    doc.heading("Your decision — write one of these on this page, in ink")
    doc.note("@approved", "build it as proposed. Add @opus, @sonnet or "
             "@fable to choose the builder; no tag means opus.")
    doc.note("@input …your answers or changes…",
             "I fold them into the requirement and plan and send you r"
             f"{data['rev'] + 1}. Loop as often as you want.")
    doc.note("@drop", "close this Build; nothing is executed.")
    doc.footer(BUILD_INVITE)
    doc.render(out_path, page_header=f"{brand()} Build {data['id']} r{data['rev']}",
               author=AUTHORS["build"])
    log.info("build proposal PDF written to %s", out_path)
    return out_path


def result_header(build_id: str) -> str:
    """The running header of a Build result's pages — what `builds.header_re()`
    matches in the text layer, on whichever page of an edition the result
    lands. Kept in one place so the edition and the standalone document
    stamp the same words."""
    return f"{brand()} Build {build_id} result"


def _result_body(doc: Document, data: dict) -> None:
    """The execution report back to the device the approval was written on:
    what was built, the PR to review (a real `☐` action line — ticking it
    on this very page closes the action), and the decision trail."""
    if data.get("review_url"):
        doc.para(data["review_url"], cls="url")
    doc.markdown(data["report"])
    if data.get("actions"):
        doc.heading("For you")
        for a in data["actions"]:
            doc.action(a)
    if data.get("history"):
        doc.heading("Decision history")
        doc.markdown(data["history"])


def _result_head(doc: Document, data: dict, outline: str = "") -> None:
    doc.head(f"{brand()} Build {data['id']} · result · {data['topic']}",
             data["title"], f"built with {data['model']} · {data['date']}",
             outline=outline)


def build_result_pdf(data: dict, out_path: Path) -> Path:
    """A Build result as a document of its own (local preview and the
    fixture corpus; on the device it is a section of the next edition)."""
    doc = Document()
    doc.cover("result", date=data.get("date"),
              kicker=f"{brand()} Build · {data['topic']}", wordmark="BUILD RESULT",
              subtitle=data["title"],
              line=f"built with {data['model']} · review before merging")
    _result_head(doc, data)
    _result_body(doc, data)
    doc.footer(INVITE)
    doc.render(out_path, page_header=result_header(data["id"]),
               author=AUTHORS["build"])
    log.info("build result PDF written to %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# the mail: what your ink became

# When the ink and the reader disagree (ink.reconcile), nothing is closed and
# the line comes back open with the reason. Neither sentence asks for anything
# new: a tick on the reprinted line is an ordinary tick.
RECHECK = {
    "unseen": "read as ticked, but no tick was found beside it — not closed; "
              "tick it again to close it",
    "unread": "a tick was found beside it, but it was not read — not closed; "
              "tick it again to close it",
}


def _mail_body(doc: Document, data: dict) -> None:
    """Everything the latest runs produced, back to the device the ink came
    from — what the ink became, and anything that needs the owner's eyes."""
    cfg = get_config()
    # Receipts come first: the one thing a ✓ writer needs at a glance is
    # whether it took — and what is still open when it did not.
    if data.get("receipts"):
        doc.heading("Your checkmarks")
        for r in data["receipts"]:
            if r["closed"]:
                doc.action(r["closed"], done=True, source=f"your ink: {r['line']}")
            elif r.get("kind") in RECHECK:
                # the ink check and the reader disagreed; the line prints
                # open, so ticking it again closes it the ordinary way
                doc.action(r["line"])
                doc.para(RECHECK[r["kind"]], cls="faint")
            else:
                doc.action(r["line"])
                doc.para("matched no open action" +
                         (" — closest still open:" if r["candidates"]
                          else " — and no open action looks close"),
                         cls="faint")
                for c in r["candidates"]:
                    doc.action(c)

    # Build outcomes that have no section of their own: a drop confirmed, a
    # second @approved refused, an unmatched page, a failed run. Proposals
    # travel as their own "<brand> Build" PDFs; results are sections of the
    # edition this rides in, and are only ever summarised here.
    if data.get("builds"):
        doc.heading("Builds")
        for line in data["builds"]:
            doc.para(line)

    if data.get("work"):
        doc.heading("Built (@claude) · review before merging")
        for item in data["work"]:
            doc.note(item["title"], item["summary"])
            doc.markdown(item.get("body") or "")

    if data.get("plain"):
        doc.heading("Read from your ink")
        for item in data["plain"]:
            doc.note(item["title"], item["summary"], label=item["topic"])
            for a in item.get("actions", []):
                doc.action(a)
            if item.get("image"):
                doc.image(cfg.vault_dir / str(item["image"]))
                doc.para((item.get("body") or "")[:500], cls="summary")

    if data.get("research"):
        doc.heading("Research briefs · sent as their own documents")
        for item in data["research"]:
            doc.para(item["title"])


def _mail_line(data: dict) -> str:
    counts = [(len(data.get("receipts") or []), "checkmark"),
              (len(data.get("plain") or []), "page read"),
              (len(data.get("work") or []), "build"),
              (len(data.get("research") or []), "brief")]
    return (" · ".join(f"{n} {word}{'' if n == 1 else 's'}"
                       for n, word in counts if n)
            or "nothing new since the last one")


def build_mail_pdf(data: dict, out_path: Path) -> Path:
    """The mail as a document of its own (local preview and the fixture
    corpus; on the device it is the first section of every edition)."""
    doc = Document()
    doc.cover("mail", date=data["stamp"], kicker=brand(), wordmark="MAIL",
              subtitle="what your ink became", line=_mail_line(data))
    doc.head(f"{brand()} mail", "What your ink became", data["stamp"])
    _mail_body(doc, data)
    doc.footer(INVITE)
    doc.render(out_path, author=AUTHORS["mail"])
    log.info("mail PDF written to %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# the learning sheet

def _learn_body(doc: Document, data: dict) -> None:
    """What was learned from the videos pinned since the last sheet, what is
    due to come back today, and what could not be read. Every item carries
    its note id so a margin note that quotes it lands connected; ruled lines
    under each question are the writing surface. No `☐` anywhere — nothing
    here is an action unless the owner writes one in red (invariant 5)."""
    if data.get("new"):
        doc.heading("New")
        for item in data["new"]:
            meta = " · ".join(x for x in (
                item.get("channel"), item.get("duration"),
                f"pinned {item['pinned']}" if item.get("pinned") else "") if x)
            doc.note(item["title"], meta)
            doc.para(f"note: {item['id']}", cls="faint")
            doc.markdown(item.get("brief") or "")
            doc.subheading("In your own words")
            doc.rules(2)

    if data.get("due"):
        doc.heading("Due again")
        for d in data["due"]:
            doc.note(d["title"], f"learned {d['learned']} · {d['nth']} time")
            doc.para(f"note: {d['id']}", cls="faint")
            if d.get("cue"):
                doc.markdown(f"Cue: {d['cue']}")
            if d.get("question"):
                doc.markdown(f"Question: {d['question']}")
            doc.rules(2)

    if data.get("skipped"):
        doc.heading("Couldn't read")
        for s_ in data["skipped"]:
            doc.markdown(f"- {s_['title']} — {s_['reason']}")



WIKI_INTRO = (
    "What the wiki learned since last Sunday. Write beside a claim to correct "
    "it; strike a line through a claim to drop it. Your ink outranks every "
    "brief the page was compiled from — next cycle reads it and edits the page.")

WIKI_INVITE = ("Annotate in ink and re-export to Drive: margin notes correct "
               "the wiki, a struck line drops a claim, red ink creates an "
               "action.")


def _wiki_body(doc: Document, data: dict) -> None:
    """The Sunday wiki sheet. Every page prints its id so a correction lands
    on the right page; claims print without their citation runs (those stay
    in the vault). No `☐` anywhere — a correction is not an action."""
    doc.para(WIKI_INTRO, cls="summary")
    for p in data.get("pages") or []:
        doc.note(p["title"], ("new page" if p["new"] else "updated") + f" · {p['updated']}")
        doc.para(f"page: {p['id']}", cls="faint")
        if p.get("description"):
            doc.markdown(p["description"])
        if p.get("changes"):
            doc.subheading("This week")
            doc.markdown("\n".join(f"- {c}" for c in p["changes"]))
        if p.get("claims"):
            doc.subheading("What I know")
            doc.markdown("\n".join(p["claims"]))
        doc.rules(1)
    if data.get("more"):
        n = data["more"]
        doc.para(f"and {n} more page{'' if n == 1 else 's'} changed — "
                 f"all in topics/wiki/", cls="faint")
    if data.get("errors"):
        n = data["errors"]
        doc.para(f"{n} note{'' if n == 1 else 's'} could not be folded in — "
                 f"ozu wiki requeue", cls="faint")


def _wiki_line(data: dict) -> str:
    pages = data.get("pages") or []
    new = sum(1 for p in pages if p["new"])
    bits = [f"{new} new" if new else "",
            f"{len(pages) - new} updated" if len(pages) - new else ""]
    return " · ".join(b for b in bits if b) or "nothing changed"


def _learn_line(data: dict) -> str:
    counts = [f"{len(data['new'])} new" if data.get("new") else "",
              f"{len(data['due'])} due again" if data.get("due") else ""]
    return (" · ".join(c for c in counts if c)
            or "nothing new — the shelf is empty tonight")



def build_learn_pdf(data: dict, out_path: Path) -> Path:
    """The "<brand> Learn" sheet as a document of its own (local preview; the
    evening edition is how it reaches the device)."""
    doc = Document()
    doc.cover("learn", date=data.get("date"), kicker=brand(), wordmark="LEARN",
              subtitle="what the videos taught, back for a second pass",
              line=_learn_line(data))
    doc.head("", f"{brand()} Learn", data["weekday"])
    _learn_body(doc, data)
    doc.footer(LEARN_INVITE)
    doc.render(out_path, author=AUTHORS["learn"])
    log.info("learn sheet written to %s", out_path)
    return out_path



# ---------------------------------------------------------------------------
# the weekly digest

# The digest lists new notes, but a busy week can produce far more than fit.
# Cap the list — and always say what the cap hid, so "New notes (40)" followed
# by twelve rows is never something the reader has to notice for themselves.
NEW_NOTE_LIMIT = 12


def _digest_body(doc: Document, data: dict) -> None:
    cfg = get_config()
    if data["new"]:
        doc.heading(f"New notes ({len(data['new'])})")
        for r in data["new"][:NEW_NOTE_LIMIT]:
            doc.note(r["title"], r["summary"], label=r["topic"])
        hidden = len(data["new"]) - NEW_NOTE_LIMIT
        if hidden > 0:
            doc.para(f"+ {hidden} more this week — all of them are in the vault.",
                     cls="faint")

    if data["links"]:
        doc.heading("New connections")
        for e in data["links"]:
            reason = f"  ({e['reason']})" if e.get("reason") else ""
            doc.para(f"{e['src']}  ←→  {e['dst']}{reason}")

    if data["actions"]:
        doc.heading("Open actions · your red ink")
        for a in data["actions"]:
            doc.action(a["text"], source=f"from: {a['title']}")

    if data["built"]:
        doc.heading("Claude built (@claude)")
        for b in data["built"]:
            doc.note(b["title"], b["summary"])

    if data["queued"]:
        doc.heading("@claude queued")
        for q in data["queued"]:
            where = ("runs tonight" if canonical_topic(q["topic"]) in data["repos"]
                     else f"waiting — no repo mapped for '{q['topic']}'")
            doc.para(f"{q['title']}  ({where})")

    if data["failed"]:
        doc.heading("@claude failed")
        for f_ in data["failed"]:
            doc.para(f_["title"], cls="note")
        doc.para("see: journalctl --user -u ozu-nightly", cls="faint")

    if data["r_built"]:
        doc.heading("Research briefs (@research)")
        for b in data["r_built"]:
            doc.note(b["title"], b["summary"])

    if data["r_queued"]:
        doc.heading("@research queued")
        for qn in data["r_queued"]:
            doc.para(qn["title"])

    if data["r_failed"]:
        doc.heading("@research failed")
        for f_ in data["r_failed"]:
            doc.para(f_["title"], cls="note")

    if data.get("learned") or data.get("coming_back"):
        n, m = data["learned"], data["coming_back"]
        doc.heading("Learned this week")
        doc.para(f"{n} video{'' if n == 1 else 's'} · {m} coming back next week")

    if data.get("l_failed"):
        doc.heading("@learn failed")
        for f_ in data["l_failed"]:
            doc.para(f_["title"], cls="note")

    if data["review"]:
        doc.heading("Needs review · check these against your ink")
        for r in data["review"]:
            fm, body = {}, ""
            path = Path(r["path"])
            if path.exists():
                fm, body = _parse_frontmatter(path.read_text())
            doc.note(r["title"])
            doc.para(f"note: {r['id']}", cls="faint")
            if fm.get("image"):
                doc.image(cfg.vault_dir / str(fm["image"]))
            if body:
                doc.para(body.strip()[:600], cls="summary")

    if data["gem"]:
        g = data["gem"]
        doc.heading("Forgotten gem")
        doc.note(g["title"], f"{g['summary']}  (from {g['created']})")

    if not data["new"] and not data["actions"]:
        doc.zero("Quiet week · the Scribe misses you.")


def _digest_line(data: dict) -> str:
    counts = [(len(data["new"]), "new note"),
              (len(data["actions"]), "open action"),
              (len(data["links"]), "new connection")]
    return (" · ".join(f"{n} {word}{'' if n == 1 else 's'}"
                       for n, word in counts if n)
            or "a quiet week — nothing new in the vault")


def build_pdf(data: dict, out_path: Path) -> Path:
    """The weekly digest as a document of its own (local preview and the
    fixture corpus; on the device it is a section of Sunday's evening)."""
    doc = Document()
    doc.cover("digest", date=data["date"], kicker=brand(),
              wordmark="WEEKLY DIGEST", subtitle="the week the vault had",
              line=_digest_line(data))
    # No kicker: the digest's title *is* its identity, which is how the old
    # renderer had it. Wording is Stage 3's job, not this one's.
    doc.head("", f"{brand()} weekly digest", data["date"])
    _digest_body(doc, data)
    doc.footer(INVITE)
    doc.render(out_path, author=AUTHORS["digest"])
    log.info("digest PDF written to %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# the editions


EDITION_WORDMARK = {"morning": "MORNING", "evening": "EVENING", "extra": "EXTRA"}
EDITION_SUBTITLE = {
    "morning": "what your ink became overnight",
    "evening": "what your ink became · what the videos taught",
    "extra": "what your ink became, out of cycle",
}

# The delete list is the one section that is about the device rather than the
# vault. Its wording is the whole feature: a name the reader can find in the
# library, and the reason it is dead, in five words.
DELETE_INTRO = ("These documents on the Scribe are finished with — every one "
                "is in the vault, a superseded revision, or an edition whose "
                "ink has been read. Delete them from the device when you "
                "like; export any page you have written on first.")
DELETE_OLDER = ("and {n} older one{s} listed before — the monthly sweep on "
                "Manage Your Content and Devices catches those.")
DELETE_LIST_MAX = 60


def _edition_sections(data: dict) -> list[dict]:
    """The sections of an edition, in reading order, as (title, filler)
    pairs. Computed once so the contents page and the body agree."""
    out: list[dict] = []
    mail = data.get("mail")
    if mail:
        out.append({"title": "What your ink became", "kind": "mail",
                    "header": "", "data": mail, "line": _mail_line(mail)})
    for r in data.get("results") or []:
        out.append({"title": f"Build result · {r['title']}", "kind": "result",
                    "header": result_header(r["id"]), "data": r,
                    "line": f"built with {r['model']} · review before merging"})
    if data.get("learn"):
        out.append({"title": f"{brand()} Learn", "kind": "learn", "header": "",
                    "data": data["learn"], "line": _learn_line(data["learn"])})
    if data.get("wiki"):
        out.append({"title": "Wiki this week", "kind": "wiki", "header": "",
                    "data": data["wiki"], "line": _wiki_line(data["wiki"])})
    if data.get("digest"):
        out.append({"title": "Weekly digest", "kind": "digest", "header": "",
                    "data": data["digest"],
                    "line": _digest_line(data["digest"])})
    trouble = data.get("trouble")
    if trouble:
        n = trouble["total"]
        out.append({"title": "Pages I could not read", "kind": "trouble",
                    "header": "", "data": trouble,
                    "line": f"{n} page{'' if n == 1 else 's'} waiting"})
    dead = data.get("dead") or {}
    if dead.get("recent") or dead.get("older"):
        n = len(dead.get("recent") or [])
        out.append({"title": "Safe to delete from the Scribe", "kind": "dead",
                    "header": "", "data": dead,
                    "line": (f"{n} document{'' if n == 1 else 's'}"
                             if n else "nothing new")})
    return out


def _edition_line(sections: list[dict]) -> str:
    """The cover's one line: counts, not titles — a list of section titles
    overran the 120-character cap on the first render and was cut mid-word."""
    bits: list[str] = []
    results = sum(1 for s in sections if s["kind"] == "result")
    for s in sections:
        if s["kind"] == "mail":
            bits.append(s["line"])
        elif s["kind"] == "learn":
            bits.append("learn sheet")
        elif s["kind"] == "digest":
            bits.append("weekly digest")
        elif s["kind"] == "wiki":
            bits.append("wiki sheet")
        elif s["kind"] == "trouble":
            bits.append(f"{s['data']['total']} page(s) unread")
        elif s["kind"] == "dead":
            n = len(s["data"].get("recent") or [])
            bits.append(f"{n} to delete" if n else "")
    if results:
        bits.insert(1 if sections and sections[0]["kind"] == "mail" else 0,
                    f"{results} build result{'' if results == 1 else 's'}")
    return " · ".join(b for b in bits if b) or "nothing since the last edition"


TROUBLE_INTRO = (
    "Ink you wrote that has not become anything yet. Nothing is lost — every "
    "page below is still on the device and still in the pipeline's own queue — "
    "but none of it has been read, so no action closed and no Build moved.")


def _trouble_body(doc: Document, trouble: dict) -> None:
    """Said plainly and early, because the failure this section exists for is
    a silent one: the editions kept arriving while the reading half was dead."""
    doc.para(TROUBLE_INTRO, cls="summary")
    for item in trouble["items"]:
        doc.note(item["notebook"],
                 f"{item['pages']} page{'' if item['pages'] == 1 else 's'} · "
                 f"since {item['since']}")
        doc.para(item["why"], cls="faint")
    if trouble["walled"]:
        n = trouble["walled"]
        doc.para(f"and {n} page{'' if n == 1 else 's'} from a walled notebook, "
                 f"not named here", cls="faint")


def _dead_body(doc: Document, dead: dict) -> None:
    doc.para(DELETE_INTRO, cls="summary")
    recent = dead.get("recent") or []
    for row in recent[:DELETE_LIST_MAX]:
        doc.para(f"•  {row['name']}", cls="bullet")
        if row.get("reason"):
            doc.para(row["reason"], cls="faint")
    if len(recent) > DELETE_LIST_MAX:
        doc.para(f"+ {len(recent) - DELETE_LIST_MAX} more", cls="faint")
    if dead.get("older"):
        n = dead["older"]
        doc.para(DELETE_OLDER.format(n=n, s="" if n == 1 else "s"),
                 cls="faint")


_BODY = {
    "mail": _mail_body, "result": _result_body, "digest": _digest_body,
    "learn": _learn_body, "dead": _dead_body, "trouble": _trouble_body,
    "wiki": _wiki_body,
}
_FOOTER = {"learn": LEARN_INVITE, "wiki": WIKI_INVITE}


def build_edition_pdf(data: dict, out_path: Path) -> Path:
    """One edition: a cover, a contents page, then one section per part —
    each on a fresh page with its own header band, and a Build result's
    pages stamped with that Build's id (D-07) so its ink routes home from
    inside a document whose filename names no Build.

    The contents page is a printed list; the same entries go into the PDF
    outline, which is what the device's "Go to" uses. Nothing here is a
    `☐` unless the section it belongs to would print one on its own."""
    kind = data["kind"]
    sections = _edition_sections(data)
    doc = Document()
    doc.cover(kind if kind in ("morning", "evening") else "mail",
              date=data["date"], kicker=brand(),
              wordmark=EDITION_WORDMARK[kind],
              subtitle=EDITION_SUBTITLE[kind],
              line=_edition_line(sections))
    doc.head(f"{brand()} {kind}", f"{brand()} {kind.capitalize()}",
             f"{data['weekday']} · {data['stamp']}")
    doc.heading("In this edition")
    for s in sections:
        doc.para(f"•  {s['title']}", cls="bullet")
        if s.get("line"):
            doc.para(s["line"], cls="faint")
    if not sections:
        doc.zero("Nothing since the last edition.")

    for s in sections:
        doc.section(page_header=s["header"])
        if s["kind"] == "mail":
            doc.head(f"{brand()} mail", "What your ink became", data["stamp"],
                     outline=s["title"])
        elif s["kind"] == "result":
            _result_head(doc, s["data"], outline=s["title"])
        elif s["kind"] == "learn":
            doc.head("", f"{brand()} Learn", s["data"]["weekday"], outline=s["title"])
        elif s["kind"] == "digest":
            doc.head("", f"{brand()} weekly digest", s["data"]["date"],
                     outline=s["title"])
        elif s["kind"] == "wiki":
            doc.head("", "Wiki this week", s["data"]["date"],
                     outline=s["title"])
        else:
            doc.head(f"{brand()} {kind}", s["title"], data["weekday"])
        _BODY[s["kind"]](doc, s["data"])
    # one footer, on the last page, worded for the last section on it
    last = sections[-1]["kind"] if sections else "mail"
    doc.footer(_FOOTER.get(last, INVITE))
    doc.render(out_path, author=AUTHORS["edition"])
    log.info("%s edition written to %s (%d sections)", kind, out_path,
             len(sections))
    return out_path
