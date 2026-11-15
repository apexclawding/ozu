"""The weekly board (board.py, docs/board.md). The headless call is mocked;
the development brief (read from real git repositories under tmp_path), the
board file, its edges, its once-a-day gate, its confinement (invariant 11f)
and its line on the Sunday sheet (typeset into a real edition PDF) are real."""
import datetime as dt
import json
import os
import os.path
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from test_pipeline import env  # noqa: F401 — shared fixture
from test_learn import deliver, pdf_text
from test_wiki import note, seed

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def git(repo, *args, when=None):
    e = {**os.environ, **GIT_ENV}
    if when:
        e["GIT_AUTHOR_DATE"] = e["GIT_COMMITTER_DATE"] = f"{when}T12:00:00"
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=e)


def make_repo(tmp_path, name, commits, plan=None, plan_name="ROADMAP.md"):
    repo = tmp_path / "repos" / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    if plan is not None:
        (repo / plan_name).parent.mkdir(parents=True, exist_ok=True)
        (repo / plan_name).write_text(plan)
    for i, (when, subject) in enumerate(commits):
        (repo / f"f{i}.txt").write_text(subject)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", subject, when=when)
    return repo


ROADMAP = """# Roadmap

## Offline mode

**Status:** next.

Why it waits.

## Done thing

Shipped.

- [ ] write the migration note
- [x] closed item
"""

OLD_BOARD = """---
type: Board
id: 20260830-my-own-board-id
title: My board
description: Old board.
created: 2026-08-30
updated: 2026-09-06
source: board:weekly
related: []
needs_review: false
---

# My board

old
"""


def board_reply(body="## The short version\nShipped [[20260901-ink-page]] and "
                     "[[20990101-ghost]].", description="The ink loop had the week."):
    return json.dumps({"description": description, "body": body})


def test_parse_repos():
    from ozu import board
    got = board.parse_repos(" a=~/x , b=/y#docs/plan.md|TODO.md, =nameless, c=,  ")
    assert got == [("a", Path(os.path.expanduser("~/x")), board.DEFAULT_PLANS),
                   ("b", Path("/y"), ("docs/plan.md", "TODO.md"))]


def test_the_brief_is_facts_from_the_named_repositories(env, tmp_path):
    from ozu import board
    busy = make_repo(tmp_path, "busy", [("2026-09-01", "old work"),
                                        ("2026-09-20", "add offline cache"),
                                        ("2026-09-21", "fix the sync")], plan=ROADMAP)
    quiet = make_repo(tmp_path, "quiet", [("2026-08-01", "last touch")])
    out = board.repo_brief("busy", busy, board.DEFAULT_PLANS, dt.date(2026, 9, 15))
    assert "### busy" in out and "3 commit(s)" not in out and "2 commit(s) since 2026-09-15" in out
    assert "add offline cache" in out and "fix the sync" in out and "old work" not in out
    assert "plan `ROADMAP.md`: changed in 1 commit(s) this week" not in out  # plan came first
    assert "- ROADMAP.md: Offline mode — **Status:** next." in out
    assert "- ROADMAP.md: - [ ] write the migration note" in out
    assert "closed item" not in out and "Done thing" not in out
    out = board.repo_brief("quiet", quiet, board.DEFAULT_PLANS, dt.date(2026, 9, 15))
    assert "no commits since 2026-09-15 (last 2026-08-01)" in out
    out = board.repo_brief("gone", tmp_path / "nope", board.DEFAULT_PLANS, dt.date(2026, 9, 15))
    assert "not a git repository" in out
    # a plan path that leaves the repository is not read
    (tmp_path / "repos" / "secret.md").write_text("- [ ] outside")
    out = board.repo_brief("busy", busy, ("../secret.md",), dt.date(2026, 9, 15))
    assert "outside" not in out


