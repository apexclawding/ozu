"""The editions (edition.py): two documents a day instead of one per event.

Pinned here:
  · the morning goes out once a day and always (it carries the development
    brief), Monday's also carries the week; a second run is a no-op unless
    forced;
  · an evening with nothing sends nothing, and a send that did not reach the
    device writes no ledger — the same items ride the next edition;
  · a Build result rides the next edition exactly once, its pages stamped
    with the Build's own id while the rest of the edition's are not, and its
    ☐ Review line survives extraction;
  · the delete list follows the rules in the module docstring, the backlog on
    disk is seeded once, a listed document is named for a week and counted
    after, and the `delivered` ledger survives `ozu index`;
  · the Build filename puts the idea first and keeps the id where the
    matcher reads it;
  · an edition's sections start on fresh pages with no blank sheet between,
    it carries a PDF outline, and its metadata names the kind (D-07).
"""
import datetime as dt
import sys
from pathlib import Path

import fitz
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from test_pipeline import env, make_pdf, MOCK_RESULT  # noqa: F401 — shared fixture
from test_builds import (make_build, annotate, annotation_result,  # noqa: F401
                         sent, runs)
from test_agent import make_origin, map_repo  # noqa: F401

from ozu import textlayer

FRIDAY = dt.date(2026, 9, 11)
MONDAY = dt.date(2026, 9, 14)


def deliver(monkeypatch):
    from ozu import send_kindle
    monkeypatch.setattr(send_kindle, "send_pdf", lambda p: True)


def pages_of(pdf: Path) -> list[str]:
    with fitz.open(pdf) as doc:
        return [textlayer.extract(p) for p in doc]


def seed_receipt(env, monkeypatch, line="checked the competitor pricing",
                 name="c.pdf", text="tick page"):
    """One processed page whose ink is a ✓ — the smallest thing that
    deserves an answer."""
    from ozu import ingest, understand
    done = dict(MOCK_RESULT, notes=[], completions=[line])
    make_pdf(env.raw_dir / "Inbox" / name, [text])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: done)
    understand.process_pending()


# ---------------------------------------------------------------------------
# cadence

def test_morning_goes_out_once_a_day(env, monkeypatch):
    from ozu import edition
    deliver(monkeypatch)
    seed_receipt(env, monkeypatch)

    assert edition.send("morning", FRIDAY) is True
    pdf = env.editions_dir / "Ozu Morning 2026-09-11.pdf"
    text = "\n".join(pages_of(pdf))
    assert "In this edition" in text
    assert "Friday 11 September 2026" in text
    with fitz.open(pdf) as doc:
        assert doc.metadata["author"] == "Ozu Edition"
        assert doc.metadata["title"] == "Ozu Morning 2026-09-11"
        titles = [t[1] for t in doc.get_toc()]
    assert "In this edition" in titles

    assert edition.send("morning", FRIDAY) is False        # one a day
    seed_receipt(env, monkeypatch, line="booked the dentist", name="d.pdf",
                 text="another tick page")
    assert edition.send("morning", FRIDAY) is False        # still one a day
    assert edition.send("morning", FRIDAY, force=True) is True

def test_an_evening_with_nothing_sends_nothing_and_a_failed_send_keeps_the_ledgers(
        env, monkeypatch):
    from ozu import db, edition
    thursday = dt.date(2026, 9, 10)
    assert edition.send("evening", thursday) is False       # nothing at all
    assert not list(env.editions_dir.glob("*.pdf")) if env.editions_dir.exists() else True

    seed_receipt(env, monkeypatch)
    # Send-to-Kindle is not configured in tests: built, not sent, nothing
    # marked — the receipt is still unreported and rides the next one
    assert edition.send("evening", thursday) is False
    con = db.connect(env.db_path)
    assert db.unreported_receipts(con)
    assert con.execute("SELECT COUNT(*) c FROM delivered").fetchone()["c"] == 0

    deliver(monkeypatch)
    assert edition.send("evening", thursday) is True
    con = db.connect(env.db_path)
    assert not db.unreported_receipts(con)
    row = con.execute("SELECT * FROM delivered").fetchone()
    assert (row["name"], row["kind"], row["sent_on"]) == (
        "Ozu Evening 2026-09-10", "evening", "2026-09-10")
    text = "\n".join(pages_of(env.editions_dir / "Ozu Evening 2026-09-10.pdf"))
    assert "Your checkmarks" in text and "checked the competitor pricing" in text
    assert edition.send("evening", thursday) is False       # once


