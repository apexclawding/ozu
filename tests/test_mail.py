"""Return-mail tests: the mailed ledger, categorisation, delivery gating.
The mail reaches the device as the first section of an edition, so the
send here is `edition.send("extra")` — the on-demand edition."""
import sys
from pathlib import Path

import fitz

from ozu import textlayer

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from test_pipeline import env, make_pdf, MOCK_RESULT  # noqa: F401 — shared fixture


def extra():
    from ozu import edition
    return edition.send("extra")


def seed_note(env, monkeypatch, result=None, name="m.pdf", notebook="Inbox",
              text="x"):
    # each seeded page needs distinct ink: identical bitmaps dedup as the
    # same page and the second seed would silently never be processed
    from ozu import ingest, understand
    make_pdf(env.raw_dir / notebook / name, [text])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page",
                        lambda *a, **k: result or dict(MOCK_RESULT))
    understand.process_pending()


def test_first_run_seeds_history_as_already_mailed(env, monkeypatch):
    from ozu import db, mail
    seed_note(env, monkeypatch)  # exists BEFORE the mail feature first runs
    con = db.connect(env.db_path)
    mail._ensure_ledger(con)
    assert mail.gather(con) is None, "pre-existing notes must not be mailed"


def test_mail_covers_new_notes_once_and_only_when_delivered(env, monkeypatch):
    from ozu import db, mail, send_kindle
    con = db.connect(env.db_path)
    mail._ensure_ledger(con)  # seeded while empty
    seed_note(env, monkeypatch)

    # not configured → nothing marked, notes still pending
    assert extra() is False
    assert mail.gather(db.connect(env.db_path)) is not None

    # configured → delivered and marked; second send is a no-op
    monkeypatch.setattr(send_kindle, "send_pdf", lambda p: True)
    assert extra() is True
    assert extra() is False
    pdfs = list(env.editions_dir.glob("Ozu Extra *.pdf"))
    assert pdfs
    doc = fitz.open(pdfs[0])
    text = "".join(textlayer.extract(p) for p in doc)
    doc.close()
    assert "What your ink became" in text
    assert "Kettle freemium pricing" in text
    assert "Check competitor pricing" in text  # the action, on the page


def test_checkmark_receipts_travel_in_mail_once(env, monkeypatch):
    """Every ✓ answers back: a matched one is confirmed with the action it
    closed, a stray one is flagged — and either way exactly once."""
    from ozu import db, mail, send_kindle
    con = db.connect(env.db_path)
    mail._ensure_ledger(con)
    seed_note(env, monkeypatch)  # creates "Check competitor pricing"
    done = dict(MOCK_RESULT, notes=[], completions=[
        "checked the competitor pricing",
        "Fixed the boiler",  # nothing like it in the ledger
    ])
    seed_note(env, monkeypatch, done, name="c.pdf", text="check page")

    monkeypatch.setattr(send_kindle, "send_pdf", lambda p: True)
    assert extra() is True
    pdf = sorted(env.editions_dir.glob("Ozu Extra *.pdf"))[-1]
    doc = fitz.open(pdf)
    text = "".join(textlayer.extract(p) for p in doc)
    doc.close()
    assert "Your checkmarks" in text
    assert "Check competitor pricing" in text        # the confirmation
    assert "Fixed the boiler" in text                # the miss, visible
    assert "matched no open action" in text
    assert extra() is False              # reported exactly once


def test_receipts_alone_trigger_the_mail(env, monkeypatch):
    """A page that only checks things off still deserves an answer — no new
    note in the cycle must not mean no mail."""
    from ozu import db, mail, send_kindle
    con = db.connect(env.db_path)
    mail._ensure_ledger(con)
    seed_note(env, monkeypatch)
    monkeypatch.setattr(send_kindle, "send_pdf", lambda p: True)
    assert extra() is True  # flush the note

    done = dict(MOCK_RESULT, notes=[], completions=["checked competitor pricing"])
    seed_note(env, monkeypatch, done, name="c2.pdf", text="tick page")
    assert extra() is True  # the receipt carried the mail alone


def test_missed_checkmark_lists_nearest_open_actions(env, monkeypatch):
    """A miss shows what it nearly matched, captured against the ledger of
    the moment the ✓ was read — and closes nothing."""
    from ozu import db, mail, send_kindle
    con = db.connect(env.db_path)
    mail._ensure_ledger(con)
    db.add_action(con, "n1", "Fix water pump filter motor")
    db.add_action(con, "n2", "Fix water pump cover motor")
    done = dict(MOCK_RESULT, notes=[], completions=["Fixed the pump"])
    seed_note(env, monkeypatch, done)

    monkeypatch.setattr(send_kindle, "send_pdf", lambda p: True)
    assert extra() is True
    pdf = sorted(env.editions_dir.glob("Ozu Extra *.pdf"))[-1]
    doc = fitz.open(pdf)
    text = "".join(textlayer.extract(p) for p in doc)
    doc.close()
    assert "closest still open" in text
    assert "Fix water pump filter motor" in text
    con = db.connect(env.db_path)
    assert con.execute("SELECT COUNT(*) c FROM actions WHERE done=1").fetchone()["c"] == 0


def test_mail_carries_full_work_reports_and_skips_walled(env, monkeypatch):
    from ozu import agent, db, mail, vault
    con = db.connect(env.db_path)
    mail._ensure_ledger(con)
    # a companion work note lands in the vault
    seed_note(env, monkeypatch)
    src = db.connect(env.db_path).execute("SELECT * FROM notes").fetchone()
    vault.write_agent_note(source=dict(src), report="## Report\nBuilt the thing.\nNeeds your judgment: pricing.",
                           branch="claude/x", compare_url=None)
    # walled note must never appear
    walled = dict(MOCK_RESULT, work_flag=True)
    walled["notes"] = [dict(MOCK_RESULT["notes"][0], title="Secret work thing",
                        slug="secret", actions=[])]
    seed_note(env, monkeypatch, walled, name="w.pdf", notebook="Work", text="work ink")

    data = mail.gather(db.connect(env.db_path))
    titles = str(data)
    assert "Needs your judgment" in str([w.get("body") for w in data["work"]])
    assert "Secret work thing" not in titles


def test_send_logs_the_file_never_the_address(env, monkeypatch, tmp_path, caplog):
    """The @kindle.com address is half of a delivery credential, and the
    journal is read by anything that greps it: the log line names the
    document only."""
    import logging
    import smtplib
    from ozu import send_kindle
    monkeypatch.setattr(env, "kindle_email", "someone_x1@kindle.com")
    monkeypatch.setattr(env, "smtp_user", "me@example.com")
    monkeypatch.setattr(env, "smtp_pass", "pw")
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg["To"])

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    pdf = tmp_path / "Ozu Morning 2026-10-21.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with caplog.at_level(logging.INFO, logger="ozu.send_kindle"):
        assert send_kindle.send_pdf(pdf)
    assert sent == ["someone_x1@kindle.com"]
    assert "Ozu Morning 2026-10-21.pdf sent to Send-to-Kindle" in caplog.text
    assert "someone_x1" not in caplog.text

