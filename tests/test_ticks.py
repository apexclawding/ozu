"""Which printed ☐ carries a tick — and what the pipeline does with it.

`ink.locate_ticks` reads the ink per action line: fresh strokes, each given to
the printed marker beside its lowest point. Two jobs, each pinned here:

- D-09 must not drop a lone tick. One tick drawn over its box scores under the
  page threshold (the printed outline's HALO eats the overlap): Site Audit
  2026-09-26 p3 was filed blank with an approval on it.
- The reader's completions are checked against the ink, and the check never
  closes anything: a completion with no ink beside its line is withheld, a
  tick nobody read is receipted, and both come back on paper open.

The documents are rendered by the real layout, because `action_lines` reads
the faces and indents `layout.Document.action` prints.
"""
import sys
from pathlib import Path

import fitz
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from test_pipeline import env, MOCK_RESULT  # noqa: F401 — shared fixture

from ozu import ink

LONG = ("Call the roofer about the north slope before the grant is filed, "
        "and ask whether the quote includes the gutters")
RECEIPTS = [
    {"line": "Check the first ten brewing logs", "closed": None, "kind": "",
     "candidates": ["Fix shed latch", "Fix attic hatch",
                    "Fix water pump cover motor"]},
    {"line": LONG, "closed": None, "kind": "", "candidates": []},
    {"line": "Bought the garden hose", "closed": "Buy garden hose", "kind": "",
     "candidates": []},
]


def mail(path: Path, receipts=RECEIPTS) -> Path:
    from ozu.digest_pdf import build_mail_pdf
    path.parent.mkdir(parents=True, exist_ok=True)
    return build_mail_pdf({"stamp": "2026-09-30 07:00", "work": [], "research": [],
                           "plain": [], "receipts": receipts, "builds": []}, path)


def boxes(sent: Path, page_no: int = 2):
    with fitz.open(sent) as d:
        return ink.action_lines(d[page_no - 1])


def tick(sent: Path, back: Path, which: list[int], *, rise: float = 9,
         width: float = 1.6, page_no: int = 2, shift: float = 0) -> Path:
    """The document back with a ✓ on the chosen action lines: the stroke
    starts left of the box, its V lands on the box, and it rises up and to
    the right — the shape every real tick in the corpus has."""
    marks = boxes(sent, page_no)
    doc = fitz.open(sent)
    page = doc[page_no - 1]
    shape = page.new_shape()
    for i in which:
        r = marks[i][0]
        shape.draw_polyline([fitz.Point(r.x0 - 2, (r.y0 + r.y1) / 2),
                             fitz.Point(r.x0 + 3, r.y1 - 0.5),
                             fitz.Point(r.x1 + 5, r.y0 - rise)])
    shape.finish(color=(0, 0, 0), width=width, closePath=False)
    shape.commit()
    if shift:   # a different rendering of the "same" document
        page.set_cropbox(fitz.Rect(shift, shift, page.rect.x1, page.rect.y1))
    back.parent.mkdir(parents=True, exist_ok=True)
    doc.save(back)
    doc.close()
    return back


# --- reading the print ------------------------------------------------------

def test_action_lines_are_read_off_the_print_wrapped_and_without_the_receipt(tmp_path):
    got = [(m, t) for _, m, t in boxes(mail(tmp_path / "sent.pdf"))]
    assert got == [
        ("☐", "Check the first ten brewing logs"),
        ("☐", "Fix shed latch"),
        ("☐", "Fix attic hatch"),
        ("☐", "Fix water pump cover motor"),
        ("☐", LONG),                       # both printed lines, joined
        ("☑", "Buy garden hose"),
    ], got
    # the faint "matched no open action" line is set indented under a ☐
    # line — it must never be read as the action's continuation
    assert not any("matched" in t for _, _, t in boxes(tmp_path / "sent.pdf"))


def test_the_markers_are_the_ones_the_layout_prints():
    from ozu import layout
    assert layout.BOX in ink.OPEN_MARKERS and "[ ]" in ink.OPEN_MARKERS
    assert ink.DONE_MARKER == layout.TICKED


# --- finding the ink --------------------------------------------------------

def test_the_ticks_are_found_and_only_the_ticks(tmp_path):
    sent = mail(tmp_path / "sent.pdf")
    back = tick(sent, tmp_path / "back.pdf", [0, 2, 4])
    with fitz.open(sent) as a, fitz.open(back) as b:
        found = ink.locate_ticks(a[1], b[1])
    assert [t.ticked for t in found] == [True, False, True, False, True, False]
    assert all(t.pixels == 0 for t in found if not t.ticked), found


