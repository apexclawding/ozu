"""The MCP wall is allow-list shaped: a note leaves only if its label is
exactly `personal` (or exactly `work`, with OZU_ALLOW_WORK_MCP=1 and only
where a tool takes a vault). A label nobody migrated, or a typo, must fail
closed — so every tool is called against a walled note that carries a unique
token in its title, body, action and edge reasons, under three labels, and
the token must never come back.
"""
import importlib
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

TOKEN = "quokkawall7"
WALLED = "20260101-walled-note"
MINE = "20260101-plain-note"
OTHER = "20260102-second-note"
LABELS = ["work", "zzz", "legacy"]
ASKED = [None, "personal", "work", "legacy", "zzz"]


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("OZU_HOME", str(tmp_path / "ozu"))
    monkeypatch.setenv("OZU_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("OZU_WORK_VAULT_DIR", str(tmp_path / "work-vault"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.delenv("OZU_ALLOW_WORK_MCP", raising=False)
    import ozu.config as config
    config._config = None
    import ozu.db
    importlib.reload(ozu.db)
    import ozu.mcp_server as mod
    importlib.reload(mod)
    return mod


def _note(con, tmp: Path, note_id: str, vault: str, title: str, body: str):
    from ozu import db
    path = tmp / f"{note_id}.md"
    path.write_text(f"# {title}\n\n{body}\n")
    db.upsert_note(con, note_id=note_id, vault=vault, path=str(path),
                   title=title, topic="shared", summary=body[:60],
                   tags=["shared"], created="2026-01-01", body=body,
                   needs_review=False, source_page=None)


def _plant(srv, tmp: Path, label: str):
    """One personal note, one more personal note one hop away, and the walled
    note: an action on each, edges both ways between personal and walled."""
    from ozu import db
    con = srv._con()
    _note(con, tmp, MINE, "personal", "Plain note", "harmless garden plans")
    _note(con, tmp, OTHER, "personal", "Second note", "more garden plans")
    _note(con, tmp, WALLED, label, f"Walled {TOKEN}", f"secret {TOKEN} body")
    db.add_action(con, MINE, "water the garden")
    db.add_action(con, WALLED, f"act on {TOKEN}")
    db.add_edge(con, MINE, OTHER, reason="same garden")
    db.add_edge(con, MINE, "20250101-dangling", reason="names no note")
    db.add_edge(con, MINE, WALLED, reason=f"links to {TOKEN}")
    db.add_edge(con, WALLED, MINE, reason=f"links back from {TOKEN}")
    db.add_edge(con, WALLED, OTHER, reason=f"also {TOKEN}")
    return con


def _relabel(con, label: str):
    con.execute("UPDATE notes SET vault=? WHERE id=?", (label, WALLED))
    con.commit()


def _call(fn, *a, **k) -> str:
    """A tool's whole answer as text — a refusal's message counts as output."""
    try:
        return repr(fn(*a, **k))
    except ValueError as exc:
        return f"refused: {exc}"


def _every_answer(srv) -> dict[str, str]:
    out = {}
    for v in ASKED:
        kw = {} if v is None else {"vault": v}
        out[f"search_notes[{v}]"] = _call(srv.search_notes, TOKEN, **kw)
        out[f"search_notes_title[{v}]"] = _call(srv.search_notes, "Walled", **kw)
    out["get_note"] = _call(srv.get_note, WALLED)
    out["timeline"] = _call(srv.timeline, "shared")
    out["open_actions"] = _call(srv.open_actions)
    out["graph_neighbors[mine,1]"] = _call(srv.graph_neighbors, MINE)
    out["graph_neighbors[mine,3]"] = _call(srv.graph_neighbors, MINE, depth=3)
    out["graph_neighbors[other,2]"] = _call(srv.graph_neighbors, OTHER, depth=2)
    out["graph_neighbors[walled]"] = _call(srv.graph_neighbors, WALLED, depth=2)
    return out


def test_no_tool_returns_a_walled_note_under_any_label(srv, tmp_path):
    con = _plant(srv, tmp_path, LABELS[0])
    for label in LABELS:
        _relabel(con, label)
        answers = _every_answer(srv)
        leaked = {k: v for k, v in answers.items() if TOKEN in v.lower()}
        assert not leaked, (label, leaked)
        # Nor its id, anywhere but the refusal that echoes the asked-for id.
        named = {k: v for k, v in answers.items() if WALLED in v and k != "get_note"}
        assert not named, (label, named)
        # Refused exactly like a missing note, so it is no oracle.
        assert answers["get_note"] == repr(f"Note {WALLED} not found.")
        assert _call(srv.get_note, "20990101-missing").replace(
            "20990101-missing", WALLED) == answers["get_note"]


def test_the_personal_side_still_answers(srv, tmp_path):
    _plant(srv, tmp_path, "work")
    assert "harmless" in srv.get_note(MINE)
    assert [a["text"] for a in srv.open_actions()] == ["water the garden"]
    ends = {(e["src"], e["dst"]) for e in srv.graph_neighbors(MINE)}
    assert ends == {(MINE, OTHER), (MINE, "20250101-dangling")}
    assert [h["id"] for h in srv.search_notes("garden")] != []
    assert {n["id"] for n in srv.timeline("shared")} == {MINE, OTHER}


@pytest.mark.parametrize("asked", ["work", "legacy", "zzz", "", "Personal"])
def test_a_vault_other_than_personal_is_refused_without_the_opt_in(srv, tmp_path, asked):
    _plant(srv, tmp_path, "work")
    with pytest.raises(ValueError):
        srv.search_notes("garden", vault=asked)


def test_the_opt_in_serves_work_and_nothing_else(srv, tmp_path, monkeypatch):
    con = _plant(srv, tmp_path, "work")
    monkeypatch.setenv("OZU_ALLOW_WORK_MCP", "1")
    # The opt-in path works: an exact `work` label is served on request.
    assert TOKEN in srv.get_note(WALLED)
    assert [h["id"] for h in srv.search_notes(TOKEN, vault="work")] == [WALLED]
    # ...but never by default, and never through actions or the graph.
    assert TOKEN not in _call(srv.search_notes, TOKEN)
    assert TOKEN not in _call(srv.open_actions)
    assert TOKEN not in _call(srv.graph_neighbors, MINE, depth=3)
    assert srv.graph_neighbors(WALLED) == []
    # A label that is not exactly `work` stays walled even with the opt-in.
    for label in ("zzz", "legacy", "Work", ""):
        _relabel(con, label)
        answers = _every_answer(srv)
        leaked = {k: v for k, v in answers.items()
                  if TOKEN in v.lower() or (WALLED in v and k != "get_note")}
        assert not leaked, (label, leaked)
        assert answers["get_note"] == repr(f"Note {WALLED} not found.")
        with pytest.raises(ValueError):
            srv.search_notes(TOKEN, vault=label)


@pytest.mark.parametrize("label", LABELS)
def test_get_note_refuses_every_label_but_personal(srv, tmp_path, label):
    """The stored label is checked allow-list style: before this, only an
    exact `work` was refused and any other label was served."""
    _plant(srv, tmp_path, label)
    assert srv.get_note(WALLED) == f"Note {WALLED} not found."
