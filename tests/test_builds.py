"""The paper build loop, end to end with the vision call and the headless CLI
mocked (git is real; a local bare repo stands in for GitHub, as in
test_agent).

Covers: the directive arriving through the page JSON; propose → revise →
approve → execute → done as vault-note state; one execution per approval,
ever; three-key matching (file name / printed text layer / build_ref) with a
disagreeing case filed as needs_review; the Build id present in the text
layer of every PDF page; model routing (@fable → claude-fable-5-1, default →
claude-opus-5); the tick lock; the governance wall; the daily vision cap.
"""
import json
import subprocess
import sys
from pathlib import Path

import fitz
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from test_pipeline import env, make_pdf, MOCK_RESULT  # noqa: F401 — shared fixture
from test_agent import GIT_ID, make_origin, map_repo  # noqa: F401

PROPOSAL = """# What I understood
A weekly Sunday "week ahead" note, generated automatically.

# Requirement
Every Sunday a note summarising the week ahead is created.

# Plan
1. Add a generator module.
2. Wire it to the scheduler.

# Files I expect to touch
- src/weekly.py — the generator

# Risks
- Timezone of "Sunday".

# What I will not do
- No UI beyond the note itself.

# Open questions
None."""


def propose_page_result(**note_extra):
    """A fresh notebook page carrying @claude @kettle: propose directive plus
    claude_request on the note."""
    r = dict(MOCK_RESULT)
    r["build_directive"] = {"kind": "propose", "model": None}
    r["notes"] = [dict(MOCK_RESULT["notes"][0], claude_request=True,
                       actions=[], **note_extra)]
    return r


def annotation_result(kind, model=None, build_ref=None, transcription="",
                      notes=(), completions=()):
    return {
        "transcription": transcription, "drawing_description": "", "mermaid": "",
        "confidence": 0.95, "work_flag": False, "completions": list(completions),
        "build_directive": {"kind": kind, "model": model},
        "build_ref": build_ref, "notes": list(notes),
    }


@pytest.fixture()
def sent(monkeypatch):
    """Record every PDF handed to send_kindle without touching SMTP."""
    from ozu import send_kindle
    box = []
    monkeypatch.setattr(send_kindle, "send_pdf",
                        lambda p: box.append(Path(p)) or True)
    return box


@pytest.fixture()
def runs(monkeypatch):
    """Stand-in for every headless run. Records (allowed_tools, model,
    timeout); a run with a model (an execution) commits into its clone, a
    run without one (propose/revise) returns the proposal text."""
    from ozu import agent
    calls = []

    def fake_run(prompt, workdir, allowed_tools=None, model=None, timeout=None, writes=False):
        calls.append({"prompt": prompt, "workdir": Path(workdir),
                      "allowed_tools": allowed_tools, "model": model,
                      "timeout": timeout})
        if model:
            (Path(workdir) / "weekly.py").write_text("print('week ahead')\n")
            subprocess.run(["git", "-C", str(workdir), "add", "-A"], check=True)
            subprocess.run(["git", *GIT_ID, "-C", str(workdir), "commit", "-q",
                            "-m", "build"], check=True)
            return "Built the weekly note generator as approved."
        return PROPOSAL
    monkeypatch.setattr(agent, "_run_claude", fake_run)
    return calls


def process_page(env, monkeypatch, result, path_name="a.pdf",
                 notebook_dir="Inbox", text="x", at_root=False):
    from ozu import ingest, understand
    pdf = (env.raw_dir / path_name if at_root
           else env.raw_dir / notebook_dir / path_name)
    make_pdf(pdf, [text])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: result)
    understand.process_pending()


def make_build(env, monkeypatch, sent, runs, tmp_path, origin=None):
    """One proposed Build for topic kettle; returns its id."""
    from ozu import builds
    if origin is None:
        origin = make_origin(tmp_path)
    process_page(env, monkeypatch, propose_page_result())
    map_repo(monkeypatch, origin)
    assert builds.run_pending() == 1
    note = next((env.vault_dir / "topics" / "kettle").glob("B-*.md"))
    return note.stem, origin


def annotate(env, monkeypatch, bid, result, rev="r1", name=None):
    """A returning annotated page of the Build's own PDF: the file name and
    the printed text layer both carry the id, as the real document does.
    The transcription is baked into the page body so each annotation is a
    genuinely new bitmap — dedup is per page, and a page identical to one
    already seen is (correctly) never re-read."""
    header = f"Ozu Build {bid} {rev}"
    uniq = (result.get("transcription") or "clean") + str(result.get("work_flag"))
    process_page(env, monkeypatch, result,
                 path_name=name or f"{header}  - Kindle.pdf",
                 text=f"{header}\nprinted proposal text\n[{uniq}]", at_root=True)


# ---------------------------------------------------------------------------
# directive parsing / schema

def test_schema_carries_the_build_directive(env):
    from ozu import understand
    props = understand.PAGE_SCHEMA["properties"]
    assert "build_directive" in props and "build_ref" in props
    assert set(understand.PAGE_SCHEMA["required"]) >= {"build_directive", "build_ref"}
    kinds = props["build_directive"]["properties"]["kind"]["enum"]
    assert set(kinds) == {"propose", "input", "approve", "drop", "none"}
    # anyOf [string-enum, null] — the API 400s a type-union-plus-enum form
    model = props["build_directive"]["properties"]["model"]
    branches = model["anyOf"]
    assert {"type": "null"} in branches
    strings = next(b for b in branches if b.get("type") == "string")
    assert set(strings["enum"]) == {"opus", "sonnet", "fable"}
    assert "enum" not in model and "type" not in model


# ---------------------------------------------------------------------------
# propose

