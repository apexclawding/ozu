"""A renamed topic stays readable everywhere, through `config.canonical_topic`.

`OZU_TOPIC_ALIASES=old:new,...` is the one door every topic passes on its way
to a folder, a repo or a query. A writer that skips it reopens the old
folder and splits the history, so each door has a test here.

Malformed entries (no colon, an empty side, a name aliased to itself) are
ignored, not fatal: a typo in `.env` must not stop the tick.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pipeline import env, make_pdf, MOCK_RESULT  # noqa: E402,F401

from ozu import ingest, understand  # noqa: E402
from ozu.config import canonical_topic, topic_aliases  # noqa: E402


@pytest.fixture()
def aliased(env, monkeypatch):
    import ozu.config as config
    monkeypatch.setenv("OZU_TOPIC_ALIASES", "legacy:main,Old:New")
    config._config = None
    return config.get_config()


def test_unset_and_empty_are_identity(monkeypatch):
    monkeypatch.delenv("OZU_TOPIC_ALIASES", raising=False)
    assert canonical_topic("legacy") == "legacy"
    monkeypatch.setenv("OZU_TOPIC_ALIASES", "")
    assert topic_aliases() == {}
    assert canonical_topic("legacy") == "legacy"


def test_alias_is_case_insensitive_and_unknown_passes_through(monkeypatch):
    monkeypatch.setenv("OZU_TOPIC_ALIASES", "legacy:main,Old:New")
    assert canonical_topic("legacy") == "main"
    assert canonical_topic("LEGACY") == "main"
    assert canonical_topic(" legacy ") == "main"
    assert canonical_topic("old") == "new"
    assert canonical_topic("garden") == "garden"
    assert canonical_topic("Garden") == "Garden"
    assert canonical_topic("main") == "main"


def test_malformed_entries_are_ignored(monkeypatch):
    monkeypatch.setenv("OZU_TOPIC_ALIASES",
                       "nocolon,:new,old:,  :  ,same:same,ok:fine,,a:b:c")
    assert topic_aliases() == {"ok": "fine", "a": "b:c"}
    assert canonical_topic("nocolon") == "nocolon"
    assert canonical_topic("same") == "same"


def test_aliases_are_one_step_not_chained(monkeypatch):
    monkeypatch.setenv("OZU_TOPIC_ALIASES", "a:b,b:c")
    assert canonical_topic("a") == "b"


def test_tagged_note_is_written_under_the_new_folder(aliased, monkeypatch):
    make_pdf(aliased.raw_dir / "Inbox" / "m.pdf", ["z"])
    ingest.ingest()
    result = dict(MOCK_RESULT)
    result["notes"] = [dict(MOCK_RESULT["notes"][0], topic="legacy")]
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: result)
    understand.process_pending()
    assert list((aliased.vault_dir / "topics" / "main").glob("*.md"))
    assert not (aliased.vault_dir / "topics" / "legacy").exists()


def test_repo_key_and_projects_and_isa_read_the_alias(env, monkeypatch):
    import ozu.config as config
    monkeypatch.setenv("OZU_TOPIC_ALIASES", "legacy:main")
    monkeypatch.setenv("OZU_CLAUDE_REPOS", "Legacy=git@example.com:me/main.git")
    monkeypatch.setenv("OZU_PROJECTS", "legacy,garden")
    monkeypatch.setenv("OZU_ISA_TOPICS", "legacy")
    config._config = None
    cfg = config.get_config()
    assert cfg.claude_repos == {"main": "git@example.com:me/main.git"}
    assert cfg.projects == ["main", "garden"]
    assert cfg.isa_topics == ("main",)
    assert canonical_topic("legacy") in cfg.isa_topics
    assert understand._projects().count("main") == 1
    assert "legacy" not in understand._projects()


def test_mcp_timeline_and_capture_accept_the_old_name(aliased, monkeypatch):
    import importlib
    make_pdf(aliased.raw_dir / "Inbox" / "m.pdf", ["z"])
    ingest.ingest()
    result = dict(MOCK_RESULT)
    result["notes"] = [dict(MOCK_RESULT["notes"][0], topic="main")]
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: result)
    understand.process_pending()
    import ozu.mcp_server as srv
    importlib.reload(srv)
    assert [n["title"] for n in srv.timeline("legacy")] == ["Kettle freemium pricing"]
    assert srv.timeline("main") == srv.timeline("LEGACY")
    out = srv.capture("a thought", title="A thought", topic="legacy")
    assert out.startswith("Captured as")
    assert not (aliased.vault_dir / "topics" / "legacy").exists()
    assert any(n["title"] == "A thought" for n in srv.timeline("main"))


def test_a_note_in_an_old_folder_is_indexed_under_the_new_topic(aliased):
    from ozu import db, indexer
    old = aliased.vault_dir / "topics" / "legacy"
    old.mkdir(parents=True)
    note = old / "20260101-x.md"
    note.write_text("---\nid: 20260101-x\ntitle: X\ndescription: d\n---\nbody\n")
    con = db.connect(aliased.db_path)
    indexer.index_file(con, note)
    assert con.execute("SELECT topic FROM notes WHERE id='20260101-x'"
                       ).fetchone()[0] == "main"
