"""The weekly digest shows a low-confidence note the week it arrives, then only
counts it. Nothing on paper clears `needs_review`, so listing every flagged note
printed the same page images in every digest for good (paper feedback,
2026-11-26)."""
from __future__ import annotations

import datetime as dt
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OZU_HOME", str(tmp_path / "ozu"))
    monkeypatch.setenv("OZU_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("OZU_WORK_VAULT_DIR", str(tmp_path / "work-vault"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    import ozu.config as config
    config._config = None
    for m in ("ozu.db", "ozu.digest"):
        if m in sys.modules:
            importlib.reload(sys.modules[m])
    return config.get_config()


def _flagged(con, note_id: str, created: str) -> None:
    from ozu import db
    db.upsert_note(con, note_id=note_id, vault="personal", path=f"{note_id}.md",
                   title=note_id, topic="ideas", summary="s", tags=[], created=created,
                   body="b", needs_review=True, source_page=None)


def test_only_this_weeks_flagged_notes_are_listed(env):
    from ozu import db, digest
    con = db.connect(env.db_path)
    today = dt.date.today()
    _flagged(con, "old-a", (today - dt.timedelta(days=50)).isoformat())
    _flagged(con, "old-b", (today - dt.timedelta(days=8)).isoformat())
    _flagged(con, "fresh", (today - dt.timedelta(days=2)).isoformat())
    con.commit()
    data = digest.gather(days=7)
    assert [r["id"] for r in data["review"]] == ["fresh"]
    assert data["review_older"] == 2
    assert "2 older notes still marked draft" in digest.build_digest(data=data)


def test_older_flagged_notes_print_one_line_and_no_image(env):
    from ozu import digest_pdf
    data = {"review": [], "review_older": 3}
    lines = []

    class Doc:
        def heading(self, t): lines.append(("h", t))
        def para(self, t, cls=None): lines.append(("p", t))
        def image(self, *a): raise AssertionError("no image for an older note")

    digest_pdf._older_review(Doc(), data)
    assert lines == [("h", "Needs review"),
                     ("p", "3 older notes still marked draft in the vault"
                           " (each was shown here the week it came in).")]