def test_claude_with_project_tag_proposes_instead_of_building(
        env, monkeypatch, sent, runs, tmp_path):
    from ozu import agent, db, okf
    bid, origin = make_build(env, monkeypatch, sent, runs, tmp_path)

    note = env.vault_dir / "topics" / "kettle" / f"{bid}.md"
    text = note.read_text()
    assert "build: proposed" in text and "build_rev: 1" in text
    assert "## Proposal (r1)" in text and "## Original ink" in text

    # the source note's ledger line points at the Build — and the old
    # one-shot path (claude='requested') can never fire on it
    src = next(p for p in (env.vault_dir / "topics" / "kettle").glob("*.md")
               if p.stem != bid and p.name != "index.md")
    assert f"claude: proposed-build {bid}" in src.read_text()
    assert agent.work_pending() == 0

    # the proposal PDF went to the Scribe, named for the idea first and the
    # Build last (the library grid truncates from the right)
    from ozu import builds
    assert sent and sent[-1].name == builds.pdf_name(bid, 1, "Kettle freemium pricing") + ".pdf"
    assert builds.ID_RE.search(sent[-1].stem).group(0) == bid
    # the run was read-only: no Edit, no Bash, web allowed
    tools = runs[0]["allowed_tools"]
    assert "Edit" not in tools and "Bash" not in tools
    assert "WebSearch" in tools and "Read(" in tools
    # the vault stays a conformant bundle with a Build note in it
    errors, _ = okf.check(env.vault_dir)
    assert errors == []
    con = db.connect(env.db_path)
    row = con.execute("SELECT * FROM builds").fetchone()
    assert row["id"] == bid and row["status"] == "proposed" and row["rev"] == 1


def test_unmapped_topic_still_proposes_from_the_vault(env, monkeypatch, sent, runs):
    """@claude with no mapped repo (including the bare-@claude 'ideas' case)
    proposes anyway; the run reads only the vault and the prompt says to ask
    which repo."""
    from ozu import builds
    r = propose_page_result(topic="ideas")
    process_page(env, monkeypatch, r)
    assert builds.run_pending() == 1
    assert list((env.vault_dir / "topics" / "ideas").glob("B-*.md"))
    assert "No repository is mapped" in runs[0]["prompt"]
    assert str(env.vault_dir) in runs[0]["allowed_tools"]


# ---------------------------------------------------------------------------
# ISA-shaped proposals: projects named in OZU_ISA_TOPICS get proposals shaped
# as an ISA (ideal state / decisions / numbered steps each a check), and their
# execution grades the report step by step. Off unless named, so every other
# project keeps the usual shape and the two can be compared.

ISA_HEADING = "# Ideal state"
PROPOSE_HEADING = "# What I understood"
ISA_GRADING_PHRASE = "pass, fail or not verified"


def isa(monkeypatch, topics="kettle"):
    import ozu.config as config
    monkeypatch.setenv("OZU_ISA_TOPICS", topics)
    config._config = None


def test_isa_topic_gets_the_isa_proposal_prompt(env, monkeypatch, sent, runs, tmp_path):
    isa(monkeypatch)
    make_build(env, monkeypatch, sent, runs, tmp_path)
    prompt = runs[0]["prompt"]
    assert ISA_HEADING in prompt and "# Steps" in prompt
    assert PROPOSE_HEADING not in prompt


def test_other_topic_keeps_the_usual_proposal_prompt(env, monkeypatch, sent, runs, tmp_path):
    isa(monkeypatch, "garden")
    make_build(env, monkeypatch, sent, runs, tmp_path)
    prompt = runs[0]["prompt"]
    assert PROPOSE_HEADING in prompt and "# Plan" in prompt
    assert ISA_HEADING not in prompt


def test_isa_topics_are_off_unless_named(env, monkeypatch):
    import ozu.config as config
    monkeypatch.delenv("OZU_ISA_TOPICS", raising=False)
    config._config = None
    assert config.get_config().isa_topics == ()
    isa(monkeypatch, " Kettle, garden ,")
    assert config.get_config().isa_topics == ("kettle", "garden")


def test_unset_isa_topics_keep_the_usual_proposal(env, monkeypatch, sent, runs, tmp_path):
    monkeypatch.delenv("OZU_ISA_TOPICS", raising=False)
    make_build(env, monkeypatch, sent, runs, tmp_path)
    prompt = runs[0]["prompt"]
    assert PROPOSE_HEADING in prompt and ISA_HEADING not in prompt


def test_isa_build_work_prompt_carries_the_grading_rule(
        env, monkeypatch, sent, runs, tmp_path):
    from ozu import builds
    isa(monkeypatch)
    bid, _ = make_build(env, monkeypatch, sent, runs, tmp_path)
    annotate(env, monkeypatch, bid,
             annotation_result("approve", build_ref=bid, transcription="@approved"))
    assert builds.run_pending() == 1
    work_prompt = runs[-1]["prompt"]
    assert ISA_GRADING_PHRASE in work_prompt
    assert "test suite" in work_prompt.lower()


def test_other_topic_work_prompt_has_no_grading_rule(env, monkeypatch, sent, runs, tmp_path):
    from ozu import builds
    bid, _ = make_build(env, monkeypatch, sent, runs, tmp_path)
    annotate(env, monkeypatch, bid,
             annotation_result("approve", build_ref=bid, transcription="@approved"))
    assert builds.run_pending() == 1
    assert ISA_GRADING_PHRASE not in runs[-1]["prompt"]


# ---------------------------------------------------------------------------
# matching

def test_match_build_keys(env):
    from ozu.builds import match_build
    a, b = "B-260906-7f3a", "B-260906-1111"
    assert match_build(f"Ozu Build {a} r2", f"Ozu Build {a} r2", a) == \
        (a, "build_ref+filename+textlayer")
    assert match_build(f"Ozu Build {a} r2", "", None) == (a, "filename")
    # the stamp is a line of its own in the text layer, wherever it falls
    assert match_build("", f"printed text\nOzu Build {a} r1\nmore", None) == \
        (a, "textlayer")
    assert match_build("Ozu Mail 2026-09-05", "", a) == (a, "build_ref")
    assert match_build("Ozu Mail 2026-09-05", "", None) == (None, "no key")
    # disagreement is never a match
    got, why = match_build(f"Ozu Build {a} r2", "", b)
    assert got is None and "disagree" in why
    # a bare mention in printed text (a mail line) is not the header key
    assert match_build("Ozu Mail x", f"the build {a} was dropped", None) == \
        (None, "no key")