# ---------------------------------------------------------------------------
# a Build result inside an edition

def test_a_result_rides_the_next_edition_once_with_its_own_page_header(
        env, monkeypatch, sent, runs, tmp_path):
    from ozu import builds, db, edition
    bid, _ = make_build(env, monkeypatch, sent, runs, tmp_path)
    r1 = sent[-1].stem
    annotate(env, monkeypatch, bid,
             annotation_result("approve", model="sonnet", build_ref=bid,
                               transcription="@approved @sonnet"))
    assert builds.run_pending() == 1
    assert not any("result" in p.name for p in sent), "no result document"

    assert edition.send("extra") is True
    pdf = sent[-1]
    assert pdf.name.startswith("Ozu Extra ")
    pages = pages_of(pdf)
    header = f"Ozu Build {bid} result"
    result_pages = [t for t in pages if header in t]
    assert result_pages, "the result section must stamp the Build id"
    assert any("☐ Review " in t for t in result_pages)
    assert header not in pages[0] and header not in pages[1], (
        "the cover and the contents page belong to the edition, not the Build")
    # matching a returning page of it works on the header alone — the
    # edition's filename names no Build
    assert builds.match_build("Ozu Extra 2026-09-11 0700", result_pages[0], bid) == (
        bid, "build_ref+textlayer")

    assert edition.send("extra") is False, "reported exactly once"

    # and the proposal it came from is now safe to delete
    con = db.connect(env.db_path)
    dead = {d["name"]: d["reason"] for d in edition.dead_documents(con, dt.date.today())}
    assert r1 in dead and dead[r1].startswith("built")


# ---------------------------------------------------------------------------
# the delete list

def _build_row(con, bid, status, rev=1):
    from ozu import db
    db.upsert_build(con, build_id=bid, path=f"/x/{bid}.md", topic="t",
                    title="T", status=status, rev=rev, model=None,
                    branch=None, pr=None, pending="", source_note=None,
                    updated="2026-09-01")


def test_delete_list_rules(env):
    from ozu import db, edition
    con = db.connect(env.db_path)
    edition.ensure_ledgers(con)
    today = FRIDAY
    rec = edition.record

    _build_row(con, "B-260901-aaaa", "revised", rev=2)
    rec(con, "Ozu Build r1 - Idea (B-260901-aaaa)", "build",
        build_id="B-260901-aaaa", rev=1, sent_on="2026-09-01")
    rec(con, "Ozu Build r2 - Idea (B-260901-aaaa)", "build",
        build_id="B-260901-aaaa", rev=2, sent_on="2026-09-02")
    _build_row(con, "B-260901-bbbb", "done")
    rec(con, "Ozu Build r1 - Done (B-260901-bbbb)", "build",
        build_id="B-260901-bbbb", rev=1, sent_on="2026-09-01")
    _build_row(con, "B-260901-cccc", "error")
    rec(con, "Ozu Build r1 - Broken (B-260901-cccc)", "build",
        build_id="B-260901-cccc", rev=1, sent_on="2026-09-01")
    rec(con, "Ozu Research old", "research", sent_on="2026-08-10")
    rec(con, "Ozu Research new", "research", sent_on="2026-09-06")
    rec(con, "Ozu Morning 2026-09-10", "morning", sent_on="2026-09-10")
    rec(con, "Ozu Evening 2026-09-10", "evening", sent_on="2026-09-10")
    rec(con, "Ozu Mail 2026-09-07 1634", "mail", sent_on="2026-09-07")
    rec(con, "Ozu Build r1 - Sim (B-260901-dddd)", "simulate",
        build_id="B-260901-dddd", sent_on="2026-09-09")

    dead = {d["name"]: d["reason"] for d in edition.dead_documents(con, today)}
    assert dead["Ozu Build r1 - Idea (B-260901-aaaa)"] == "superseded by r2"
    assert "Ozu Build r2 - Idea (B-260901-aaaa)" not in dead
    assert dead["Ozu Build r1 - Done (B-260901-bbbb)"].startswith("built")
    assert "Ozu Build r1 - Broken (B-260901-cccc)" not in dead   # re-armable
    assert "older than 30 days" in dead["Ozu Research old"]
    assert "Ozu Research new" not in dead
    assert "Ozu Morning 2026-09-10" in dead                     # the older edition
    assert "Ozu Evening 2026-09-10" not in dead                 # the newest stays
    assert "Ozu Mail" in dead["Ozu Mail 2026-09-07 1634"]
    assert "simulated" in dead["Ozu Build r1 - Sim (B-260901-dddd)"]

    # named for a week, counted after
    first = edition.gather_dead(con, today)
    assert {d["name"] for d in first["recent"]} == set(dead)
    assert first["older"] == 0 and set(first["new_names"]) == set(dead)
    edition.mark_dead(con, first["new_names"], today)
    later = edition.gather_dead(con, today + dt.timedelta(days=3))
    assert {d["name"] for d in later["recent"]} == set(dead) and later["new_names"] == []
    much_later = edition.gather_dead(con, today + dt.timedelta(days=8))
    assert much_later["recent"] == [] and much_later["older"] == len(dead)