def test_a_tall_tick_is_not_given_to_the_line_above_it(tmp_path):
    """Counting pixels in a window round each box confused a tick with the
    top of the next line's tick rising into the window — on Ozu Mail
    2026-09-08 p2 an unticked line scored 16 px of its neighbour's ink, above
    a real tick at 15. A stroke belongs to the box beside its lowest point."""
    sent = mail(tmp_path / "sent.pdf")
    back = tick(sent, tmp_path / "back.pdf", [2], rise=16)
    with fitz.open(sent) as a, fitz.open(back) as b:
        found = ink.locate_ticks(a[1], b[1])
    assert [t.ticked for t in found][:4] == [False, False, True, False]
    assert found[1].pixels == 0


def test_a_tick_taller_than_a_line_errs_towards_paper_never_towards_a_veto(tmp_path):
    """The known limit. A stroke rising more than a whole line pitch (~19 pt
    here) crosses the printed line above, whose halo cuts it, and the cut-off
    top can be credited to that line too. There is no geometric rule that
    fixes it: a real tick drawn over its own box is cut by the outline the
    same way (most real strokes bottom out at the box's top edge). What must
    hold is the direction of the error — the real line keeps its tick, so a
    true completion is never withheld; the line above can at worst earn an
    `unread` receipt, which closes nothing. None of the 94 real boxes did."""
    sent = mail(tmp_path / "sent.pdf")
    back = tick(sent, tmp_path / "back.pdf", [2], rise=24)
    with fitz.open(sent) as a, fitz.open(back) as b:
        found = ink.locate_ticks(a[1], b[1])
    assert found[2].ticked
    keep, unseen, _ = ink.reconcile(["Fix attic hatch"], found)
    assert keep == ["Fix attic hatch"] and unseen == []


def test_the_tick_constants_sit_between_the_measurements():
    """Measured over every returning page with printed boxes: 67 ticks at
    15-113 px, 27 unticked boxes at exactly 0. And the lone tick D-09 lost
    scored 72 px against a page threshold of 100."""
    assert 0 < ink.TICK_MIN < ink.FAINTEST_TICK
    assert ink.FAINTEST_TICK < ink.THRESHOLD


def test_a_return_that_does_not_line_up_gets_no_opinion(tmp_path):
    """Site Audit 2026-09-10 was re-rendered after it was sent: 13.5 % of its
    print survived the comparison. The check then has no opinion at all."""
    sent = mail(tmp_path / "sent.pdf")
    back = tick(sent, tmp_path / "back.pdf", [0], shift=6)
    assert ink.locate(sent, back, 2) is None
    assert ink.locate(sent, tmp_path / "missing.pdf", 2) is None
    assert ink.locate(sent, back, 99) is None
    # the cover prints no box: nothing to judge, which is not "unknown"
    assert ink.locate(sent, tick(sent, tmp_path / "ok.pdf", [0]), 1) == []


# --- D-09 must not drop a lone tick -----------------------------------------

def test_a_lone_tick_on_a_box_is_read_even_under_the_page_threshold(env):
    """Site Audit 2026-09-26 p3: one approval, 72 px, filed as blank."""
    from ozu import db, ingest
    sent = mail(env.mail_dir / "Ozu Mail 2026-09-30 0700.pdf")
    back = tick(sent, env.raw_dir / "Ozu Mail 2026-09-30 0700  - Kindle.pdf",
                [2], width=1.0, rise=6)
    score = ink.score_pages(sent, back)[1]
    assert 0 < score < ink.THRESHOLD, \
        f"the scenario needs a tick under the page threshold, got {score}"

    assert ingest.ingest() == 1
    con = db.connect(env.db_path)
    got = dict(con.execute("SELECT page_no, status FROM pages").fetchall())
    assert got == {1: "skipped", 2: "new"}


# --- checking the reader ----------------------------------------------------

def T(text, pixels, marker="☐"):
    return ink.Tick(text, marker, (0, 0, 1, 1), pixels)


def test_reconcile_withholds_a_completion_with_no_ink_beside_it():
    ticks = [T("Fix shed latch", 0), T("Fix attic hatch", 40)]
    keep, unseen, unread = ink.reconcile(
        ["Fix shed latch", "Fix attic hatch"], ticks)
    assert keep == ["Fix attic hatch"]
    assert unseen == ["Fix shed latch"]
    assert unread == []


def test_reconcile_reports_a_tick_nobody_read_once():
    ticks = [T("Remind the plumber", 50), T("Remind the plumber", 60), T("Fix door", 0)]
    assert ink.reconcile([], ticks) == ([], [], ["Remind the plumber"])


def test_reconcile_leaves_alone_what_the_ink_cannot_judge():
    ticks = [T("Remind the plumber about the boiler", 0),
             T("Remind the plumber about the boiler", 55),   # the same line printed twice
             T("Buy garden hose", 0, marker="☑"),
             T("apply fix MAIL-INBOUND-SILENCE: Four days without inbound mail, and…", 30)]
    keep, unseen, unread = ink.reconcile(
        ["Remind the plumber about the boiler",            # one of two copies ticked
         "Buy garden hose",                                 # a ☑ line: agreement
         "Bought garden hose, it's a 30m reel",             # hand-written, on no line
         "apply fix MAIL-INBOUND-SILENCE: Four days without inbound mail, and the alarm"],
        ticks)
    assert unseen == [] and unread == []
    assert len(keep) == 4


