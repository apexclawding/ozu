"""The morning brief: the first page of the Morning edition, built to answer
"what do I do today?" rather than "what did the system do overnight?".

The Morning used to open on a contents page and then report: what the ink
became, the code that moved, the spend, and a list of documents safe to delete.
None of it said what was due. The open actions — the one list the reader
wrote himself, in red — never appeared on paper at all, so an action whose
own text carried a date ("By Nov 19th: …") passed that date without a word.

The page, top to bottom, and the rule behind each part:

  Today's one thing   a ruled line to write on. Never a box: an outlined
                      rectangle is a checkbox to the annotation pass (D-04).
  Due                 open actions whose text names a date, overdue or within
                      DUE_AHEAD_DAYS, soonest first, each with how late or how
                      near. Dates are read from the text by rule (`due_date`),
                      no model call.
  Next                at most NEXT_MAX more, newest first: the newest are the
                      ones still in his head. The rest are a count and the date
                      of the oldest, never a list (a list that only grows is a
                      list nobody reads).
  Longest open        one action open more than STALE_DAYS, a different one each
                      day, with its age — so an old action surfaces now and
                      then instead of printing every morning or never.
  Waiting on you      Builds held for a decision: a proposal to approve or a
                      failed run to re-arm, the WAITING_MAX most recently moved
                      named and the rest counted.
  Still open in your notes
                      up to LATELY_MAX recent notebook notes that made no
                      action and read as unfinished (a question, or words like
                      "should" / "need"), quoted. A rule, not a model: dumber,
                      free, and it cannot invent.

Every action line prints the action's own text after a `☐`, exactly as the
mail does: a tick on it closes that action (invariant 5) and the age or due
clause goes on the faint line under it, never into the matched text.
"""
from __future__ import annotations

import datetime as dt
import re

DUE_AHEAD_DAYS = 14
NEXT_MAX = 5
STALE_DAYS = 30
LATELY_DAYS = 7
LATELY_MAX = 3
WAITING_MAX = 3

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
# A month is its full name or its exact abbreviation, never a prefix: "separate
# 3 boxes" must not read as 3 September.
_MON = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?")
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_MONTH_DAY = re.compile(rf"\b{_MON}\s+{_DAY}\b", re.I)          # Nov 19th
_DAY_MONTH = re.compile(rf"\b{_DAY}\s+(?:of\s+)?{_MON}", re.I)   # 19 November
_ISO = re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})\b")

_OPEN_WORDS = re.compile(
    r"\?|\b(should|need to|needs to|must|want to|maybe|consider|decide|"
    r"figure out|not sure|todo|to do|idea)\b", re.I)


def due_date(text: str, written: dt.date) -> dt.date | None:
    """The date an action's own text names, or None. A date without a year is
    the first such day on or after the day it was written: "April 15th"
    written in October is next April. A month alone ("next July") names no
    day and gives None — guessing a day would print a false deadline."""
    m = _ISO.search(text)
    if m:
        try:
            return dt.date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    m = _MONTH_DAY.search(text)
    if m:
        mon, day = m[1], m[2]
    else:
        m = _DAY_MONTH.search(text)
        if not m:
            return None
        day, mon = m[1], m[2]
    month = _MONTHS[mon[:3].lower()]
    for year in (written.year, written.year + 1):
        try:
            d = dt.date(year, month, int(day))
        except ValueError:
            return None
        if d >= written:
            return d
    return None


def when(due: dt.date, today: dt.date) -> str:
    n = (due - today).days
    if n < 0:
        return f"{-n} day{'' if n == -1 else 's'} late"
    if n == 0:
        return "due today"
    if n == 1:
        return "due tomorrow"
    return f"due {due.strftime('%a %-d %b')} · in {n} days"


def _age(days: int) -> str:
    return "opened today" if days <= 0 else f"open {days} day{'' if days == 1 else 's'}"


def _actions(con, today: dt.date) -> list[dict]:
    rows = con.execute(
        "SELECT a.id, a.text, a.created, n.title, n.topic FROM actions a"
        " JOIN notes n ON n.id = a.note_id"
        " WHERE a.done = 0 AND n.vault = 'personal'"
        " ORDER BY a.created DESC, a.id DESC").fetchall()
    out = []
    for r in rows:
        written = dt.datetime.fromtimestamp(r["created"]).date()
        out.append({"id": r["id"], "text": r["text"], "topic": r["topic"],
                    "written": written, "age": (today - written).days,
                    "due": due_date(r["text"], written)})
    return out


