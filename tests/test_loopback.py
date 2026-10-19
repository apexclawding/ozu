"""Scribe loop-back tests: digest PDF rendering, send-to-kindle gating, and
the ingest guard keeping re-exported digests out of the vault."""
import sys
from pathlib import Path

import fitz

from ozu import textlayer

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from test_pipeline import env, make_pdf, MOCK_RESULT  # noqa: F401 — shared fixture


def seed(env, monkeypatch, result=None):
    from ozu import ingest, understand
    make_pdf(env.raw_dir / "Inbox" / "s.pdf", ["seed"])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page",
                        lambda *a, **k: result or dict(MOCK_RESULT))
    understand.process_pending()


def test_digest_pdf_renders_content_and_review_ink(env, monkeypatch):
    from ozu import digest, digest_pdf
    seed(env, monkeypatch, dict(MOCK_RESULT, confidence=0.4))  # → needs_review
    out = env.digest_dir / "Ozu Digest test.pdf"
    digest_pdf.build_pdf(digest.gather(), out)
    doc = fitz.open(out)
    text = "".join(textlayer.extract(p) for p in doc)
    doc.close()
    assert "Ozu weekly digest" in text
    assert "Check competitor pricing" in text     # the red-ink action
    assert "Needs review" in text                 # low-confidence section
    assert "Annotate in ink" in text              # footer invites the loop


def test_digest_rides_sundays_evening_edition(env, monkeypatch):
    """`digest.run` writes the Markdown only; the typeset digest is a section
    of the Sunday evening edition, from the same gather."""
    import datetime as dt
    from ozu import digest, edition, send_kindle
    seed(env, monkeypatch)
    assert digest.run().suffix == ".md"
    assert not list(env.digest_dir.glob("*.pdf"))
    monkeypatch.setattr(send_kindle, "send_pdf", lambda p: True)
    sunday = dt.date(2026, 9, 13)
    assert edition.send("evening", sunday) is True
    pdf = env.editions_dir / "Ozu Evening 2026-09-13.pdf"
    doc = fitz.open(pdf)
    text = "".join(textlayer.extract(p) for p in doc)
    doc.close()
    assert "Ozu weekly digest" in text and "Weekly digest" in text
    assert (env.digest_dir / "2026-09-13.md").exists() or list(env.digest_dir.glob("*.md"))


def test_send_kindle_is_a_noop_without_config(env, monkeypatch, tmp_path):
    from ozu import send_kindle

    def explode(*a, **k):
        raise AssertionError("SMTP must not be touched when unconfigured")

    monkeypatch.setattr(send_kindle.smtplib, "SMTP", explode)
    pdf = tmp_path / "d.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert send_kindle.send_pdf(pdf) is False


def test_send_kindle_sends_when_configured(env, monkeypatch, tmp_path):
    import ozu.config as config
    from ozu import send_kindle
    monkeypatch.setenv("OZU_KINDLE_EMAIL", "owner_x@kindle.com")
    monkeypatch.setenv("OZU_SMTP_USER", "me@example.com")
    monkeypatch.setenv("OZU_SMTP_PASS", "app-pass")
    config._config = None
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=0):
            sent["host"] = (host, port)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def starttls(self):
            sent["tls"] = True
        def login(self, u, p):
            sent["login"] = (u, p)
        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr(send_kindle.smtplib, "SMTP", FakeSMTP)
    pdf = tmp_path / "Ozu Digest 2026-08-23.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert send_kindle.send_pdf(pdf) is True
    assert sent["msg"]["To"] == "owner_x@kindle.com"
    assert sent["tls"] and sent["login"] == ("me@example.com", "app-pass")


def test_ozu_reexport_queues_annotation_pages(env):
    """Annotated "Ozu " documents come back through the same Drive folder.
    The old guard skipped them; now they queue as kind='annotation', read by
    the annotation prompt whose contract is that printed text never becomes
    notes — only the handwriting does."""
    from ozu import ingest, db
    make_pdf(env.raw_dir / "Ozu Digest 2026-08-23  - Kindle.pdf",
             ["printed digest text"])
    assert ingest.ingest() == 1
    con = db.connect(env.db_path)
    row = con.execute("SELECT notebook, kind FROM pages").fetchone()
    assert row["kind"] == "annotation"
    assert row["notebook"] == "Ozu Digest 2026-08-23"  # Kindle suffix stripped


ANNOTATION_EMPTY = {  # a page that came back with no ink — the common case
    "transcription": "", "drawing_description": "", "mermaid": "",
    "confidence": 0.95, "work_flag": False, "completions": [],
    "build_directive": {"kind": "none", "model": None}, "build_ref": None,
    "notes": [],
}


def test_annotation_tick_closes_action_and_receipts(env, monkeypatch):
    """The loop the footer promises: tick a printed action on the Scribe,
    re-export, and the action closes — confirmed by a receipt."""
    from ozu import ingest, understand, db
    seed(env, monkeypatch)  # creates open "Check competitor pricing"

    make_pdf(env.raw_dir / "Ozu Mail 2026-08-25 1030  - Kindle.pdf",
             ["[ ] Check competitor pricing"])
    ingest.ingest()
    seen = {}

    def fake(image_path, notebook, page_no, ocr_text="", kind="ink"):
        seen["kind"], seen["notebook"] = kind, notebook
        return dict(ANNOTATION_EMPTY, completions=["Check competitor pricing"])

    monkeypatch.setattr(understand, "analyze_page", fake)
    understand.process_pending()

    assert seen == {"kind": "annotation", "notebook": "Ozu Mail 2026-08-25 1030"}
    con = db.connect(env.db_path)
    assert con.execute("SELECT done FROM actions").fetchone()["done"] == 1
    r = con.execute("SELECT closed FROM receipts").fetchone()
    assert r["closed"] == "Check competitor pricing"