def test_disagreeing_keys_file_needs_review_not_a_match(
        env, monkeypatch, sent, runs, tmp_path):
    from ozu import db
    bid, _ = make_build(env, monkeypatch, sent, runs, tmp_path)
    other = "B-260906-0000"
    annotate(env, monkeypatch, bid,
             annotation_result("approve", build_ref=other,
                               transcription="@approved — go ahead with this"))
    con = db.connect(env.db_path)
    # the Build did not move
    assert con.execute("SELECT status FROM builds").fetchone()["status"] == "proposed"
    # the ink survived as a needs_review note and the mail will say so
    row = con.execute("SELECT * FROM notes WHERE title LIKE 'Unmatched%'").fetchone()
    assert row and row["needs_review"] == 1
    ev = db.unreported_build_events(con)
    assert ev and "could not be matched" in ev[0]["line"]


def test_margin_ink_links_to_the_build(env, monkeypatch, sent, runs, tmp_path):
    from ozu import db
    bid, _ = make_build(env, monkeypatch, sent, runs, tmp_path)
    margin = {"title": "Margin: also snooze Saturdays", "slug": "snooze-saturdays",
              "topic": "kettle", "tags": ["kettle"], "summary": "Margin thought.",
              "body": "Re: the plan — Saturdays too.", "related": [],
              "related_reason": "", "actions": [], "claude_request": False,
              "research_request": False}
    annotate(env, monkeypatch, bid,
             annotation_result("none", build_ref=bid, notes=[margin]))
    con = db.connect(env.db_path)
    note = con.execute("SELECT * FROM notes WHERE title LIKE 'Margin%'").fetchone()
    assert note is not None
    edge = con.execute("SELECT * FROM edges WHERE src=? AND dst=?",
                       (note["id"], bid)).fetchone()
    assert edge is not None
    assert f"[{bid}]" in Path(note["path"]).read_text()  # related: [... B-…]


# ---------------------------------------------------------------------------
# revise / approve / execute / drop

def test_input_revises_and_sends_r2(env, monkeypatch, sent, runs, tmp_path):
    from ozu import builds, db
    bid, _ = make_build(env, monkeypatch, sent, runs, tmp_path)
    annotate(env, monkeypatch, bid,
             annotation_result("input", build_ref=bid,
                               transcription="@input keep it under 10 lines"))
    note = env.vault_dir / "topics" / "kettle" / f"{bid}.md"
    assert "build_pending: revise" in note.read_text()
    assert "keep it under 10 lines" in note.read_text()

    assert builds.run_pending() == 1
    text = note.read_text()
    assert "build: revised" in text and "build_rev: 2" in text
    assert "## Proposal (r2)" in text and "build_pending" not in text
    assert sent[-1].name == builds.pdf_name(bid, 2, "Kettle freemium pricing") + ".pdf"
    # the revision run saw the input as a decision
    assert "keep it under 10 lines" in runs[-1]["prompt"]
    con = db.connect(env.db_path)
    assert con.execute("SELECT rev FROM builds").fetchone()["rev"] == 2


def test_approve_executes_once_on_branch_and_reports(
        env, monkeypatch, sent, runs, tmp_path):
    from ozu import builds, db
    bid, origin = make_build(env, monkeypatch, sent, runs, tmp_path)
    annotate(env, monkeypatch, bid,
             annotation_result("approve", model="sonnet", build_ref=bid,
                               transcription="@approved @sonnet"))
    note = env.vault_dir / "topics" / "kettle" / f"{bid}.md"
    assert "build: approved" in note.read_text()
    assert "build_model: sonnet" in note.read_text()

    assert builds.run_pending() == 1
    text = note.read_text()
    assert "build: done" in text and f"build_branch: build/{bid}" in text
    assert "## Result" in text
    # the branch reached the origin; main did not move
    branches = subprocess.run(["git", "-C", str(origin), "branch"],
                              capture_output=True, text=True).stdout
    assert f"build/{bid}" in branches
    # the execution used the approved model id
    assert runs[-1]["model"] == "claude-sonnet-5"
    # the work prompt is the approved proposal + decision history + quoted
    # ink — not the raw ink alone
    p = runs[-1]["prompt"]
    assert "Every Sunday a note summarising the week ahead" in p  # requirement
    assert "approved — " in p and "sonnet" in p                   # history
    assert "> Freemium with pantry scan" in p                     # quoted ink
    # a ☐ Review action landed in the ledger; the result is NOT a document
    # of its own any more — it rides the next edition (test_edition)
    con = db.connect(env.db_path)
    act = con.execute("SELECT text FROM actions WHERE note_id=?", (bid,)).fetchone()
    assert act and act["text"].startswith("Review ")
    assert not any("result" in p.name for p in sent)
    from ozu import edition
    assert edition.send("extra") is True
    assert sent[-1].name.startswith("Ozu Extra ")

    # one execution per approval, ever: a second @approved is a mail line.
    # The page comes back from inside an edition, whose filename names no
    # Build — the printed result header is what routes it.
    n_runs = len(runs)
    annotate(env, monkeypatch, bid,
             annotation_result("approve", build_ref=bid,
                               transcription="@approved"), rev="result",
             name="Ozu Extra 2026-09-11 1200 (1)  - Kindle.pdf")
    assert builds.run_pending() == 0
    assert len(runs) == n_runs
    ev = db.unreported_build_events(db.connect(env.db_path))
    assert any("second @approved never re-runs" in e["line"] for e in ev)


