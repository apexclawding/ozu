"""The morning brief (brief.py): due dates read from an action's own text, the
next actions newest first, the oldest surfacing one a day, Builds waiting on a
decision, and recent notes that read as unfinished."""
from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_pipeline import env  # noqa: F401,E402 — shared fixture

TODAY = dt.date(2026, 11, 27)
WROTE = dt.date(2026, 10, 2)


def test_a_date_in_the_text_is_read_and_a_year_inferred():
    from ozu.brief import due_date
    assert due_date("By Nov 19th: choose a name", WROTE) == dt.date(2026, 11, 19)
    assert due_date("April 15th: letter to the bishop", WROTE) == dt.date(2027, 4, 15)
    assert due_date("send it 3 December", WROTE) == dt.date(2026, 12, 3)
    assert due_date("renew on 2026-12-01", WROTE) == dt.date(2026, 12, 1)
    assert due_date("on 2 sept. call", WROTE) == dt.date(2027, 9, 2)


def test_no_day_means_no_deadline():
    from ozu.brief import due_date
    for text in ("break the lease next July", "separate 3 boxes",
                 "confirm the fee by May", "Read the report of B-260908-4e6d",
                 "the #3 in the email", "Feb 30 is not a day"):
        assert due_date(text, WROTE) is None, text


def test_when_reads_like_a_person():
    from ozu.brief import when
    assert when(dt.date(2026, 11, 19), TODAY) == "8 days late"
    assert when(dt.date(2026, 11, 26), TODAY) == "1 day late"
    assert when(TODAY, TODAY) == "due today"
    assert when(dt.date(2026, 11, 28), TODAY) == "due tomorrow"
    assert when(dt.date(2026, 12, 3), TODAY) == "due Thu 3 Dec · in 6 days"


def _note(con, note_id, *, created, title="t", summary="s", tags=(), page=None, vault="personal"):
    from ozu import db
    db.upsert_note(con, note_id=note_id, vault=vault, path=f"{note_id}.md", title=title,
                   topic="ideas", summary=summary, tags=list(tags), created=created,
                   body="b", needs_review=False, source_page=page)


def _action(con, note_id, text, written: dt.date):
    con.execute("INSERT INTO actions (note_id, text, created) VALUES (?,?,?)",
                (note_id, text, time.mktime(written.timetuple()) + 3600))


def test_gather_ranks_due_then_newest_and_surfaces_one_old_action(env):
    from ozu import brief, db
    con = db.connect(env.db_path)
    _note(con, "n1", created="2026-08-01")
    _note(con, "walled", created="2026-11-20", vault="work")
    _action(con, "n1", "By Nov 19th: choose a name", WROTE)
    _action(con, "n1", "Due 3 Jan: file the form", WROTE)          # beyond 14 days
    for i in range(8):
        _action(con, "n1", f"recent thing {i}", TODAY - dt.timedelta(days=i))
    _action(con, "n1", "fix the pool cover", dt.date(2026, 8, 24))
    _action(con, "n1", "sand the balcony", dt.date(2026, 8, 25))
    _action(con, "walled", "a work thing", TODAY)                  # never on paper
    con.commit()

    data = brief.gather(con, TODAY)
    assert [a["text"] for a in data["due"]] == ["By Nov 19th: choose a name"]
    assert [a["text"] for a in data["next"]] == [f"recent thing {i}" for i in range(5)]
    assert data["longest"]["text"] in {"fix the pool cover", "sand the balcony",
                                       "Due 3 Jan: file the form"}
    shown = 1 + 5 + 1
    assert data["more"] == 12 - shown
    assert all("work" not in a["text"] for a in data["next"] + data["due"])

    # a different old action on a different day
    picks = {brief.gather(con, TODAY + dt.timedelta(days=d))["longest"]["text"]
             for d in range(3)}
    assert len(picks) > 1


def test_waiting_builds_and_unfinished_notes(env):
    from ozu import brief, db
    con = db.connect(env.db_path)
    for bid, status in (("B-1", "proposed"), ("B-2", "error"), ("B-3", "done")):
        db.upsert_build(con, build_id=bid, path="p", topic="t", title=f"title {bid}",
                        status=status, rev=2, model=None, branch=None, pr=None,
                        pending="", source_note=None, updated="2026-11-26")
    page = db.enqueue_page(con, "sha", "x.pdf", "Inbox", 1, "i.png")
    back = db.enqueue_page(con, "sha2", "y.pdf", "OZU Evening", 1, "j.png", kind="annotation")
    _note(con, "q", created="2026-11-25", title="Should the shed face south?", page=page)
    _note(con, "plain", created="2026-11-25", title="Bought milk", page=page)
    _note(con, "old", created="2026-11-01", title="Should I?", page=page)
    _note(con, "margin", created="2026-11-25", title="Maybe later?", page=back)
    _note(con, "acted", created="2026-11-25", title="Need to call?", page=page)
    _action(con, "acted", "call", TODAY)
    con.commit()

    data = brief.gather(con, TODAY)
    assert [(b["id"], b["what"]) for b in data["waiting"]] == [
        ("B-1", "r2 waiting for @approved on its proposal"),
        ("B-2", "stopped · @input on its proposal re-arms it")]
    assert [n["title"] for n in data["lately"]] == ["Should the shed face south?"]


def test_an_empty_vault_has_no_brief(env):
    from ozu import brief, db
    assert brief.gather(db.connect(env.db_path), TODAY) is None


def test_the_brief_prints_no_unfilled_box_and_every_action_is_ticked_by_its_text(tmp_path):
    """Today's one thing is a ruled line, never a rectangle (D-04); each action
    line is `☐  <its own text>` so a tick matches it (invariant 5)."""
    import fitz
    import corpus
    from ozu import textlayer
    from ozu.digest_pdf import build_edition_pdf
    payload = corpus.sample()["edition"]["payload"]
    out = build_edition_pdf(payload, tmp_path / "m.pdf")
    with fitz.open(out) as doc:
        page = doc[1]
        text = textlayer.extract(page)
        for g in page.get_drawings():
            if not g.get("fill"):
                assert not any(i[0] == "re" for i in g["items"]), g
    for a in payload["brief"]["next"]:
        assert "☐ " + a["text"].split()[0] in " ".join(text.split())


def test_a_morning_goes_out_for_the_brief_alone_and_the_evening_asks_about_it(env):
    from ozu import db, edition
    con = db.connect(env.db_path)
    edition.ensure_ledgers(con)
    _note(con, "n1", created="2026-11-01")
    _action(con, "n1", "call the notary", TODAY)
    con.commit()
    morning = edition.gather(con, "morning", TODAY)
    assert morning and morning["brief"]["next"][0]["text"] == "call the notary"
    assert not edition.gather(con, "evening", TODAY)["ask_morning"]
    edition.record(con, f"Ozu Morning {TODAY}", "morning", sent_on=TODAY.isoformat())
    evening = edition.gather(con, "evening", TODAY)
    assert evening["ask_morning"] and evening["brief"] is None


def test_the_delete_list_command_prints_and_init_still_runs(env, monkeypatch, capsys):
    """`ozu edition delete-list` prints the list; adding it once shadowed the
    module's `db` inside the CLI and broke `ozu init` (caught by ruff F823)."""
    from ozu import pipeline
    monkeypatch.setattr(sys, "argv", ["ozu", "init"])
    pipeline.main()
    monkeypatch.setattr(sys, "argv", ["ozu", "edition", "delete-list"])
    pipeline.main()
    assert "Nothing on the Scribe is finished with." in capsys.readouterr().out
