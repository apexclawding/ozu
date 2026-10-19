"""The vault as an OKF v0.2 bundle (okf.py) and the one-time migration.

What is pinned here: frontmatter that strict YAML parses and our readers
still grep; `type` derived from `source`; a note's filename is its id; the
derived views (index.md, log.md) regenerated from frontmatter and flagged
when stale; the export's wikilink rewrite; and that the migration turns a
pre-OKF vault into a conformant one and is a no-op the second time.
"""
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from test_pipeline import env, make_pdf, MOCK_RESULT  # noqa: F401 — shared fixture

from ozu import okf


# ---------------------------------------------------------------------------
# frontmatter

def test_frontmatter_round_trips_and_quotes_only_when_needed():
    fm = {
        "type": "Research",
        "id": "20260902-x",
        "title": 'Vision for "Kettle": brew-day timers',
        "description": "OKF standardises exactly two things. Nothing else [03:16].",
        "tags": ["research", "claude-research"],
        "created": "2026-09-02",
        "duration": "00:19:37",
        "needs_review": False,
        "related": [],
        "generated": {"by": "ozu/claude-opus-5", "at": "2026-09-02T14:30:00Z"},
        "sources": [{"id": "page", "resource": "/assets/2026-09/p1.png", "title": "Inbox p6"}],
        "skipped": None,
    }
    text = okf.dump_frontmatter(fm) + "\nbody\n"
    # the house style every reader greps for survives
    assert "tags: [research, claude-research]\n" in text
    assert "created: 2026-09-02\n" in text            # dates stay bare
    assert 'duration: "00:19:37"\n' in text           # sexagesimal would parse as an int
    assert "needs_review: false\n" in text
    assert 'title: "Vision for \\"Kettle\\": brew-day timers"\n' in text
    assert "generated: { by: ozu/claude-opus-5, at: 2026-09-02T14:30:00Z }\n" in text
    assert "sources:\n  - id: page\n    resource: /assets/2026-09/p1.png\n" in text
    assert "skipped" not in text
    # strict YAML agrees with what we wrote, and keeps dates as strings
    assert okf.strict_error(text) is None
    parsed, body = okf.parse_frontmatter(text)
    assert parsed["title"] == fm["title"] and parsed["duration"] == "00:19:37"
    assert parsed["created"] == "2026-09-02" and parsed["needs_review"] is False
    assert parsed["generated"]["at"] == "2026-09-02T14:30:00Z"
    assert parsed["sources"][0]["resource"] == "/assets/2026-09/p1.png"
    assert body == "\n\nbody\n"           # the newline after the closing --- belongs to the body


def test_legacy_frontmatter_still_parses_and_is_reported():
    legacy = ('---\nid: 1\ntitle: Research: Finally, an Open Standard\ntags: [a, b]\n'
              'duration: "00:19:37"\nsummary: s\n---\n\nbody\n')
    assert okf.strict_error(legacy).startswith("frontmatter is not valid YAML")
    fm, _ = okf.parse_frontmatter(legacy)
    assert fm["title"] == "Research: Finally, an Open Standard" and fm["tags"] == ["a", "b"]
    assert fm["duration"] == "00:19:37"          # quotes belong to YAML, not the value
    assert okf.description_of(fm) == "s"


def test_a_wikilink_inside_code_is_syntax_shown_not_a_link():
    body = ("See [[20260901-real]]. Every `[[id]]` must resolve.\n\n"
            "```\n[[20250101-in-a-fence]]\n```\n")
    assert okf.wikilinks(body) == {"20260901-real"}


def test_type_follows_the_source_prefix():
    assert okf.type_for("scribe:Inbox/p6") == "Note"
    assert okf.type_for("research:20260823-x") == "Research"
    assert okf.type_for("claude:20260818-x") == "Build Report"
    assert okf.type_for("build:20260906-x") == "Build"
    assert okf.type_for("youtube:abc123") == "Transcript"
    assert okf.type_for("learn:20260901-x") == "Learning"
    assert okf.type_for("voice:memo-1") == "Voice Memo"
    assert okf.type_for("") == "Note"


# ---------------------------------------------------------------------------
# what the writers produce

def write_page(env, monkeypatch, result=None):
    from ozu import ingest, understand
    make_pdf(env.raw_dir / "Inbox" / "p.pdf", ["x"])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: dict(result or MOCK_RESULT))
    understand.process_pending()


def test_written_note_is_a_conformant_concept_named_by_its_id(env, monkeypatch):
    write_page(env, monkeypatch)
    # index.md is a derived view and lives in every folder; glob order is not
    # ours to rely on, so skip it rather than assert it sorted last
    note = next(p for p in (env.vault_dir / "topics" / "kettle").glob("*.md")
                if p.name != "index.md")
    fm, _ = okf.parse_frontmatter(note.read_text())
    assert note.stem == fm["id"]                       # filename = id
    assert fm["type"] == "Note"
    assert fm["description"] == "Freemium model gated on pantry scanning."
    assert "summary" not in fm
    assert fm["generated"]["by"] == "ozu/claude-opus-5"
    assert fm["generated"]["at"].endswith("Z")
    assert fm["sources"][0]["resource"].startswith("/assets/") and "Inbox p" in fm["sources"][0]["title"]
    assert "status" not in fm                          # confident read: no lifecycle flag
    assert okf.strict_error(note.read_text()) is None
    errors, warnings = okf.check(env.vault_dir)
    assert errors == []
    # the only warning is the body's deliberate link to a note that does not exist
    assert warnings == [f"{note.relative_to(env.vault_dir)}: link to unknown note 20250101-old-note"]