def test_input_and_approve_on_one_page_applies_input_first(
        env, monkeypatch, sent, runs, tmp_path):
    from ozu import builds
    bid, _ = make_build(env, monkeypatch, sent, runs, tmp_path)
    annotate(env, monkeypatch, bid,
             annotation_result("approve", model="fable", build_ref=bid,
                               transcription="@input also skip holidays @approved @fable"))
    note = (env.vault_dir / "topics" / "kettle" / f"{bid}.md").read_text()
    assert "applied before approval" in note
    assert "also skip holidays" in note
    assert "build: approved" in note and "build_model: fable" in note
    builds.run_pending()
    assert runs[-1]["model"] == "claude-fable-5-1"
    assert "also skip holidays" in runs[-1]["prompt"]


def test_default_model_is_opus(env, monkeypatch, sent, runs, tmp_path):
    from ozu import builds
    bid, _ = make_build(env, monkeypatch, sent, runs, tmp_path)
    annotate(env, monkeypatch, bid,
             annotation_result("approve", build_ref=bid, transcription="@approved"))
    builds.run_pending()
    assert runs[-1]["model"] == "claude-opus-5"


def test_drop_closes_without_executing(env, monkeypatch, sent, runs, tmp_path):
    from ozu import builds, db
    bid, _ = make_build(env, monkeypatch, sent, runs, tmp_path)
    n_pdfs = len(sent)
    annotate(env, monkeypatch, bid,
             annotation_result("drop", build_ref=bid, transcription="@drop"))
    note = (env.vault_dir / "topics" / "kettle" / f"{bid}.md").read_text()
    assert "build: dropped" in note
    con = db.connect(env.db_path)
    ev = db.unreported_build_events(con)
    assert any("dropped — nothing was executed" in e["line"] for e in ev)
    assert builds.run_pending() == 0      # nothing executes
    assert len(sent) == n_pdfs            # and no PDF answers a drop
    # ink on a dropped build changes nothing
    annotate(env, monkeypatch, bid,
             annotation_result("approve", build_ref=bid,
                               transcription="@approved"), rev="r1",
             name=f"Ozu Build {bid} r1 (1)  - Kindle.pdf")
    assert "build: dropped" in (env.vault_dir / "topics" / "kettle" / f"{bid}.md").read_text()


def test_failed_execution_is_error_and_reapprovable(
        env, monkeypatch, sent, runs, tmp_path):
    from ozu import agent, builds, db
    bid, origin = make_build(env, monkeypatch, sent, runs, tmp_path)
    annotate(env, monkeypatch, bid,
             annotation_result("approve", build_ref=bid, transcription="@approved"))

    def boom(prompt, workdir, allowed_tools=None, model=None, timeout=None, writes=False):
        raise RuntimeError("claude exited 1")
    monkeypatch.setattr(agent, "_run_claude", boom)
    builds.run_pending()
    note = env.vault_dir / "topics" / "kettle" / f"{bid}.md"
    assert "build: error" in note.read_text()
    ev = db.unreported_build_events(db.connect(env.db_path))
    assert any("execution failed" in e["line"] for e in ev)

    # a fresh @approved re-arms it, and a working run finishes the job
    def ok_run(prompt, workdir, allowed_tools=None, model=None, timeout=None, writes=False):
        (Path(workdir) / "f.py").write_text("x\n")
        subprocess.run(["git", "-C", str(workdir), "add", "-A"], check=True)
        subprocess.run(["git", *GIT_ID, "-C", str(workdir), "commit", "-q",
                        "-m", "retry"], check=True)
        return "done on retry"
    monkeypatch.setattr(agent, "_run_claude", ok_run)
    annotate(env, monkeypatch, bid,
             annotation_result("approve", build_ref=bid,
                               transcription="@approved retry"), rev="r1",
             name=f"Ozu Build {bid} r1 (2)  - Kindle.pdf")
    assert "build: approved" in note.read_text()
    assert builds.run_pending() == 1
    assert "build: done" in note.read_text()


def test_ink_after_execution_answers_with_mail_lines_only(
        env, monkeypatch, sent, runs, tmp_path):
    """@input and @drop on a done Build change nothing; each earns a line."""
    from ozu import builds, db
    bid, _ = make_build(env, monkeypatch, sent, runs, tmp_path)
    annotate(env, monkeypatch, bid,
             annotation_result("approve", build_ref=bid, transcription="@approved"))
    builds.run_pending()
    note = env.vault_dir / "topics" / "kettle" / f"{bid}.md"
    assert "build: done" in note.read_text()

    annotate(env, monkeypatch, bid,
             annotation_result("input", build_ref=bid,
                               transcription="@input one more thing"),
             rev="result")
    annotate(env, monkeypatch, bid,
             annotation_result("drop", build_ref=bid, transcription="@drop"),
             rev="result", name=f"Ozu Build {bid} result (1)  - Kindle.pdf")
    assert "build: done" in note.read_text()      # still done, untouched
    lines = [e["line"] for e in
             db.unreported_build_events(db.connect(env.db_path))]
    assert any("@input arrived after execution" in ln for ln in lines)
    assert any("@drop changes nothing" in ln for ln in lines)


def test_stale_executing_is_swept_to_error_and_reapprovable(
        env, monkeypatch, sent, runs, tmp_path):
    """A Build left 'executing' by a dead process must not refuse @approved
    forever: the next builds phase flips it to error, and ink re-arms it."""
    from ozu import builds, db
    bid, _ = make_build(env, monkeypatch, sent, runs, tmp_path)
    b = builds.load_build(env.vault_dir / "topics" / "kettle" / f"{bid}.md")
    b["fm"]["build"] = "executing"                # simulate the dead run
    builds._save_build(b, "test: wedge")
    assert builds.run_pending() == 0              # nothing to run, but…
    note = env.vault_dir / "topics" / "kettle" / f"{bid}.md"
    assert "build: error" in note.read_text()
    lines = [e["line"] for e in
             db.unreported_build_events(db.connect(env.db_path))]
    assert any("died mid-flight" in ln for ln in lines)
    annotate(env, monkeypatch, bid,
             annotation_result("approve", build_ref=bid,
                               transcription="@approved again"))
    assert "build: approved" in note.read_text()