def test_the_backlog_on_disk_is_seeded_once_and_finished_builds_are_not_re_reported(env):
    from ozu import db, edition
    files = {
        env.mail_dir: ["Ozu Mail 2026-09-07 1634.pdf"],
        env.research_dir: ["Ozu Research 20260823-x.pdf"],
        env.builds_dir: ["Ozu Build B-260905-e5f1 r1.pdf",
                         "Ozu Build B-260905-e5f1 r2.pdf",
                         "Ozu Build B-260905-e5f1 result.pdf",
                         "not a ozu file.pdf"],
    }
    for folder, names in files.items():
        folder.mkdir(parents=True, exist_ok=True)
        for n in names:
            (folder / n).write_bytes(b"%PDF-1.4 stub")
    con = db.connect(env.db_path)
    _build_row(con, "B-260905-e5f1", "done", rev=2)
    _build_row(con, "B-260905-ffff", "done")      # executed under the old path
    _build_row(con, "B-260905-0000", "approved")  # not yet — must be reported

    edition.ensure_ledgers(con)
    rows = {r["name"]: r for r in con.execute("SELECT * FROM delivered")}
    assert len(rows) == 5 and "not a ozu file" not in rows
    assert rows["Ozu Build B-260905-e5f1 r2"]["kind"] == "build"
    assert rows["Ozu Build B-260905-e5f1 r2"]["rev"] == 2
    assert rows["Ozu Build B-260905-e5f1 result"]["kind"] == "result"
    mailed = {r["note_id"] for r in con.execute("SELECT note_id FROM mailed")}
    assert {"B-260905-e5f1", "B-260905-ffff"} <= mailed
    assert "B-260905-0000" not in mailed

    dead = {d["name"] for d in edition.dead_documents(con, FRIDAY)}
    assert dead == set(rows) - {"Ozu Research 20260823-x"}   # 19 days old: kept

    # once: a file that appears later is not a delivery
    (env.mail_dir / "Ozu Mail 2026-09-11 0700.pdf").write_bytes(b"%PDF")
    edition.ensure_ledgers(con)
    assert con.execute("SELECT COUNT(*) c FROM delivered").fetchone()["c"] == 5


def test_delivered_ledger_survives_index_rebuild(env):
    from ozu import db, edition, indexer
    con = db.connect(env.db_path)
    edition.record(con, "Ozu Morning 2026-09-11", "morning")
    indexer.rebuild()
    assert db.connect(env.db_path).execute(
        "SELECT COUNT(*) c FROM delivered").fetchone()["c"] == 1


# ---------------------------------------------------------------------------
# names

def test_build_filename_puts_the_idea_first_and_keeps_the_id_for_the_matcher():
    from ozu import builds
    name = builds.pdf_name("B-260906-7f3a", 2, 'Weekly "week ahead" note: for atlas/v4?')
    assert name == 'Ozu Build r2 - Weekly week ahead note for atlas v4 (B-260906-7f3a)'
    assert builds.ID_RE.search(name).group(0) == "B-260906-7f3a"
    assert not set('\\/:*?"<>|') & set(name)
    long = builds.pdf_name("B-260906-7f3a", 1, "word " * 30)
    assert len(long) <= len("Ozu Build r1 -  (B-260906-7f3a)") + builds.TITLE_IN_NAME
    assert long.endswith("(B-260906-7f3a)") and "  " not in long
    assert builds.pdf_name("B-260906-7f3a", 1, "") == "Ozu Build r1 - B-260906-7f3a (B-260906-7f3a)"
    # the edition's own name is dated, and an extra carries the hour
    from ozu import edition
    assert edition.edition_name("morning", FRIDAY) == "Ozu Morning 2026-09-11"
    assert edition.edition_name("extra", FRIDAY, dt.datetime(2026, 9, 11, 16, 34)) == \
        "Ozu Extra 2026-09-11 1634"