def test_reconcile_reads_through_the_line_breaks_of_the_print():
    """Replayed over the journal, a space-sensitive match called three ticks
    unread that the reader had read: the print broke the action at `web-` /
    `gateway.service` and `data/` / `library`, and joining the lines put a
    space the reader did not write."""
    ticks = [T("apply fix WEB-SVC-FLAP-web-server.service: web- server.service "
               "restarted 5× in 24h", 60),
             T("apply fix DB-NOT-BACKED-UP: data/ library missing "
               "from nightly backup", 55)]
    completions = ["apply fix WEB-SVC-FLAP-web-server.service: web-server.service "
                   "restarted 5× in 24h",
                   "apply fix DB-NOT-BACKED-UP: data/library missing "
                   "from nightly backup"]
    assert ink.reconcile(completions, ticks) == (completions, [], [])


# --- end to end: what closes, what comes back on paper ----------------------

def _run(env, monkeypatch, ticked: list[int], completions: list[str]):
    from ozu import db, ingest, understand
    sent = mail(env.mail_dir / "Ozu Mail 2026-09-30 0700.pdf")
    tick(sent, env.raw_dir / "Ozu Mail 2026-09-30 0700  - Kindle.pdf", ticked)
    con = db.connect(env.db_path)
    for text in ("Fix shed latch", "Fix attic hatch",
                 "Fix water pump cover motor"):
        db.add_action(con, "n-1", text)
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: dict(
        MOCK_RESULT, completions=completions, notes=[]))
    understand.process_pending()
    done = {r["text"]: r["done"] for r in con.execute("SELECT text, done FROM actions")}
    receipts = [(r["line"], r["closed"], r["kind"]) for r in
                con.execute("SELECT * FROM receipts ORDER BY id")]
    return done, receipts


def test_a_completion_read_off_the_print_closes_nothing(env, monkeypatch):
    """The effort-sweep failure: the reader names a printed line nobody
    ticked. On a Site Audit that close would be an approved fix."""
    done, receipts = _run(env, monkeypatch, ticked=[2],
                          completions=["Fix attic hatch", "Fix shed latch"])
    assert done == {"Fix shed latch": 0, "Fix attic hatch": 1,
                    "Fix water pump cover motor": 0}
    assert ("Fix shed latch", None, "unseen") in receipts
    assert ("Fix attic hatch", "Fix attic hatch", "") in receipts


def test_a_tick_the_reader_missed_is_receipted_never_closed(env, monkeypatch):
    done, receipts = _run(env, monkeypatch, ticked=[1, 2],
                          completions=["Fix attic hatch"])
    assert done["Fix shed latch"] == 0, "the locator must never close"
    assert ("Fix shed latch", None, "unread") in receipts


def test_when_ink_and_reader_agree_nothing_changes(env, monkeypatch):
    done, receipts = _run(env, monkeypatch, ticked=[1, 2],
                          completions=["Fix shed latch", "Fix attic hatch"])
    assert done["Fix shed latch"] == done["Fix attic hatch"] == 1
    assert all(kind == "" for _, _, kind in receipts)


def test_without_the_sent_copy_the_reader_decides_alone(env, monkeypatch):
    """No opinion is not a veto: with nothing to compare against, the
    completions pass through exactly as they did before the check existed."""
    from ozu import db, understand
    con = db.connect(env.db_path)
    db.add_action(con, "n-1", "Fix shed latch")
    db.enqueue_page(con, "sha-x", "/nowhere.pdf", "Ozu Mail 2026-01-01 0700", 2,
                    str(env.pages_dir / "x.png"), "", kind="annotation")
    (env.pages_dir / "x.png").parent.mkdir(parents=True, exist_ok=True)
    (env.pages_dir / "x.png").write_bytes(b"png")
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: dict(
        MOCK_RESULT, completions=["Fix shed latch"], notes=[]))
    understand.process_pending()
    assert con.execute("SELECT done FROM actions").fetchone()["done"] == 1


def test_the_disagreement_comes_back_on_paper_as_an_open_line(tmp_path):
    from ozu.digest_pdf import RECHECK
    out = mail(tmp_path / "next.pdf", receipts=[
        {"line": "Fix shed latch", "closed": None, "kind": "unseen", "candidates": []},
        {"line": "Fix attic hatch", "closed": None, "kind": "unread", "candidates": []}])
    with fitz.open(out) as d:
        text = " ".join(" ".join(p.get_text().split()) for p in d)
    assert "☐ Fix shed latch" in text and "☐ Fix attic hatch" in text
    for sentence in RECHECK.values():
        assert " ".join(sentence.split()) in text
    assert "matched no open action" not in text
