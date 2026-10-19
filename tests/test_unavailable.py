"""The reader going down must not cost ink, and must not be silent.

Both halves of the 2026-09-16 failure are pinned here. The Anthropic credit
balance ran out at 07:51 that morning; every page read after it was marked
`error`, and `db.pending_pages` only ever returns `new`, so those pages could
never be retried — seven of them were the `@input` the owner had written on a
Build, which therefore never produced its r2. Four days passed before they
asked, because the editions kept arriving (a section that needs no model
call still fills one), so the library looked healthy while the reading half was dead.

So: a transient failure leaves the page queued (and stops the run), and the
next edition says what has not been read.
"""
import datetime as dt
import sys
import time
from pathlib import Path

import fitz
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from test_pipeline import env, make_pdf, MOCK_RESULT  # noqa: F401 — shared fixture

from ozu import textlayer

CREDIT = ("Error code: 400 - {'type': 'error', 'error': {'type': "
          "'invalid_request_error', 'message': 'Your credit balance is too low "
          "to access the Anthropic API. Please go to Plans & Billing...'}}")


def seed(env, n=3):
    from ozu import ingest
    for i in range(n):
        make_pdf(env.raw_dir / "Inbox" / f"p{i}.pdf", [f"page {i} of ink"])
    assert ingest.ingest() == n


def statuses(env):
    from ozu import db
    con = db.connect(env.db_path)
    return sorted(r["status"] for r in con.execute("SELECT status FROM pages"))


# --- classification ---------------------------------------------------------

def test_the_credit_message_is_transient_and_a_bad_page_is_not():
    """A 400 is normally our own bug, so the SDK's class is the wrong signal
    for this one: the credit-balance failure arrives as a 400 and is the most
    transient thing there is."""
    from ozu import understand as u
    assert u._unavailable(RuntimeError(CREDIT))
    assert u._unavailable(RuntimeError("429 rate_limit_error"))
    assert u._unavailable(RuntimeError("upstream temporarily unavailable"))
    # ...and the failures that really are about this page
    assert not u._unavailable(RuntimeError("the model declined this page (x)"))
    assert not u._unavailable(RuntimeError("page truncated at max_tokens"))
    assert not u._unavailable(ValueError("Expecting value: line 1 column 1"))


# --- the queue --------------------------------------------------------------

def test_an_unavailable_api_leaves_every_page_queued_and_stops(env, monkeypatch):
    """The bug, stated as a test: a page must not be spent by the API being
    down, and the run must not empty the queue into a dead endpoint."""
    from ozu import db, understand
    seed(env, 3)
    calls = []

    def dead(*a, **k):
        calls.append(1)
        raise RuntimeError(CREDIT)

    monkeypatch.setattr(understand, "analyze_page", dead)
    assert understand.process_pending() == 0
    assert statuses(env) == ["new", "new", "new"], "nothing may be marked error"
    assert len(calls) == 1, "the run stops at the first failure, not after 3"

    # the API comes back; the same pages are read, with no intervention
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: dict(MOCK_RESULT))
    assert understand.process_pending() == 3
    assert statuses(env) == ["processed"] * 3


def test_a_page_the_model_refuses_is_still_an_error(env, monkeypatch):
    """The other direction: a genuinely bad page must not retry for ever."""
    from ozu import understand
    seed(env, 1)
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k:
                        (_ for _ in ()).throw(RuntimeError("the model declined this page (x)")))
    assert understand.process_pending() == 0
    assert statuses(env) == ["error"]


# --- saying so --------------------------------------------------------------

def test_the_edition_says_what_it_could_not_read(env, monkeypatch, tmp_path):
    from ozu import db, edition, understand
    seed(env, 2)
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k:
                        (_ for _ in ()).throw(RuntimeError(CREDIT)))
    understand.process_pending()

    con = db.connect(env.db_path)
    # not yet: a page queued minutes ago is a busy pipeline, not a stuck one
    assert edition.gather_trouble(con, dt.date.today()) is None
    con.execute("UPDATE pages SET created_at = created_at - ?",
                ((edition.STALL_HOURS + 1) * 3600,))
    con.commit()

    trouble = edition.gather_trouble(con, dt.date.today())
    assert trouble and trouble["total"] == 2
    assert trouble["items"][0]["notebook"] == "Inbox"
    assert "not getting through" in trouble["items"][0]["why"]

    monkeypatch.setattr(edition.send_kindle, "send_pdf", lambda p: True)
    assert edition.send("evening", dt.date.today()) is True
    pdf = env.editions_dir / f"Ozu Evening {dt.date.today().isoformat()}.pdf"
    with fitz.open(pdf) as doc:
        text = "\n".join(textlayer.extract(p) for p in doc)
    assert "Pages I could not read" in text
    assert "Inbox" in text and "2 pages" in text


def test_an_edition_goes_out_for_trouble_alone(env, monkeypatch, tmp_path):
    """An evening with no ink, no result and no sheet sends nothing — unless
    something is stuck, which is exactly the night it must not stay quiet."""
    from ozu import db, edition, understand
    seed(env, 1)
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k:
                        (_ for _ in ()).throw(RuntimeError(CREDIT)))
    understand.process_pending()
    con = db.connect(env.db_path)
    con.execute("UPDATE pages SET created_at = created_at - ?",
                ((edition.STALL_HOURS + 1) * 3600,))
    con.commit()

    monkeypatch.setattr(edition.send_kindle, "send_pdf", lambda p: True)
    edition.ensure_ledgers(con)          # as send() does before it gathers
    assert edition.gather(con, "extra", dt.date.today()) is not None
    assert edition.send("extra") is True


def test_a_walled_notebook_is_counted_but_never_named(env, monkeypatch):
    """Invariant 2 reaches outbound documents: a work notebook's name is
    employer content, and this section must not be the one place it leaks."""
    from ozu import db, edition, ingest, understand
    make_pdf(env.raw_dir / "Work" / "a.pdf", ["work ink"])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k:
                        (_ for _ in ()).throw(RuntimeError(CREDIT)))
    understand.process_pending()
    con = db.connect(env.db_path)
    con.execute("UPDATE pages SET created_at = created_at - ?",
                ((edition.STALL_HOURS + 1) * 3600,))
    con.commit()

    trouble = edition.gather_trouble(con, dt.date.today())
    assert trouble["walled"] == 1 and trouble["items"] == []
    assert "Work" not in str(trouble["items"])


def test_the_credit_failure_prints_the_one_thing_that_fixes_it(env, monkeypatch):
    """A report that does not say what to do is a report that gets skimmed."""
    from ozu import db, edition, understand
    seed(env, 1)
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k:
                        (_ for _ in ()).throw(RuntimeError("the model declined this page (x)")))
    understand.process_pending()
    con = db.connect(env.db_path)
    con.execute("UPDATE pages SET error=?, status='error'", (CREDIT,))
    con.commit()
    trouble = edition.gather_trouble(con, dt.date.today())
    why = trouble["items"][0]["why"]
    assert "console.anthropic.com" in why and "ozu requeue error" in why