def test_auth_error_leaves_the_build_approved(env, monkeypatch, sent, runs, tmp_path):
    from ozu import agent, builds
    bid, _ = make_build(env, monkeypatch, sent, runs, tmp_path)
    annotate(env, monkeypatch, bid,
             annotation_result("approve", build_ref=bid, transcription="@approved"))

    def auth_dead(prompt, workdir, allowed_tools=None, model=None, timeout=None, writes=False):
        raise agent.AuthError("please authenticate")
    monkeypatch.setattr(agent, "_run_claude", auth_dead)
    builds.run_pending()                  # swallows AuthError, leaves queued
    assert "build: approved" in (env.vault_dir / "topics" / "kettle"
                                 / f"{bid}.md").read_text()


# ---------------------------------------------------------------------------
# governance, ledgers, documents

def test_walled_page_never_touches_a_build(env, monkeypatch, sent, runs, tmp_path):
    from ozu import db
    bid, _ = make_build(env, monkeypatch, sent, runs, tmp_path)
    r = annotation_result("approve", build_ref=bid, transcription="@approved")
    r["work_flag"] = True
    annotate(env, monkeypatch, bid, r)
    assert "build: proposed" in (env.vault_dir / "topics" / "kettle"
                                 / f"{bid}.md").read_text()
    con = db.connect(env.db_path)
    assert con.execute("SELECT status FROM builds").fetchone()["status"] == "proposed"


def test_builds_table_is_derived_and_rebuilt(env, monkeypatch, sent, runs, tmp_path):
    from ozu import db, indexer
    bid, _ = make_build(env, monkeypatch, sent, runs, tmp_path)
    annotate(env, monkeypatch, bid,
             annotation_result("approve", model="fable", build_ref=bid,
                               transcription="@approved @fable"))
    env.db_path.unlink()
    indexer.rebuild()
    con = db.connect(env.db_path)
    row = con.execute("SELECT * FROM builds").fetchone()
    assert row["id"] == bid and row["status"] == "approved"
    assert row["model"] == "fable"


def test_build_id_in_the_text_layer_of_every_page(env, tmp_path):
    from ozu import digest_pdf, textlayer
    long_proposal = PROPOSAL + "\n" + "\n".join(
        f"- consideration {i}: " + "detail " * 20 for i in range(120))
    out = tmp_path / "b.pdf"
    digest_pdf.build_propose_pdf(
        {"id": "B-260906-7f3a", "title": "Weekly note", "topic": "atlas",
         "rev": 2, "proposal": long_proposal, "history": "### r1 proposed — x",
         "date": "2026-09-06"}, out)
    doc = fitz.open(out)
    assert doc.page_count >= 2, "the case must exercise a multi-page document"
    for page in doc:
        assert "Ozu Build B-260906-7f3a r2" in textlayer.extract(page)
    doc.close()
    # and the decision box prints the exact words to write
    text = "".join(textlayer.extract(p) for p in fitz.open(out))
    for words in ("@approved", "@input", "@drop", "@fable"):
        assert words in text


def test_result_pdf_prints_a_real_action_box(env, tmp_path):
    from ozu import digest_pdf, textlayer
    out = tmp_path / "r.pdf"
    digest_pdf.build_result_pdf(
        {"id": "B-260906-7f3a", "title": "Weekly note", "topic": "atlas",
         "model": "sonnet", "report": "Built.", "review_url":
         "https://github.com/o/r/pull/7", "history": "### approved — x",
         "actions": ["Review PR #7 — Weekly note"], "date": "2026-09-06"}, out)
    text = "".join(textlayer.extract(p) for p in fitz.open(out))
    assert "☐ Review PR #7" in text        # extraction collapses the spacing
    assert "Ozu Build B-260906-7f3a result" in text


def test_mail_carries_build_events_and_skips_build_notes(
        env, monkeypatch, sent, runs, tmp_path):
    from ozu import db, mail
    bid, _ = make_build(env, monkeypatch, sent, runs, tmp_path)
    con = db.connect(env.db_path)
    mail._ensure_ledger(con)
    con.execute("INSERT OR IGNORE INTO mailed SELECT id FROM notes")  # only events left
    con.commit()
    db.add_build_event(con, bid, f"{bid} (Weekly): dropped — nothing was executed.")
    data = mail.gather(con)
    assert data and any("dropped" in line for line in data["builds"])
    assert bid not in data["ids"], "Build notes travel as their own PDFs, not in mail"


def bid_of(pdf: Path) -> str:
    from ozu import builds
    return builds.ID_RE.search(pdf.stem).group(0)


def test_simulate_renders_without_touching_the_vault(env, monkeypatch, sent, runs):
    from ozu import builds
    process_page(env, monkeypatch, dict(MOCK_RESULT))
    from ozu import db
    con = db.connect(env.db_path)
    nid = con.execute("SELECT id FROM notes").fetchone()["id"]
    n_files = len(list(env.vault_dir.rglob("*.md")))
    pdf = builds.simulate(nid)
    assert pdf.exists() and pdf.name.startswith("Ozu Build r1 - ") and bid_of(pdf) in pdf.name
    assert len(list(env.vault_dir.rglob("*.md"))) == n_files
    con2 = db.connect(env.db_path)
    assert con2.execute("SELECT COUNT(*) c FROM builds").fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# the tick

def test_pipeline_lock_is_exclusive(env):
    from ozu import pipeline
    with pipeline.hold_lock() as held:
        assert held is True
        with pipeline.hold_lock() as second:
            assert second is False, "two pipeline walks must never overlap"
    with pipeline.hold_lock() as again:
        assert again is True