def _waiting(con, today: dt.date) -> list[dict]:
    rows = con.execute(
        "SELECT id, title, status, rev, updated FROM builds"
        " WHERE status IN ('proposed', 'revised', 'error')"
        " ORDER BY updated DESC, id").fetchall()
    out = []
    for r in rows:
        # Directives act only on the Build's own stamped pages (11c(e)), so
        # the line points there: ink beside it on this page would do nothing.
        what = ("stopped · @input on its proposal re-arms it" if r["status"] == "error"
                else f"r{r['rev']} waiting for @approved on its proposal")
        out.append({"id": r["id"], "title": r["title"], "what": what})
    return out


def _lately(con, today: dt.date) -> list[dict]:
    since = (today - dt.timedelta(days=LATELY_DAYS)).isoformat()
    rows = con.execute(
        "SELECT n.id, n.title, n.summary, n.created FROM notes n"
        " JOIN pages p ON p.id = n.source_page AND p.kind = 'ink'"
        " WHERE n.vault = 'personal' AND n.created >= ?"
        " AND n.id NOT IN (SELECT note_id FROM actions)"
        " AND (','||n.tags||',') NOT LIKE '%,build,%'"
        " ORDER BY n.created DESC, n.id DESC", (since,)).fetchall()
    out = [{"title": r["title"], "summary": r["summary"], "created": r["created"]}
           for r in rows if _OPEN_WORDS.search(f"{r['title']} {r['summary']}")]
    return out[:LATELY_MAX]


def gather(con, today: dt.date) -> dict | None:
    """The brief's rows, or None when there is nothing to put on it."""
    actions = _actions(con, today)
    horizon = today + dt.timedelta(days=DUE_AHEAD_DAYS)
    due = sorted((a for a in actions if a["due"] and a["due"] <= horizon),
                 key=lambda a: a["due"])
    shown = {a["id"] for a in due}
    rest = [a for a in actions if a["id"] not in shown]
    nxt = rest[:NEXT_MAX]
    shown |= {a["id"] for a in nxt}
    stale = [a for a in rest if a["id"] not in shown and a["age"] > STALE_DAYS]
    longest = stale[today.toordinal() % len(stale)] if stale else None
    if longest:
        shown.add(longest["id"])
    more = [a for a in actions if a["id"] not in shown]
    data = {
        "due": due, "next": nxt, "longest": longest,
        "more": len(more),
        "oldest": min((a["written"] for a in more), default=None),
        "waiting": _waiting(con, today), "lately": _lately(con, today),
        "today": today,
    }
    if not (actions or data["waiting"] or data["lately"]):
        return None
    return data


def body(doc, data: dict) -> None:
    """Print the brief into a layout.Document."""
    today = data["today"]
    doc.heading("Today's one thing")
    doc.rules(1)

    if data["due"]:
        doc.heading("Due")
        for a in data["due"]:
            doc.action(a["text"], source=f"{when(a['due'], today)} · {a['topic']}")

    if data["next"] or data["longest"]:
        doc.heading("Next")
        for a in data["next"]:
            doc.action(a["text"], source=f"{a['topic']} · {_age(a['age'])}")
        if data["longest"]:
            a = data["longest"]
            doc.action(a["text"], source=f"longest open: {a['topic']} · "
                                         f"written {a['written'].strftime('%-d %b')}, "
                                         f"{_age(a['age'])}")
        if data["more"]:
            doc.para(f"+ {data['more']} more open, the oldest from "
                     f"{data['oldest'].strftime('%-d %b')}", cls="faint")

    if data["waiting"]:
        doc.heading("Waiting on you")
        for b in data["waiting"][:WAITING_MAX]:
            doc.note(f"{b['id']} · {b['title']}", b["what"])
        older = len(data["waiting"]) - WAITING_MAX
        if older > 0:
            doc.para(f"+ {older} older Build{'' if older == 1 else 's'} waiting",
                     cls="faint")

    if data["lately"]:
        doc.heading("Still open in your notes")
        for n in data["lately"]:
            doc.note(n["title"], n["summary"])

    if not (data["due"] or data["next"] or data["waiting"] or data["lately"]):
        doc.zero("Nothing due, nothing waiting.")


def line(data: dict) -> str:
    """The cover's count line for the brief."""
    bits = []
    late = sum(1 for a in data["due"] if a["due"] < data["today"])
    if late:
        bits.append(f"{late} overdue")
    if len(data["due"]) - late:
        bits.append(f"{len(data['due']) - late} due soon")
    if data["waiting"]:
        bits.append(f"{len(data['waiting'])} waiting on you")
    return " · ".join(bits)
