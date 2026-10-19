"""@research tests. The headless call is mocked; the ledger, the companion
note, the PDF, the delivery gating, and the governance boundary are real."""
import os.path
import sys
from pathlib import Path

import fitz

from ozu import textlayer

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from test_pipeline import env, make_pdf, MOCK_RESULT  # noqa: F401 — shared fixture

BRIEF = """# TL;DR
Freemium works if the scanner is the paywall.

# What I found
Competitors charge for pantry scanning.
https://example.com/pricing-study

# Against your own notes
Supports [20250101-old-note].

# What to decide or do next
- Pick the free tier's scan limit."""


def research_result():
    r = dict(MOCK_RESULT)
    r["notes"] = [dict(MOCK_RESULT["notes"][0], research_request=True, actions=[])]
    return r


def process_one(env, monkeypatch, result, notebook="Inbox"):
    from ozu import ingest, understand
    make_pdf(env.raw_dir / notebook / "r.pdf", ["x"])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: result)
    understand.process_pending()


def test_research_request_flows_to_frontmatter_and_db(env, monkeypatch):
    from ozu import db
    process_one(env, monkeypatch, research_result())
    note = next((env.vault_dir / "topics" / "kettle").glob("*kettle-freemium-pricing*.md"))
    assert "research: requested" in note.read_text()
    con = db.connect(env.db_path)
    assert con.execute("SELECT research FROM notes").fetchone()["research"] == "requested"


def test_walled_note_never_queues_research(env, monkeypatch):
    from ozu import db, research
    process_one(env, monkeypatch, research_result() | {"work_flag": True}, notebook="Work")
    con = db.connect(env.db_path)
    assert con.execute("SELECT research FROM notes").fetchone()["research"] == ""
    assert research.work_pending() == 0


def test_research_run_writes_brief_note_and_pdf(env, monkeypatch):
    from ozu import agent, db, research
    process_one(env, monkeypatch, research_result())
    seen = {}

    def fake_run(prompt, workdir, allowed_tools=None, model=None, timeout=None, writes=False):
        seen["tools"] = allowed_tools
        seen["cwd"] = workdir
        seen["prompt"] = prompt
        seen["model"] = model
        return BRIEF

    monkeypatch.setattr(agent, "_run_claude", fake_run)
    assert research.work_pending() == 1

    # read-only researcher, working inside the vault, fed the note body
    assert "Write" not in seen["tools"] and "Bash" not in seen["tools"]
    assert seen["cwd"] == env.vault_dir
    assert "Freemium" in seen["prompt"]
    # D-10: research reads arbitrary web pages and argues against his own
    # notes, so it is deliberately NOT rightsized down — no model asked for
    # means the CLI's own default, as it always has.
    assert seen["model"] is None and env.run_models["research"] == ""
    # …and its sight is pinned to the vault. A research run reads arbitrary web
    # pages, so an unscoped Read is a route from a poisoned page to ~/ozu-data/.env.
    scope = "//" + os.path.abspath(env.vault_dir).lstrip("/") + "/**"
    assert f"Read({scope})" in seen["tools"]
    assert "Read," not in seen["tools"] + ","

    con = db.connect(env.db_path)
    src = con.execute("SELECT * FROM notes WHERE research='done'").fetchone()
    assert src and "research: done" in Path(src["path"]).read_text()

    companion = next((env.vault_dir / "topics" / "kettle").glob("*research*.md"))
    text = companion.read_text()
    assert "pantry scanning" in text and f"[[{src['id']}]]" in text
    assert "not configured" in text  # no Send-to-Kindle in tests → said honestly
    edge = con.execute("SELECT reason FROM edges WHERE dst=?", (src["id"],)).fetchone()
    assert edge["reason"] == "research brief"

    pdf = env.research_dir / f"Ozu Research {src['id']}.pdf"
    assert pdf.exists()
    doc = fitz.open(pdf)
    pdf_text = "".join(textlayer.extract(p) for p in doc)
    doc.close()
    assert "TL;DR" in pdf_text and "example.com" in pdf_text

    assert research.work_pending() == 0  # one run per note, ever


def test_research_failure_marked_error_and_terminal(env, monkeypatch):
    from ozu import agent, db, research
    process_one(env, monkeypatch, research_result())

    def boom(*a, **k):
        raise RuntimeError("claude exited 1")

    monkeypatch.setattr(agent, "_run_claude", boom)
    assert research.work_pending() == 1
    con = db.connect(env.db_path)
    assert con.execute("SELECT research FROM notes").fetchone()["research"] == "error"
    assert research.work_pending() == 0


def test_research_ledger_survives_index_rebuild(env, monkeypatch):
    from ozu import agent, indexer, research
    process_one(env, monkeypatch, research_result())
    monkeypatch.setattr(agent, "_run_claude",
                        lambda *a, **k: BRIEF)
    research.work_pending()
    env.db_path.unlink()
    indexer.rebuild()
    assert research.work_pending() == 0


def test_digest_surfaces_research(env, monkeypatch):
    from ozu import agent, digest, research
    process_one(env, monkeypatch, research_result())
    monkeypatch.setattr(agent, "_run_claude", lambda *a, **k: BRIEF)
    research.work_pending()
    text = digest.build_digest()
    assert "Research briefs (@research):" in text
    assert "Research: Kettle freemium pricing" in text


def test_auth_failure_leaves_note_queued_not_error(env, monkeypatch):
    """An expired claude.ai session must not burn the one-run ledger: the
    note stays requested and runs once auth is fixed."""
    from ozu import agent, db, research
    process_one(env, monkeypatch, research_result())

    def expired(*a, **k):
        raise agent.AuthError("Failed to authenticate: OAuth session expired")

    monkeypatch.setattr(agent, "_run_claude", expired)
    assert research.work_pending() == 1  # attempted, not completed
    con = db.connect(env.db_path)
    assert con.execute("SELECT research FROM notes").fetchone()["research"] == "requested"


def test_run_claude_reads_error_from_stdout_json(env, monkeypatch, tmp_path):
    """The CLI reports failures in its stdout JSON, not stderr — found live
    when an auth failure surfaced as 'claude exited 1: <nothing>'."""
    import json as j
    from ozu import agent

    def fake_run(cmd, **kw):
        class P:
            returncode = 1
            stdout = j.dumps({"result": "Failed to authenticate: OAuth session "
                                        "expired and could not be refreshed",
                              "terminal_reason": "api_error", "subtype": "success"})
            stderr = ""
        return P()

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    try:
        agent._run_claude("x", tmp_path)
        assert False, "should have raised"
    except agent.AuthError as e:
        assert "authenticate" in str(e)

    def fake_run2(cmd, **kw):
        class P:
            returncode = 1
            stdout = j.dumps({"result": "max turns exceeded"})
            stderr = ""
        return P()

    monkeypatch.setattr(agent.subprocess, "run", fake_run2)
    try:
        agent._run_claude("x", tmp_path)
        assert False, "should have raised"
    except agent.AuthError:
        assert False, "a plain failure is not an auth failure"
    except RuntimeError as e:
        assert "max turns exceeded" in str(e)