def test_model_and_timeout_reach_the_cli(env, monkeypatch, tmp_path):
    from ozu import agent
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["timeout"] = cmd, kw.get("timeout")

        class P:
            returncode = 0
            stdout = json.dumps({"result": "ok", "is_error": False})
            stderr = ""
        return P()

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    agent._run_claude("p", tmp_path, model="claude-fable-5-1", timeout=4242)
    i = seen["cmd"].index("--model")
    assert seen["cmd"][i + 1] == "claude-fable-5-1"
    assert seen["timeout"] == 4242
    # and without a model, no --model flag at all (the CLI's own default)
    agent._run_claude("p", tmp_path)
    assert "--model" not in seen["cmd"]


def test_daily_vision_cap_holds_the_queue(env, monkeypatch):
    from ozu import ingest, understand
    make_pdf(env.raw_dir / "Inbox" / "two.pdf", ["page one", "page two"])
    ingest.ingest()
    monkeypatch.setenv("OZU_MAX_PAGES_PER_DAY", "1")
    import ozu.config as config
    config._config = None
    monkeypatch.setattr(understand, "analyze_page",
                        lambda *a, **k: dict(MOCK_RESULT))
    assert understand.process_pending() == 1
    assert understand.process_pending() == 0, "the day's budget is spent"


# ---------------------------------------------------------------------------
# the attempt budget (2026-09-21)
#
# The night of 2026-09-20 is the test corpus: build B-260916-2824 was
# relaunched 116 times between 20:36 and 06:31 under a per-tick cap of 2 that
# was never once exceeded. Three launches burned the whole claude.ai usage
# window; 113 bounced off the spent window in two seconds. Nothing was built
# and nothing was recorded. Each test below pins one half of why.

# The CLI's own refusal, copied from the transcript of one of those runs.
SESSION_LIMIT = "You've hit your session limit · resets 6:30am (UTC)"


def set_caps(monkeypatch, attempts=None, deferrals=None):
    import ozu.config as config
    if attempts is not None:
        monkeypatch.setenv("OZU_MAX_BUILD_ATTEMPTS", str(attempts))
    if deferrals is not None:
        monkeypatch.setenv("OZU_MAX_BUILD_DEFERRALS", str(deferrals))
    config._config = None
    return config.get_config()


def limit_run(spent=True, launches=None):
    """A headless run that always dies on the session limit."""
    from ozu import agent

    def run(prompt, workdir, allowed_tools=None, model=None, timeout=None, writes=False):
        if launches is not None:
            launches.append(Path(workdir).name)
        raise agent.UsageLimitError(
            SESSION_LIMIT,
            resets_at=agent._resets_at(SESSION_LIMIT), spent=spent)
    return run


def reopen_window(env):
    """The usage window comes back: the phase-wide stand-down expires."""
    from ozu import builds, db
    con = db.connect(env.db_path)
    db.set_meta(con, builds.RETRY_AFTER, "")


def build_row(env, bid):
    from ozu import db
    return db.connect(env.db_path).execute(
        "SELECT * FROM builds WHERE id=?", (bid,)).fetchone()


def approved_build(env, monkeypatch, sent, runs, tmp_path):
    """One Build, approved in ink and waiting for the builds phase."""
    bid, origin = make_build(env, monkeypatch, sent, runs, tmp_path)
    annotate(env, monkeypatch, bid,
             annotation_result("approve", build_ref=bid,
                               transcription="@approved"))
    return bid, origin


def test_a_repeatedly_failing_build_cannot_exceed_its_cap(
        env, monkeypatch, sent, runs, tmp_path):
    """The headline: however many ticks fire, and however often the window
    comes back, one Build is launched at most OZU_MAX_BUILD_ATTEMPTS times in
    its whole life — then it is `exhausted`, which no phase of the tick
    selects, so it stops being work."""
    from ozu import agent, builds, db
    set_caps(monkeypatch, attempts=3)
    bid, _ = approved_build(env, monkeypatch, sent, runs, tmp_path)
    launches = []
    monkeypatch.setattr(agent, "_run_claude", limit_run(spent=True,
                                                        launches=launches))

    for _ in range(40):                       # forty ticks, window back each
        reopen_window(env)                    # time — the 2026-09-20 shape
        builds.run_pending()

    assert len(launches) == 3, (
        f"launched {len(launches)} times under a cap of 3 — this is the "
        f"defect that ran a Build 116 times")
    note = (env.vault_dir / "topics" / "kettle" / f"{bid}.md").read_text()
    assert "build: exhausted" in note
    assert "build_attempts: 3" in note
    row = build_row(env, bid)
    assert row["status"] == "exhausted" and row["attempts"] == 3

    # every launch and every outcome is in the ledger, so the night leaves a
    # trace instead of a Build that looks relaunchable forever
    con = db.connect(env.db_path)
    trace = [e["line"] for e in db.build_trace(con, bid)]
    assert sum(ln.startswith("launch ") for ln in trace) == 3
    assert sum("usage window consumed" in ln for ln in trace) == 3
    # and the owner is told once, on paper, never 116 times
    mailed = [e["line"] for e in db.unreported_build_events(con)]
    assert sum("stopped —" in ln for ln in mailed) == 1


def test_exhausted_refuses_a_fresh_approval_and_the_tick_skips_it(
        env, monkeypatch, sent, runs, tmp_path):
    """`exhausted` is terminal on paper too: a fresh @approved answers with a
    line and re-arms nothing, or the ink would hand the timer its loop back."""
    from ozu import agent, builds, db
    set_caps(monkeypatch, attempts=1)
    bid, _ = approved_build(env, monkeypatch, sent, runs, tmp_path)
    monkeypatch.setattr(agent, "_run_claude", limit_run(spent=True))
    builds.run_pending()
    note = env.vault_dir / "topics" / "kettle" / f"{bid}.md"
    assert "build: exhausted" in note.read_text()

    annotate(env, monkeypatch, bid,
             annotation_result("approve", build_ref=bid,
                               transcription="@approved again"), rev="r1",
             name=f"Ozu Build {bid} r1 (2)  - Kindle.pdf")
    assert "build: exhausted" in note.read_text()
    lines = [e["line"] for e in
             db.unreported_build_events(db.connect(env.db_path))]
    assert any("@approved changes nothing" in ln for ln in lines)

    reopen_window(env)
    assert builds.run_pending() == 0, "the tick must not select an exhausted Build"

    # the hand reset is the way back, and it is bounded again
    assert builds.reset_budget(bid) == "approved"
    assert build_row(env, bid)["attempts"] == 0