# ---------------------------------------------------------------------------
# the page (D-07)

def test_sections_start_on_fresh_pages_with_no_blank_sheet_and_an_outline(tmp_path, env):
    import corpus
    from ozu import layout
    from ozu.digest_pdf import build_edition_pdf
    case = corpus.sample()["edition"]
    out = build_edition_pdf(case["payload"], tmp_path / "e.pdf")
    with fitz.open(out) as doc:
        pages = [textlayer.extract(p) for p in doc]
        toc = doc.get_toc()
        meta = doc.metadata
    assert all(p.strip() for p in pages), "a blank page between sections"
    assert meta["author"] == "Ozu Edition" and meta["title"] == "e"

    level1 = [(t, page) for level, t, page in toc if level == 1]
    assert [t for t, _ in level1] == [
        "Ozu Morning", "What your ink became",
        "Build result · Weekly Sunday “week ahead” note",
        "Safe to delete from the Scribe"]
    starts = [page for _, page in level1]
    assert starts == sorted(starts) and len(set(starts)) == len(starts), (
        "every section starts on its own page")
    # the section's band is the first thing on its page: only the running
    # header and the band's kicker can come before its title
    for (_, page), band_title in zip(level1[1:], (
            "What your ink became", "Weekly Sunday",
            "Safe to delete")):
        assert band_title in pages[page - 1][:160], (band_title, pages[page - 1][:160])
    assert any(level == 2 and t == "Your checkmarks" for level, t, _ in toc)

    # the result's pages carry its id; the mail's and the brief's do not
    header = "Ozu Build B-260906-7f3a result"
    with_header = [i for i, p in enumerate(pages) if header in p]
    assert with_header, "the result section must stamp the Build id"
    mail_page = next(i for i, p in enumerate(pages) if "Your checkmarks" in p)
    assert mail_page not in with_header
    assert "☐ Review PR #12" in "\n".join(pages[i] for i in with_header)

    # the delete list names the documents and counts the rest
    tail = "\n".join(pages)
    assert "Ozu Mail 2026-09-07 1634" in tail and "superseded by r2" in tail
    assert "12 older ones" in tail

    # the CSS trap this design exists to avoid (module docstring, D-07)
    assert "page-break-before" not in layout.CSS


def test_an_edition_with_nothing_still_has_a_contents_page(tmp_path, env):
    from ozu.digest_pdf import build_edition_pdf
    out = build_edition_pdf({"kind": "extra", "date": "2026-09-11",
                             "weekday": "Friday 11 September 2026",
                             "stamp": "2026-09-11 16:34", "mail": None,
                             "results": [], "digest": None, "dead": {}}, tmp_path / "x.pdf")
    pages = pages_of(out)
    assert len(pages) == 2 and "Nothing since the last edition" in pages[1]


def test_every_kind_declares_its_author():
    from ozu import layout
    for kind in ("edition", "build", "research"):
        assert layout.AUTHORS[kind].startswith("Ozu ")
    assert "morning" in layout.MARKS and "evening" in layout.MARKS


def _digest_reason(env):
    from ozu import db, edition
    con = db.connect(env.db_path)
    edition.ensure_ledgers(con)
    edition.record(con, "Ozu Digest 2026-09-01", "digest", sent_on="2026-09-01")
    return {d["name"]: d["reason"]
            for d in edition.dead_documents(con, FRIDAY)}["Ozu Digest 2026-09-01"]


def test_digest_reason_names_the_configured_digest_folder(env, tmp_path):
    # env points OZU_HOME at a tmp dir, outside the user's home
    assert _digest_reason(env) == (
        f"an old digest — the Markdown is in {tmp_path / 'ozu' / 'digests'}")


def test_digest_reason_is_unchanged_for_the_default_home(env, tmp_path, monkeypatch):
    import ozu.config as config
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.delenv("OZU_HOME", raising=False)
    config._config = None
    cfg = config.get_config()
    cfg.home.mkdir(parents=True, exist_ok=True)
    fake_home = tmp_path / "fakehome"
    rel = cfg.digest_dir.relative_to(fake_home).as_posix()
    reason = _digest_reason(cfg)
    assert reason == f"an old digest — the Markdown is in ~/{rel}"
    assert reason.startswith("an old digest — the Markdown is in ~/")
    assert reason.endswith("/digests")
