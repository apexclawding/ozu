"""The db is what a rebuild of the files gives — nothing more, nothing less.

Each writer used to put its own rows in the db, and an edge's `reason`
lived only there: every rebuild dropped it, so a link's reason never
survived the next `ozu index`. Writers now record the reason in the note
(`related_why`) and all go through `indexer.index_file`, so the first test
pins the property the bug broke: index live, rebuild, compare."""
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from test_pipeline import MOCK_RESULT, env, make_pdf  # noqa: F401 — shared fixture


def _graph(con):
    edges = {tuple(r) for r in con.execute("SELECT src, dst, type, reason FROM edges")}
    notes = {tuple(r) for r in con.execute(
        "SELECT id, vault, path, title, topic, summary, tags, created, needs_review,"
        " claude, research, learn, source_page FROM notes")}
    return edges, notes


def _page(env, monkeypatch, related, reason):
    from ozu import ingest, understand
    make_pdf(env.raw_dir / "Inbox" / "ideas.pdf", ["an idea"])
    ingest.ingest()
    result = dict(MOCK_RESULT)
    result["notes"] = [dict(MOCK_RESULT["notes"][0], related=related,
                            related_reason=reason)]
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: result)
    assert understand.process_pending() == 1
    return next((env.vault_dir / "topics" / "kettle").glob("*kettle-freemium-pricing*.md"))


def test_a_rebuild_reproduces_the_live_graph_reasons_included(env, monkeypatch):
    from ozu import db, indexer, vault
    note = _page(env, monkeypatch, ["20250101-old-plan", "20250102-pricing"],
                 "contradicts earlier plan; supports: the pricing idea")
    nid = note.stem
    source = {"id": nid, "title": "Kettle freemium pricing", "topic": "kettle",
              "path": str(note)}
    vault.write_research_note(source=source, report="Findings.", pdf_name=None)
    vault.write_agent_note(source=source, report="Done.", branch="b", compare_url=None)
    vault.write_learning_note(source=source, report="# Takeaways\n- see [[20250103-cited]]",
                              video={"url": "https://example.com/v", "channel": "c"})

    con = db.connect(env.db_path)
    live = _graph(con)
    reasons = {(s, d): r for s, d, _, r in live[0]}
    assert reasons[(nid, "20250101-old-plan")] == "contradicts earlier plan"
    assert reasons[(nid, "20250102-pricing")] == "supports: the pricing idea"
    assert "research brief" in reasons.values()
    assert "claude work run" in reasons.values()
    assert "learning brief" in reasons.values()
    assert "cited in learning brief" in reasons.values()
    assert any(row[-1] is not None for row in live[1])     # the page it was read from

    indexer.rebuild()
    assert _graph(db.connect(env.db_path)) == live


def test_the_reason_is_in_the_note_and_round_trips(env, monkeypatch):
    from ozu import okf
    note = _page(env, monkeypatch, ["20250101-old-plan"], "a reason: with, punctuation")
    fm, _ = okf.parse_frontmatter(note.read_text())
    assert fm["related"] == ["20250101-old-plan"]
    assert okf.related_why(fm) == {"20250101-old-plan": "a reason: with, punctuation"}
    assert okf.strict_error(note.read_text()) is None
    # the related array stays on one line, as line-based readers expect
    assert "related: [20250101-old-plan]" in note.read_text()


def test_one_sentence_reason_covers_every_link(tmp_path):
    from ozu import okf
    fm, _ = okf.parse_frontmatter(okf.dump_frontmatter(
        {"id": "wiki-x", "related": ["a", "b"], "related_why": "cited on wiki page"}))
    assert okf.related_why(fm) == {"*": "cited on wiki page"}


def test_a_failed_rebuild_leaves_the_old_graph_served(env, monkeypatch):
    from ozu import db, indexer
    _page(env, monkeypatch, ["20250101-old-plan"], "why")
    before = _graph(db.connect(env.db_path))

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(indexer, "index_file", boom)
    try:
        indexer.rebuild()
    except RuntimeError:
        pass
    assert _graph(db.connect(env.db_path)) == before


def _git_vault(env):
    v = env.vault_dir
    v.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=v, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=v, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=v, check=True)


def _git(env, *args):
    return subprocess.run(["git", *args], cwd=env.vault_dir, capture_output=True,
                          text=True).stdout.strip()


def test_catch_up_indexes_a_hand_edit_and_commits_it(env, monkeypatch):
    from ozu import db, indexer
    _git_vault(env)
    note = _page(env, monkeypatch, [], "")
    assert indexer.catch_up() is True          # first sight of this HEAD
    assert indexer.catch_up() is False         # nothing moved: an idle tick

    # a hand edit on the box: a link, with its reason, and nobody commits
    text = note.read_text().replace(
        "related: []", "related: [20250105-new-link]\nrelated_why:\n  20250105-new-link: added by hand")
    note.write_text(text)
    assert indexer.catch_up() is True
    assert _git(env, "status", "--porcelain") == ""
    assert "vault: hand edits" in _git(env, "log", "--format=%s")
    con = db.connect(env.db_path)
    row = con.execute("SELECT reason FROM edges WHERE dst='20250105-new-link'").fetchone()
    assert row["reason"] == "added by hand"
    assert indexer.catch_up() is False


def test_catch_up_indexes_a_commit_made_outside_the_pipeline(env, monkeypatch):
    """another program writes and commits on its own: HEAD moves with a
    clean tree, and that alone must be enough."""
    from ozu import db, indexer
    _git_vault(env)
    _page(env, monkeypatch, [], "")
    indexer.catch_up()
    shared = env.vault_dir / "topics" / "shared"
    shared.mkdir(parents=True)
    (shared / "20260101-term.md").write_text(
        "---\ntype: Note\nid: 20260101-term\ntitle: Term\ndescription: Term.\n"
        "related: []\n---\n\n# Term\n")
    _git(env, "add", "-A")
    _git(env, "commit", "-q", "-m", "sync: notes (1 file(s))")
    assert indexer.catch_up() is True
    con = db.connect(env.db_path)
    assert con.execute("SELECT 1 FROM notes WHERE id='20260101-term'").fetchone()


def test_catch_up_without_a_git_vault_does_nothing(env, monkeypatch):
    import shutil
    from ozu import indexer
    _page(env, monkeypatch, [], "")
    shutil.rmtree(env.vault_dir / ".git")      # a vault copied off the box
    assert indexer.catch_up() is False