def test_a_spent_window_stands_the_phase_down_instead_of_relaunching(
        env, monkeypatch, sent, runs, tmp_path):
    """113 of the 116 relaunches were five-minute retries against a window
    that was already gone. The phase now waits for the reset the CLI named."""
    from ozu import agent, builds, db
    set_caps(monkeypatch, attempts=3)
    bid, _ = approved_build(env, monkeypatch, sent, runs, tmp_path)
    launches = []
    monkeypatch.setattr(agent, "_run_claude", limit_run(spent=True,
                                                        launches=launches))
    for _ in range(30):                       # two and a half hours of ticks
        builds.run_pending()
    assert len(launches) == 1, "the window cannot come back sooner by asking"
    assert build_row(env, bid)["status"] == "approved", "still queued, not failed"

    when = db.get_meta(db.connect(env.db_path), builds.RETRY_AFTER)
    assert when and when.startswith(agent._resets_at(SESSION_LIMIT)
                                    .isoformat()[:16])


def test_a_free_refusal_is_a_deferral_not_an_attempt(
        env, monkeypatch, sent, runs, tmp_path):
    """A run that bounces off an already-spent window in two seconds consumed
    nothing, so it must not spend an attempt — but it is capped too, or the
    Build waits on resets that never help it."""
    from ozu import agent, builds
    set_caps(monkeypatch, attempts=3, deferrals=2)
    bid, _ = approved_build(env, monkeypatch, sent, runs, tmp_path)
    launches = []
    monkeypatch.setattr(agent, "_run_claude", limit_run(spent=False,
                                                        launches=launches))
    for _ in range(20):
        reopen_window(env)
        builds.run_pending()
    assert len(launches) == 2, "bounded by the deferral cap, not the attempt cap"
    row = build_row(env, bid)
    assert row["attempts"] == 0, "a free refusal spends no attempt"
    assert row["deferrals"] == 2 and row["status"] == "exhausted"


def test_a_run_killed_mid_flight_has_already_spent_its_attempt(
        env, monkeypatch, sent, runs, tmp_path):
    """The counter and the launch trace are written and committed *before*
    the subprocess starts. That is what makes the cap survive an OOM, a
    reboot, or the timer being stopped mid-build — the 2026-09-20 corpse was
    still `executing` with branch NULL and nothing recorded anywhere."""
    from ozu import agent, builds, db
    set_caps(monkeypatch, attempts=1)
    bid, _ = approved_build(env, monkeypatch, sent, runs, tmp_path)
    note = env.vault_dir / "topics" / "kettle" / f"{bid}.md"
    seen = {}

    def killed(prompt, workdir, allowed_tools=None, model=None, timeout=None, writes=False):
        seen["note"] = note.read_text()
        seen["trace"] = [e["line"] for e in
                         db.build_trace(db.connect(env.db_path), bid)]
        raise SystemExit("the host went down")

    monkeypatch.setattr(agent, "_run_claude", killed)
    with pytest.raises(SystemExit):
        builds.run_pending()
    assert "build_attempts: 1" in seen["note"], "the attempt is on disk first"
    assert "build: executing" in seen["note"]
    assert any(ln.startswith("launch 1/1") for ln in seen["trace"])

    # the corpse is swept, and with no attempts left it is swept to exhausted
    assert builds.run_pending() == 0
    assert "build: exhausted" in note.read_text()


def test_a_revision_earns_a_fresh_budget(
        env, monkeypatch, sent, runs, tmp_path):
    """The paper way back from `exhausted`: @input is a different plan, and
    it takes handwriting to get there, so it cannot loop on a timer."""
    from ozu import agent, builds
    set_caps(monkeypatch, attempts=1)
    bid, _ = approved_build(env, monkeypatch, sent, runs, tmp_path)
    monkeypatch.setattr(agent, "_run_claude", limit_run(spent=True))
    builds.run_pending()
    note = env.vault_dir / "topics" / "kettle" / f"{bid}.md"
    assert "build: exhausted" in note.read_text()

    annotate(env, monkeypatch, bid,
             annotation_result("input", build_ref=bid,
                               transcription="@input smaller scope please"),
             rev="r1", name=f"Ozu Build {bid} r1 (3)  - Kindle.pdf")
    reopen_window(env)
    monkeypatch.setattr(agent, "_run_claude",
                        lambda *a, **k: PROPOSAL)   # the revision run
    builds.run_pending()
    text = note.read_text()
    assert "build: revised" in text and "build_attempts" not in text


# ---------------------------------------------------------------------------
# reading the CLI's refusal