def test_annotation_reread_of_old_tick_is_quiet(env, monkeypatch):
    """Annotating more and re-exporting re-reads the page's old ticks. A ✓
    on an already-closed action is a re-read, not a miss — no receipt."""
    from ozu import ingest, understand, db
    seed(env, monkeypatch)
    con = db.connect(env.db_path)
    con.execute("UPDATE actions SET done=1")
    con.commit()

    make_pdf(env.raw_dir / "Ozu Mail x  - Kindle.pdf",
             ["[ ] Check competitor pricing"])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k:
                        dict(ANNOTATION_EMPTY,
                             completions=["Check competitor pricing"]))
    understand.process_pending()
    con = db.connect(env.db_path)
    assert con.execute("SELECT COUNT(*) c FROM receipts").fetchone()["c"] == 0


def test_annotation_margin_ink_becomes_note_and_red_action(env, monkeypatch):
    """Margin writing joins the vault like any notebook ink: a note, and red
    ink still the only action creator."""
    from ozu import ingest, understand, db
    make_pdf(env.raw_dir / "Ozu research brief x  - Kindle.pdf", ["brief text"])
    ingest.ingest()
    note = {"title": "Margin: try the north-slope angle", "slug": "north-slope-angle",
            "topic": "ideas", "tags": ["research"], "summary": "Margin follow-up.",
            "body": "Re: the brief - try the north-slope angle.", "related": [],
            "related_reason": "",
            "actions": ["Ask the installer about pellet prices"]}
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k:
                        dict(ANNOTATION_EMPTY, notes=[note]))
    understand.process_pending()
    con = db.connect(env.db_path)
    assert con.execute("SELECT title FROM notes").fetchone()["title"] \
        == "Margin: try the north-slope angle"
    a = con.execute("SELECT text, done FROM actions").fetchone()
    assert a["text"] == "Ask the installer about pellet prices" and a["done"] == 0


def test_annotation_prompt_carries_printed_reference(env):
    """The annotation prompt gets the document's own text layer as the
    printed-side reference, and the guard sentence that keeps printed text
    out of the vault."""
    from ozu import understand
    p = understand._build_annotation_prompt("Ozu Mail 2026-08-25", 2,
                                            "[ ] Fix attic hatch")
    assert "Ozu Mail 2026-08-25" in p
    assert "[ ] Fix attic hatch" in p
    assert "PRINTED TEXT IS ALREADY IN THE SYSTEM" in p


def _git(args, cwd):
    import subprocess
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t"] + args,
                   cwd=cwd, check=True, capture_output=True)


def test_vault_sync_folds_in_phone_edits(env, tmp_path):
    """Obsidian on the phone pushes to GitHub; the nightly folds it in."""
    from ozu import vault
    origin = tmp_path / "vault-origin.git"
    _git(["init", "-q", "--bare", "--initial-branch=main", str(origin)], cwd=tmp_path)
    vps = tmp_path / "vps-vault"
    _git(["clone", "-q", str(origin), str(vps)], cwd=tmp_path)
    (vps / "note.md").write_text("original\n")
    _git(["add", "-A"], cwd=vps)
    _git(["commit", "-q", "-m", "seed"], cwd=vps)
    _git(["push", "-q", "-u", "origin", "HEAD:main"], cwd=vps)

    phone = tmp_path / "phone"
    _git(["clone", "-q", str(origin), str(phone)], cwd=tmp_path)
    (phone / "note.md").write_text("edited on the phone\n")
    _git(["add", "-A"], cwd=phone)
    _git(["commit", "-q", "-m", "phone edit"], cwd=phone)
    _git(["push", "-q", "origin", "HEAD:main"], cwd=phone)

    assert vault.sync_from_origin(vps) is True
    assert (vps / "note.md").read_text() == "edited on the phone\n"
    assert vault.sync_from_origin(vps) is False  # nothing new second time


def test_vault_sync_conflict_keeps_local_and_a_clean_tree(env, tmp_path):
    """Same note edited on both sides: VPS stays truth, no half-done rebase."""
    import subprocess
    from ozu import vault
    origin = tmp_path / "vault-origin.git"
    _git(["init", "-q", "--bare", "--initial-branch=main", str(origin)], cwd=tmp_path)
    vps = tmp_path / "vps-vault"
    _git(["clone", "-q", str(origin), str(vps)], cwd=tmp_path)
    (vps / "note.md").write_text("original\n")
    _git(["add", "-A"], cwd=vps)
    _git(["commit", "-q", "-m", "seed"], cwd=vps)
    _git(["push", "-q", "-u", "origin", "HEAD:main"], cwd=vps)

    phone = tmp_path / "phone"
    _git(["clone", "-q", str(origin), str(phone)], cwd=tmp_path)
    (phone / "note.md").write_text("phone version\n")
    _git(["add", "-A"], cwd=phone)
    _git(["commit", "-q", "-m", "phone"], cwd=phone)
    _git(["push", "-q", "origin", "HEAD:main"], cwd=phone)

    (vps / "note.md").write_text("vps version\n")
    _git(["add", "-A"], cwd=vps)
    _git(["commit", "-q", "-m", "vps"], cwd=vps)

    assert vault.sync_from_origin(vps) is False
    assert (vps / "note.md").read_text() == "vps version\n"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=vps,
                            capture_output=True, text=True).stdout
    assert status.strip() == ""
    assert not (vps / ".git" / "rebase-merge").exists()


def test_vault_sync_without_remote_is_quiet(env):
    """Test vaults (and a fresh init) have no origin; sync must be a no-op."""
    from ozu import vault
    vault.scaffold(env.vault_dir)
    assert vault.sync_from_origin(env.vault_dir) is False