def test_low_confidence_note_is_a_draft(env, monkeypatch):
    write_page(env, monkeypatch, dict(MOCK_RESULT, confidence=0.4))
    note = next((env.vault_dir / "topics" / "kettle").glob("2026*.md"))
    fm, _ = okf.parse_frontmatter(note.read_text())
    assert fm["needs_review"] is True and fm["status"] == "draft"


def test_derived_views_are_regenerated_and_flagged_when_stale(env, monkeypatch):
    write_page(env, monkeypatch)
    root = (env.vault_dir / "index.md").read_text()
    assert root.startswith('---\nokf_version: "0.2"\n---')
    assert "# Folders" in root and "* [topics/kettle](topics/kettle/index.md) - 1 note; newest" in root
    assert "* [How to use this vault](CLAUDE.md)" in root      # the Guide is a root concept
    topic = (env.vault_dir / "topics" / "kettle" / "index.md").read_text()
    assert topic.startswith("# Note\n\n* [Kettle freemium pricing](2026")
    assert "Freemium model gated on pantry scanning." in topic
    log_text = (env.vault_dir / "log.md").read_text()
    assert log_text.startswith("# Vault Update Log\n\n## 20")
    assert "* **Creation**: [Kettle freemium pricing](topics/kettle/2026" in log_text
    assert "— ozu/claude-opus-5" in log_text

    # a note edited by hand: the views are stale until `ozu index`
    note = next((env.vault_dir / "topics" / "kettle").glob("2026*.md"))
    note.write_text(note.read_text().replace("description: Freemium", "description: Paid"))
    errors, _ = okf.check(env.vault_dir)
    assert any(e.startswith("topics/kettle/index.md: stale") for e in errors)
    from ozu import indexer
    indexer.rebuild()
    assert okf.check(env.vault_dir)[0] == []
    assert "Paid" in (env.vault_dir / "topics" / "kettle" / "index.md").read_text()


def test_legacy_registries_are_an_error_and_the_indexer_removes_them(env, monkeypatch):
    write_page(env, monkeypatch)
    (env.vault_dir / "INDEX.md").write_text("# old\n")
    (env.vault_dir / "topics" / "kettle" / "_topic.md").write_text("# Kettle\n")
    # …and committed them, as an outside capture bridge might
    subprocess.run(["git", "add", "-A"], cwd=env.vault_dir, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "capture: x"],
                   cwd=env.vault_dir, check=True)
    errors, _ = okf.check(env.vault_dir)
    assert "INDEX.md: legacy registry — delete it; index.md is generated" in errors
    # a writer still on the old code re-created them; `ozu index` cleans up and
    # commits, so the next sync's rebase is never blocked by a dirty tree
    from ozu import indexer
    indexer.rebuild()
    assert not (env.vault_dir / "INDEX.md").exists()
    assert not (env.vault_dir / "topics" / "kettle" / "_topic.md").exists()
    assert okf.check(env.vault_dir)[0] == []
    status = subprocess.run(["git", "status", "--porcelain"], cwd=env.vault_dir,
                            capture_output=True, text=True).stdout
    assert status == ""
    head = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=env.vault_dir,
                          capture_output=True, text=True).stdout
    assert head.startswith("index: regenerate")


def test_export_rewrites_wikilinks_to_relative_markdown_links(env, monkeypatch, tmp_path):
    result = dict(MOCK_RESULT, notes=[
        dict(MOCK_RESULT["notes"][0], body="First idea."),
        dict(MOCK_RESULT["notes"][0], title="Second", slug="second", topic="ideas",
             body="Builds on [[20260902-kettle-freemium-pricing]] and [[nope]]."),
    ])
    import datetime as dt
    monkeypatch.setattr(dt, "date", type("D", (), {"today": staticmethod(lambda: dt.datetime(2026, 9, 2).date())}))
    write_page(env, monkeypatch, result)
    out = tmp_path / "export"
    stats = okf.export(env.vault_dir, out)
    assert stats["links_rewritten"] == 1
    second = (out / "topics" / "ideas" / "20260902-second.md").read_text()
    assert "[Kettle freemium pricing](../kettle/20260902-kettle-freemium-pricing.md)" in second
    assert "[[nope]]" in second                        # unknown targets are left alone
    assert (out / "index.md").exists() and (out / "log.md").exists()
    assert list((out / "assets").rglob("*.png"))       # the page images travel with the bundle
    # a second export replaces the first; a foreign directory is never wiped
    okf.export(env.vault_dir, out)
    with pytest.raises(SystemExit):
        okf.export(env.vault_dir, tmp_path)