def test_the_session_limit_is_told_apart_from_a_broken_login(env, monkeypatch,
                                                             tmp_path):
    """Both arrive as an api_error whose story is only in the message. The
    live refusal is the fixture; `spent` comes from the CLI's own numbers."""
    from ozu import agent

    def cli(result, **extra):
        def fake_run(cmd, **kw):
            class P:
                returncode = 1
                stdout = json.dumps({"result": result, "is_error": True,
                                     "terminal_reason": "api_error", **extra})
                stderr = ""
            return P()
        monkeypatch.setattr(agent.subprocess, "run", fake_run)

    # the 113 two-second refusals: no turns, milliseconds — nothing consumed
    cli(SESSION_LIMIT, num_turns=0, duration_ms=2984)
    with pytest.raises(agent.UsageLimitError) as e:
        agent._run_claude("x", tmp_path)
    assert e.value.spent is False and e.value.resets_at.hour == 6

    # the three that ran for hours: the window is gone and the attempt stands
    cli(SESSION_LIMIT, num_turns=321, duration_ms=2_400_000)
    with pytest.raises(agent.UsageLimitError) as e:
        agent._run_claude("x", tmp_path)
    assert e.value.spent is True

    # a broken login is still a plain AuthError — it waits for a human
    cli("Failed to authenticate: OAuth session expired")
    with pytest.raises(agent.AuthError) as e:
        agent._run_claude("x", tmp_path)
    assert not isinstance(e.value, agent.UsageLimitError)

    # and a report that merely discusses rate limiting is not a limit
    cli("Added a rate limit to the API. The session limit handling is tested.",
        num_turns=12)
    with pytest.raises(agent.AuthError) as e:
        agent._run_claude("x", tmp_path)
    assert not isinstance(e.value, agent.UsageLimitError)


def test_reset_time_is_read_from_the_message(env):
    """"resets 6:30am (UTC)" names a wall clock and no date, so the answer is
    the next occurrence of it."""
    import datetime as dt
    from ozu import agent
    now = dt.datetime(2026, 9, 21, 5, 21, tzinfo=dt.timezone.utc)
    assert agent._resets_at(SESSION_LIMIT, now) == dt.datetime(
        2026, 9, 21, 6, 30, tzinfo=dt.timezone.utc)
    # past already today → tomorrow
    assert agent._resets_at("resets 1:30am (UTC)", now) == dt.datetime(
        2026, 9, 22, 1, 30, tzinfo=dt.timezone.utc)
    assert agent._resets_at("resets 8:30pm (UTC)", now) == dt.datetime(
        2026, 9, 21, 20, 30, tzinfo=dt.timezone.utc)
    assert agent._resets_at("You've hit your weekly limit") is None


# ---------------------------------------------------------------------------
# split: one Build too big for a window becomes several (2026-09-21)

def part_file(tmp_path, name, title, body):
    p = tmp_path / name
    p.write_text(f"---\ntitle: {title}\n---\n{body}\n")
    return p


def test_split_carves_a_build_without_a_model_call_or_an_approval(
        env, monkeypatch, sent, runs, tmp_path):
    """The split is text surgery, not a proposal run: no model is asked
    anything, and no successor is born approved — `apply_directive` stays
    the only thing that turns ink into an approval."""
    from ozu import builds, db
    bid, _ = approved_build(env, monkeypatch, sent, runs, tmp_path)
    before_runs, before_sent = len(runs), len(sent)

    parts = [part_file(tmp_path, "one.md", "KT-A — first half",
                       "# Requirement\n\nThe first half, word for word."),
             part_file(tmp_path, "two.md", "KT-B — second half",
                       "# Requirement\n\nThe second half, word for word.")]
    made = builds.split(bid, parts)

    assert len(runs) == before_runs, "a split must not cost a model call"
    assert len(sent) == before_sent + 2, "each successor goes to the Scribe"
    assert len(made) == 2

    parent = (env.vault_dir / "topics" / "kettle" / f"{bid}.md").read_text()
    assert "build: split" in parent
    assert all(m in parent for m in made), "the parent names its successors"

    for i, mid in enumerate(made, 1):
        note = (env.vault_dir / "topics" / "kettle" / f"{mid}.md").read_text()
        assert "build: proposed" in note, "a split hands out no approvals"
        assert f"build_split_from: {bid}" in note
        assert f"Part {i} of 2, carved from [[{bid}]]" in note
        # the parent's decision history and ink travel whole: the answers
        # written beside an approval are decisions, and which part they bind
        # is not the splitter's call
        assert "### approved — " in note and "## Original ink" in note

    # and nothing executes: the tick sees three Builds and runs none of them
    assert builds.run_pending() == 0
    rows = {r["id"]: r["status"] for r in
            db.connect(env.db_path).execute("SELECT id, status FROM builds")}
    assert rows[bid] == "split"
    assert all(rows[m] == "proposed" for m in made)


def test_ink_on_a_split_parent_is_answered_not_applied(
        env, monkeypatch, sent, runs, tmp_path):
    """The parent's document stays on the device for months. Ink on it must
    not reach a Build that will never run — the mail line sends it to the
    successors instead."""
    from ozu import builds, db
    bid, _ = approved_build(env, monkeypatch, sent, runs, tmp_path)
    made = builds.split(bid, [part_file(tmp_path, "p.md", "KT-A — all of it",
                                        "# Requirement\n\nEverything.")])
    note = env.vault_dir / "topics" / "kettle" / f"{bid}.md"

    annotate(env, monkeypatch, bid,
             annotation_result("approve", build_ref=bid,
                               transcription="@approved once more"), rev="r1",
             name=f"Ozu Build {bid} r1 (7)  - Kindle.pdf")
    assert "build: split" in note.read_text(), "ink changes nothing here"
    lines = [e["line"] for e in
             db.unreported_build_events(db.connect(env.db_path))]
    assert any("was split; its work is in" in ln and made[0] in ln
               for ln in lines)


def test_a_split_survives_ozu_index(env, monkeypatch, sent, runs, tmp_path):
    """The Markdown is the ledger (invariants 1 and 7): a rebuilt db must
    come back with the same statuses, or a split would un-split itself."""
    from ozu import builds, db, indexer
    bid, _ = approved_build(env, monkeypatch, sent, runs, tmp_path)
    made = builds.split(bid, [part_file(tmp_path, "p.md", "KT-A — a part",
                                        "# Requirement\n\nA part.")])
    env.db_path.unlink()
    indexer.rebuild()
    rows = {r["id"]: r["status"] for r in
            db.connect(env.db_path).execute("SELECT id, status FROM builds")}
    assert rows[bid] == "split" and rows[made[0]] == "proposed"