def test_board_rewrites_master_when_a_week_old(env, monkeypatch, tmp_path):
    from ozu import agent, board, db, indexer, okf
    seed(env)
    repo = make_repo(tmp_path, "kettle", [("2026-09-20", "ship the kettle timer")],
                     plan="- [ ] descale reminder\n", plan_name="docs/plan.md")
    monkeypatch.setattr(env, "board_repos", f"kettle={repo}#docs/plan.md")
    (env.vault_dir / "MASTER.md").write_text(OLD_BOARD)
    note(env, "ideas", "20260910-after-board", "scribe:Inbox/p7", "After the board",
         created="2026-09-10")
    indexer.rebuild()
    assert board.board_id() == "20260830-my-own-board-id"
    assert not board.due(dt.date(2026, 9, 12)) and board.due(dt.date(2026, 9, 13))
    seen = {}

    def fake_run(prompt, workdir, allowed_tools=None, model=None, timeout=None, writes=False):
        seen.update(prompt=prompt, tools=allowed_tools, writes=writes, workdir=workdir,
                    model=model)
        return board_reply()

    monkeypatch.setattr(agent, "_run_claude", fake_run)
    assert board.run(dt.date(2026, 9, 24))
    scope = "//" + os.path.abspath(env.vault_dir).lstrip("/") + "/**"
    assert seen["tools"] == f"Read({scope})" and seen["writes"] is False
    assert seen["workdir"] == env.vault_dir and seen["model"] is None
    p = seen["prompt"]
    assert "### kettle" in p and "ship the kettle timer" in p and "descale reminder" in p
    assert "After the board" in p and "Ink page" not in p        # older than last edition
    fm, body = okf.parse_frontmatter((env.vault_dir / "MASTER.md").read_text())
    assert fm["type"] == "Board" and fm["id"] == "20260830-my-own-board-id"
    assert fm["title"] == "My board" and fm["source"] == "board:weekly"
    assert fm["created"] == "2026-08-30" and fm["updated"] == "2026-09-24"
    assert fm["related"] == ["20260901-ink-page"] and "[[20990101-ghost]]" not in body
    assert not board.run(dt.date(2026, 9, 25))                  # fresh again
    con = db.connect(env.db_path)
    assert {r["dst"] for r in con.execute("SELECT dst FROM edges WHERE src=?",
                                          ("20260830-my-own-board-id",))} == {"20260901-ink-page"}
    assert okf.check(env.vault_dir)[0] == []
    assert "Concept, Board, Guide" in okf.GUIDE                 # the guide names the type


def test_a_first_board_gets_the_default_id(env, monkeypatch):
    from ozu import agent, board, okf
    seed(env)
    monkeypatch.setattr(agent, "_run_claude", lambda *a, **k: board_reply())
    assert board.run(dt.date(2026, 9, 24))
    fm, _ = okf.parse_frontmatter((env.vault_dir / "MASTER.md").read_text())
    assert fm["id"] == board.DEFAULT_ID and fm["title"] == board.TITLE


def test_the_cycle_runs_it_only_when_on_and_once_a_day(env, monkeypatch):
    from ozu import agent, board
    seed(env)
    calls = []
    monkeypatch.setattr(agent, "_run_claude", lambda *a, **k: calls.append(1) or "garbage")
    assert not board.run_if_due() and calls == []               # OZU_BOARD=0: nothing
    monkeypatch.setattr(env, "board", "1")
    assert not board.run_if_due() and calls == [1]              # a bad reply, logged
    assert not board.run_if_due() and calls == [1]              # and not again today
    assert not (env.vault_dir / "MASTER.md").exists()


def test_a_brief_command_replaces_the_repositories(env, monkeypatch, tmp_path):
    from ozu import agent, board
    seed(env)
    monkeypatch.setattr(env, "board_repos", "never=/does/not/exist")
    monkeypatch.setattr(env, "board_brief_cmd",
                        f"{sys.executable} -c \"print('BRIEF FROM ELSEWHERE')\"")
    seen = {}
    monkeypatch.setattr(agent, "_run_claude",
                        lambda prompt, *a, **k: seen.update(prompt=prompt) or board_reply())
    assert board.run(dt.date(2026, 9, 24))
    assert "BRIEF FROM ELSEWHERE" in seen["prompt"] and "/does/not/exist" not in seen["prompt"]
    # a failing command: the board is written anyway, and says why
    monkeypatch.setattr(env, "board_brief_cmd", f"{sys.executable} -c \"import sys; sys.exit(3)\"")
    assert board.run(dt.date(2026, 10, 1), force=True)
    assert "could not be built this week" in seen["prompt"]


@pytest.mark.parametrize("reply", ['{"description": "x", "body": "no headings"}', "not json"])
def test_a_reply_without_a_board_writes_nothing(env, monkeypatch, reply):
    from ozu import agent, board
    seed(env)
    monkeypatch.setattr(agent, "_run_claude", lambda *a, **k: reply)
    with pytest.raises(ValueError):
        board.run(dt.date(2026, 9, 24))
    assert not (env.vault_dir / "MASTER.md").exists()


def test_sunday_sheet_carries_a_new_edition_once(env, monkeypatch):
    from ozu import agent, board, db, edition, wiki
    seed(env)
    monkeypatch.setattr(agent, "_run_claude", lambda *a, **k: board_reply(
        description="Kettle shipped; the garden went quiet for a third week."))
    assert board.run()
    deliver(monkeypatch)
    sunday = dt.date.today() + dt.timedelta(days=(6 - dt.date.today().weekday()) % 7)
    con = db.connect(env.db_path)
    sheet = wiki.gather_sheet(con, sunday)
    assert sheet["board"]["id"] == board.DEFAULT_ID and sheet["pages"] == []
    assert edition.send("evening", sunday)
    text = pdf_text(next(env.editions_dir.glob("Ozu Evening *.pdf")))
    assert "The board" in text and f"page: {board.DEFAULT_ID}" in text
    assert "garden went quiet" in text and "☐" not in text
    assert wiki.gather_sheet(con, sunday) is None                # marked sent
