"""Wiki tests. The headless call is mocked; the ledger, the page files, the
edges, the changelog, the confinement and the Sunday sheet (typeset into a
real edition PDF) are real."""
import datetime as dt
import json
import os.path
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from test_pipeline import env  # noqa: F401 — shared fixture
from test_learn import deliver, pdf_text


def note(env, folder, nid, source, title="A note", body="Body.", created="2026-09-20"):
    from ozu import okf
    d = env.vault_dir / "topics" / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{nid}.md").write_text(f"""---
type: {okf.type_for(source)}
id: {nid}
title: "{title}"
description: "{title}."
tags: [x]
created: {created}
updated: {created}
source: "{source}"
related: []
needs_review: false
---

# {title}

{body}
""")


def seed(env):
    from ozu import indexer, vault
    vault.scaffold(env.vault_dir)
    note(env, "ideas", "20260901-ink-page", "scribe:Inbox/p3", "Ink page", created="2026-09-01")
    note(env, "voice", "20260902-memo", "voice:Memo.m4a", "Memo", created="2026-09-02")
    note(env, "learning", "20260903-brief-learning", "learn:20260903-brief", "Learning: brief",
         created="2026-09-03")
    note(env, "learning", "20260903-brief", "youtube:abc", "Raw transcript", created="2026-09-03")
    note(env, "kettle", "20260904-claude-run", "claude:20260901-ink-page", "Build report",
         created="2026-09-04")
    note(env, "kettle", "B-260905-a1b2", "build:20260901-ink-page", "A Build", created="2026-09-05")
    indexer.rebuild()


def reply(pages, ignored=None):
    return "Here you go:\n```json\n" + json.dumps({"pages": pages, "ignored": ignored or {}}) + "\n```"


PAGE = {
    "id": "wiki-llm-wiki-pattern",
    "title": "The LLM wiki pattern",
    "description": "Let the model keep compiled pages; you keep the raw notes.",
    "tags": ["knowledge", "Bad Tag!"],
    "body": ("# A title the model should not write\n\n## In one paragraph\nPages compound.\n\n"
             "## What I know\n- Raw stays raw; the wiki is rewritten — [[20260903-brief-learning]]\n"
             "- They want it urgently — [[20260901-ink-page]], [[20260902-memo]]\n"
             "- A claim citing a ghost — [[20990101-does-not-exist]]\n\n"
             "## Changelog\n- 1999-01-01: the model must not write this"),
    "change": "started the page from [[20260903-brief-learning]]",
}


def test_pending_is_ink_voice_learning_and_research_only(env):
    from ozu import db, wiki
    seed(env)
    ids = [r["id"] for r in wiki.pending(db.connect(env.db_path))]
    # oldest first; the raw transcript, the Build and its report stay out
    assert ids == ["20260901-ink-page", "20260902-memo", "20260903-brief-learning"]


def test_run_is_read_only_and_writes_a_cited_page(env, monkeypatch):
    from ozu import agent, db, okf, wiki
    seed(env)
    seen = {}

    def fake_run(prompt, workdir, allowed_tools=None, model=None, timeout=None, writes=False):
        seen.update(prompt=prompt, tools=allowed_tools, cwd=workdir, model=model,
                    writes=writes)
        return reply([PAGE], {"20260902-memo": "a shopping list"})

    monkeypatch.setattr(agent, "_run_claude", fake_run)
    assert wiki.work_pending() == 1

    # invariant 11e: Read pinned to the vault, and that is the whole allowlist;
    # and 8b: the run is read-only
    scope = "//" + os.path.abspath(env.vault_dir).lstrip("/") + "/**"
    assert seen["tools"] == f"Read({scope})"
    assert seen["writes"] is False
    assert seen["cwd"] == env.vault_dir
    assert "topics/ideas/20260901-ink-page.md" in seen["prompt"]
    assert "(none yet" in seen["prompt"]

    path = env.vault_dir / "topics" / "wiki" / "wiki-llm-wiki-pattern.md"
    text = path.read_text()
    fm, body = okf.parse_frontmatter(text)
    assert fm["type"] == "Concept" and fm["source"] == "wiki:compiled"
    assert fm["tags"] == ["wiki", "knowledge"]           # a malformed tag is dropped
    assert fm["related"] == ["20260903-brief-learning", "20260901-ink-page", "20260902-memo"]
    assert [s["id"] for s in fm["sources"]] == fm["related"]
    assert "A title the model should not write" not in text     # our H1, not its
    assert "1999-01-01" not in text                             # our changelog, not its
    assert "## Changelog" in body and "started the page from" in body
    assert "[[20990101-does-not-exist]]" not in text and "20990101-does-not-exist" in text

    con = db.connect(env.db_path)
    fed = {r["note_id"]: r["outcome"] for r in con.execute("SELECT * FROM wiki_fed")}
    assert fed == {"20260901-ink-page": "wiki-llm-wiki-pattern",
                   "20260902-memo": "wiki-llm-wiki-pattern",   # cited beats ignored
                   "20260903-brief-learning": "wiki-llm-wiki-pattern"}
    edges = {r["dst"] for r in con.execute(
        "SELECT dst FROM edges WHERE src='wiki-llm-wiki-pattern'")}
    assert edges == {"20260903-brief-learning", "20260901-ink-page", "20260902-memo"}
    assert okf.check(env.vault_dir)[0] == []                    # no conformance errors
    assert wiki.work_pending() == 0                             # nothing left unread


def test_update_in_place_keeps_created_and_grows_the_changelog(env, monkeypatch):
    from ozu import agent, indexer, okf, wiki
    seed(env)
    monkeypatch.setattr(agent, "_run_claude", lambda *a, **k: reply([PAGE]))
    wiki.work_pending()
    note(env, "ideas", "20260921-correction", "scribe:Ozu Evening 2026-09-20/p9",
         "Correction", body="Re: wiki-llm-wiki-pattern — struck through: They want it urgently",
         created="2026-09-21")
    indexer.rebuild()

    prompts = []
    second = dict(PAGE, body=PAGE["body"].replace(
        "- They want it urgently — [[20260901-ink-page]], [[20260902-memo]]\n", "")
        + "\n- Struck on paper — [[20260921-correction]]",
        change="dropped the struck claim per [[20260921-correction]]")

    def fake_run(prompt, *a, **k):
        prompts.append(prompt)
        return reply([second])

    monkeypatch.setattr(agent, "_run_claude", fake_run)
    import datetime as _dt

    class Later(_dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 22)

    monkeypatch.setattr(wiki.dt, "date", Later)
    assert wiki.work_pending() == 1
    assert "wiki-llm-wiki-pattern — The LLM wiki pattern" in prompts[0]
    fm, body = okf.parse_frontmatter(
        (env.vault_dir / "topics/wiki/wiki-llm-wiki-pattern.md").read_text())
    assert fm["updated"] == "2026-09-22" and fm["created"] != "2026-09-22"
    log = body.split("## Changelog")[1]
    assert "started the page" in log and "2026-09-22: dropped the struck claim" in log
    assert "They want it urgently" not in body.split("## Changelog")[0]


def test_auth_failure_leaves_the_batch_queued(env, monkeypatch):
    from ozu import agent, db, wiki
    seed(env)

    def expired(*a, **k):
        raise agent.UsageLimitError("usage limit reached")

    monkeypatch.setattr(agent, "_run_claude", expired)
    assert wiki.work_pending() == 0
    assert db.connect(env.db_path).execute("SELECT COUNT(*) c FROM wiki_fed").fetchone()["c"] == 0


def test_bad_reply_marks_error_rests_for_the_day_and_requeues(env, monkeypatch):
    from ozu import agent, db, wiki
    seed(env)
    calls = []
    monkeypatch.setattr(agent, "_run_claude", lambda *a, **k: calls.append(1) or "no json here")
    assert wiki.work_pending() == 0
    con = db.connect(env.db_path)
    assert {r["outcome"] for r in con.execute("SELECT outcome FROM wiki_fed")} == {"error"}
    note(env, "ideas", "20260922-new-ink", "scribe:Inbox/p9", "New", created="2026-09-22")
    from ozu import indexer
    indexer.rebuild()
    assert wiki.work_pending() == 0 and len(calls) == 1         # resting until tomorrow
    assert wiki.requeue() == 3
    assert len(wiki.pending(con)) == 4


def test_invalid_page_ids_are_refused(env, monkeypatch):
    from ozu import agent, wiki
    seed(env)
    bad = [dict(PAGE, id="../../etc/passwd"), dict(PAGE, id="Wiki Page"),
           dict(PAGE, id="notwiki-x"), dict(PAGE, id="wiki-ok", body="")]
    monkeypatch.setattr(agent, "_run_claude", lambda *a, **k: reply(bad))
    assert wiki.work_pending() == 0
    d = env.vault_dir / "topics" / "wiki"
    assert not d.exists() or not list(d.glob("*.md"))


def test_sunday_evening_carries_the_wiki_sheet_once(env, monkeypatch):
    from ozu import agent, db, edition, wiki
    seed(env)
    monkeypatch.setattr(agent, "_run_claude", lambda *a, **k: reply([PAGE]))
    wiki.work_pending()
    deliver(monkeypatch)
    sunday = dt.date.today() + dt.timedelta(days=(6 - dt.date.today().weekday()) % 7)

    con = db.connect(env.db_path)
    sheet = wiki.gather_sheet(con, sunday)
    assert [p["id"] for p in sheet["pages"]] == ["wiki-llm-wiki-pattern"]
    assert sheet["pages"][0]["new"]
    assert sheet["pages"][0]["claims"][0] == "- Raw stays raw; the wiki is rewritten"
    assert sheet["pages"][0]["changes"][0].endswith("started the page from ‘brief’")

    assert edition.send("evening", sunday)
    pdf = next(env.editions_dir.glob("Ozu Evening *.pdf"))
    text = pdf_text(pdf)
    assert "Wiki this week" in text and "page: wiki-llm-wiki-pattern" in text
    assert "Raw stays raw" in text and "[[" not in text and "☐" not in text
    assert wiki.gather_sheet(con, sunday) is None               # marked sent
