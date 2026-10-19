"""@claude work-run tests. The headless Claude Code call is mocked; git is real
(a local bare repo stands in for GitHub).

Covers: the claude_request flag flowing from vision JSON into frontmatter and
db, the governance boundary (walled notes never queue), the repo-map filter,
the run cap, the one-run-per-note ledger surviving an index rebuild, branch
push + companion note, and the API-key strip that keeps agent runs on the
box's claude.ai login instead of the pipeline's API key.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from test_pipeline import env, make_pdf, MOCK_RESULT  # noqa: F401 — shared fixture

GIT_ID = ["-c", "user.email=test@test", "-c", "user.name=test"]


def claude_result(**extra):
    r = dict(MOCK_RESULT)
    r["notes"] = [dict(MOCK_RESULT["notes"][0], claude_request=True, actions=[], **extra)]
    return r


def make_origin(tmp_path: Path) -> Path:
    """A bare repo seeded with one commit, standing in for the GitHub remote."""
    origin = tmp_path / "origin-kettle.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    (seed / "README.md").write_text("# kettle\n")
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
    subprocess.run(["git", *GIT_ID, "-C", str(seed), "commit", "-q", "-m", "seed"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "HEAD"], check=True)
    return origin


def fake_run_claude(prompt, workdir, **kw):
    """Stand-in for the headless run: write a file, commit, report back."""
    (workdir / "app.py").write_text("print('kettle')\n")
    subprocess.run(["git", "-C", str(workdir), "add", "-A"], check=True)
    subprocess.run(["git", *GIT_ID, "-C", str(workdir), "commit", "-q", "-m", "build"],
                   check=True)
    return "Built the pricing skeleton. Could not verify: no tests in repo yet."


def process_one(env, monkeypatch, result, notebook="Inbox", name="a.pdf"):
    from ozu import ingest, understand
    make_pdf(env.raw_dir / notebook / name, ["x"])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: result)
    understand.process_pending()


def map_repo(monkeypatch, origin):
    import ozu.config as config
    monkeypatch.setenv("OZU_CLAUDE_REPOS", f"kettle={origin}")
    config._config = None  # re-read env


def test_claude_request_flows_to_frontmatter_and_db(env, monkeypatch):
    from ozu import db
    process_one(env, monkeypatch, claude_result())
    note = next((env.vault_dir / "topics" / "kettle").glob("*kettle-freemium-pricing*.md"))
    assert "claude: requested" in note.read_text()
    con = db.connect(env.db_path)
    assert con.execute("SELECT claude FROM notes").fetchone()["claude"] == "requested"


def test_walled_note_never_queues_a_run(env, monkeypatch):
    """Governance: @claude on a work page is inert, like edges and actions."""
    from ozu import agent, db
    process_one(env, monkeypatch, claude_result(work_flag=True) | {"work_flag": True},
                notebook="Work", name="w.pdf")
    con = db.connect(env.db_path)
    assert con.execute("SELECT claude FROM notes").fetchone()["claude"] == ""
    assert agent.work_pending() == 0
    assert not env.agent_dir.exists()


def test_unmapped_topic_stays_queued(env, monkeypatch):
    from ozu import agent, db
    process_one(env, monkeypatch, claude_result())
    assert agent.work_pending() == 0  # no OZU_CLAUDE_REPOS at all
    con = db.connect(env.db_path)
    assert con.execute("SELECT claude FROM notes").fetchone()["claude"] == "requested"


def test_work_run_pushes_branch_and_writes_companion_note(env, monkeypatch, tmp_path):
    from ozu import agent, db
    origin = make_origin(tmp_path)
    process_one(env, monkeypatch, claude_result())
    map_repo(monkeypatch, origin)
    monkeypatch.setattr(agent, "_run_claude", fake_run_claude)

    assert agent.work_pending() == 1

    con = db.connect(env.db_path)
    src = con.execute("SELECT * FROM notes WHERE claude='done'").fetchone()
    assert src, "source note must be marked done"
    assert "claude: done" in Path(src["path"]).read_text()

    # the branch reached the origin, and main did not move
    branches = subprocess.run(["git", "-C", str(origin), "branch"],
                              capture_output=True, text=True).stdout
    assert f"claude/{src['id']}" in branches
    log = subprocess.run(["git", "-C", str(origin), "log", "--oneline",
                          f"claude/{src['id']}"], capture_output=True, text=True).stdout
    assert "build" in log and "seed" in log

    companion = next((env.vault_dir / "topics" / "kettle").glob("*claude*.md"))
    text = companion.read_text()
    assert f"claude/{src['id']}" in text and "pricing skeleton" in text
    assert f"[[{src['id']}]]" in text
    edge = con.execute("SELECT * FROM edges WHERE dst=?", (src["id"],)).fetchone()
    assert edge and edge["reason"] == "claude work run"
    assert "Claude run" in (env.vault_dir / "topics" / "kettle" / "index.md").read_text()


def test_one_run_per_note_ever(env, monkeypatch, tmp_path):
    from ozu import agent
    origin = make_origin(tmp_path)
    process_one(env, monkeypatch, claude_result())
    map_repo(monkeypatch, origin)
    monkeypatch.setattr(agent, "_run_claude", fake_run_claude)
    assert agent.work_pending() == 1
    assert agent.work_pending() == 0  # ledger says done; nothing re-runs


def test_run_cap_bounds_a_cycle(env, monkeypatch, tmp_path):
    from ozu import agent, db
    origin = make_origin(tmp_path)
    two = dict(MOCK_RESULT)
    two["notes"] = [
        dict(MOCK_RESULT["notes"][0], claude_request=True, actions=[]),
        dict(MOCK_RESULT["notes"][0], title="Kettle onboarding", slug="kettle-onboarding",
             claude_request=True, actions=[]),
    ]
    process_one(env, monkeypatch, two)
    monkeypatch.setenv("OZU_MAX_CLAUDE_RUNS", "1")
    map_repo(monkeypatch, origin)
    monkeypatch.setattr(agent, "_run_claude", fake_run_claude)
    assert agent.work_pending() == 1
    con = db.connect(env.db_path)
    left = con.execute("SELECT COUNT(*) c FROM notes WHERE claude='requested'").fetchone()["c"]
    assert left == 1  # the second note waits for the next cycle


def test_failed_run_is_marked_error_not_retried(env, monkeypatch, tmp_path):
    from ozu import agent, db
    origin = make_origin(tmp_path)
    process_one(env, monkeypatch, claude_result())
    map_repo(monkeypatch, origin)

    def boom(prompt, workdir):
        raise RuntimeError("claude exited 1")

    monkeypatch.setattr(agent, "_run_claude", boom)
    assert agent.work_pending() == 1
    con = db.connect(env.db_path)
    row = con.execute("SELECT claude, path FROM notes").fetchone()
    assert row["claude"] == "error"
    assert "claude: error" in Path(row["path"]).read_text()
    assert agent.work_pending() == 0  # error is terminal, not a retry loop


def test_ledger_survives_index_rebuild(env, monkeypatch, tmp_path):
    """ozu.db is derived: rebuilding it must not resurrect a finished run."""
    from ozu import agent, db, indexer
    origin = make_origin(tmp_path)
    process_one(env, monkeypatch, claude_result())
    map_repo(monkeypatch, origin)
    monkeypatch.setattr(agent, "_run_claude", fake_run_claude)
    agent.work_pending()

    env.db_path.unlink()
    indexer.rebuild()
    con = db.connect(env.db_path)
    assert con.execute("SELECT claude FROM notes WHERE claude='done'").fetchone()
    assert agent.work_pending() == 0


def test_agent_env_does_not_leak_api_key(env, monkeypatch, tmp_path):
    """Runs bill the box's claude.ai login; the CLI must never see the
    pipeline's ANTHROPIC_API_KEY or it silently switches to API billing."""
    from ozu import agent
    seen = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw.get("env")
        seen["cmd"] = cmd

        class P:
            returncode = 0
            stdout = json.dumps({"result": "ok", "is_error": False})
            stderr = ""
        return P()

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    assert agent._run_claude("prompt", tmp_path, writes=True) == "ok"
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert "--permission-mode" in seen["cmd"] and "acceptEdits" in seen["cmd"]


def _capture_cmds(monkeypatch, agent):
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)

        class P:
            returncode = 0
            stdout = json.dumps({"result": "ok", "is_error": False})
            stderr = ""
        return P()

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    return seen


def test_a_run_is_read_only_unless_its_caller_says_it_writes(env, monkeypatch, tmp_path):
    """acceptEdits approves in-workdir edits and filesystem Bash whatever the
    allowlist says (measured 2026-10-01), so a text-only run must not get it."""
    from ozu import agent
    seen = _capture_cmds(monkeypatch, agent)
    agent._run_claude("prompt", tmp_path, allowed_tools="Read(//x/**)")
    cmd = seen[-1]
    assert "acceptEdits" not in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
    for tool in ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        assert tool in denied


def test_no_run_loads_an_mcp_server(env, monkeypatch, tmp_path):
    """Ozu's own MCP server can write to the vault (`capture`), and it may be
    registered for the user's interactive sessions. A headless run, writing
    or not, must start with no MCP server at all."""
    from ozu import agent
    seen = _capture_cmds(monkeypatch, agent)
    agent._run_claude("prompt", tmp_path, allowed_tools="Read(//x/**)")
    agent._run_claude("prompt", tmp_path, writes=True)
    for cmd in seen:
        assert "--strict-mcp-config" in cmd
        assert "--mcp-config" not in cmd


def test_only_the_runs_that_change_code_may_write(monkeypatch, env):
    """The work run and Build execution edit a clone; learn, research, the
    wiki and a Build proposal only return text, so none of them passes
    writes=True. Pinned at the source so a new caller cannot slip in."""
    from ozu import agent
    src = Path(agent.__file__).parent
    writers = []
    for f in sorted(src.glob("*.py")):
        writers += [f.name] * f.read_text().count("writes=True")
    assert sorted(writers) == ["agent.py", "builds.py"]


def test_a_headless_run_inherits_no_secrets(env, monkeypatch, tmp_path):
    """The .env's values (GitHub token, SMTP password, API key) never reach a
    run: a work run executes tests it may have written, and the pipeline
    pushes what it leaves behind."""
    from types import SimpleNamespace
    from ozu import agent
    monkeypatch.setenv("OZU_SMTP_PASS", "s")
    monkeypatch.setenv("OZU_GITHUB_TOKEN", "g")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    seen = {}

    def run(cmd, **kw):
        seen.update(kw["env"])
        return SimpleNamespace(stdout='{"result": "ok"}', stderr="", returncode=0)
    monkeypatch.setattr(agent.subprocess, "run", run)
    agent._run_claude("p", tmp_path, allowed_tools="Read")
    assert set(seen) <= set(agent.RUN_ENV_KEYS)
    assert seen["CLAUDE_CODE_OAUTH_TOKEN"] == "t" and "ANTHROPIC_API_KEY" not in seen


def test_file_tools_are_pinned_to_the_working_directory(tmp_path):
    """Claude Code confines Bash to the workdir but not the file tools: a bare
    `Read` in the allowlist opens ~/ozu-data/.env, a bare `Write` writes anywhere
    the box's user can. Only the path-scoped form contains a run."""
    import os.path
    from ozu import agent
    tools = agent.work_tools(tmp_path)
    scope = "//" + os.path.abspath(tmp_path).lstrip("/") + "/**"
    assert f"Read({scope})" in tools and f"Edit({scope})" in tools
    for bare in ("Read,", "Edit,", "Write,", "Glob,", "Grep,"):
        assert bare not in tools + ","


def test_work_run_can_run_tests_but_not_a_general_shell(tmp_path):
    from ozu import agent
    tools = agent.work_tools(tmp_path)
    for allowed in ("Bash(git:*)", "Bash(pytest:*)", "Bash(npm test:*)",
                    "Bash(make test:*)"):
        assert allowed in tools
    for refused in ("Bash(pip", "Bash(npm install", "Bash(systemctl",
                    "Bash(curl", "Bash(rm", "Bash(sudo"):
        assert refused not in tools


def test_pipeline_owns_pushing(env, monkeypatch, tmp_path):
    """`git:*` would otherwise let a run push straight to the default branch."""
    from ozu import agent
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd

        class P:
            returncode = 0
            stdout = json.dumps({"result": "ok", "is_error": False})
            stderr = ""
        return P()

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    agent._run_claude("prompt", tmp_path)
    i = seen["cmd"].index("--disallowedTools")
    assert "Bash(git push:*)" in seen["cmd"][i + 1]


def test_digest_surfaces_work_runs(env, monkeypatch, tmp_path):
    from ozu import agent, digest
    origin = make_origin(tmp_path)
    two = dict(MOCK_RESULT)
    two["notes"] = [
        dict(MOCK_RESULT["notes"][0], claude_request=True, actions=[]),
        dict(MOCK_RESULT["notes"][0], title="Garden idea", slug="garden-idea",
             topic="garden", claude_request=True, actions=[]),
    ]
    process_one(env, monkeypatch, two)
    map_repo(monkeypatch, origin)  # kettle mapped, garden not
    monkeypatch.setattr(agent, "_run_claude", fake_run_claude)
    agent.work_pending()

    text = digest.build_digest()
    assert "Claude built (@claude):" in text
    assert "review before merging" in text
    assert "no repo mapped for 'garden'" in text


def test_open_pr_sends_report_and_returns_url(env, monkeypatch):
    from ozu import agent
    import ozu.config as config
    monkeypatch.setenv("OZU_GITHUB_TOKEN", "github_pat_test")
    config._config = None
    seen = {}

    class FakeResp:
        def read(self):
            return json.dumps({"html_url": "https://github.com/o/r/pull/7"}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        seen["payload"] = json.loads(req.data)
        return FakeResp()

    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_urlopen)
    note = {"id": "20260819-x", "topic": "kettle", "title": "Kettle idea"}
    url = agent._open_pr("o/r", "main", "claude/20260819-x", note, "the report")
    assert url == "https://github.com/o/r/pull/7"
    assert seen["url"] == "https://api.github.com/repos/o/r/pulls"
    assert seen["auth"] == "Bearer github_pat_test"
    p = seen["payload"]
    assert p["head"] == "claude/20260819-x" and p["base"] == "main"
    assert "the report" in p["body"] and "20260819-x" in p["body"]


def test_no_token_means_no_pr(env, monkeypatch):
    from ozu import agent

    def explode(*a, **k):
        raise AssertionError("no API call should happen without a token")

    monkeypatch.setattr(agent.urllib.request, "urlopen", explode)
    assert agent._open_pr("o/r", "main", "b", {"id": "x", "topic": "kettle",
                                               "title": "t"}, "r") is None


def test_pr_url_lands_in_companion_note(env, monkeypatch, tmp_path):
    from ozu import agent
    origin = make_origin(tmp_path)
    process_one(env, monkeypatch, claude_result())
    map_repo(monkeypatch, origin)
    monkeypatch.setattr(agent, "_run_claude", fake_run_claude)
    monkeypatch.setattr(agent, "_github_repo", lambda url: "example/kettle")
    monkeypatch.setattr(agent, "_open_pr",
                        lambda *a, **k: "https://github.com/example/kettle/pull/2")
    assert agent.work_pending() == 1
    companion = next((env.vault_dir / "topics" / "kettle").glob("*claude*.md"))
    assert "https://github.com/example/kettle/pull/2" in companion.read_text()


def test_pr_failure_falls_back_to_compare_link(env, monkeypatch, tmp_path):
    """The branch is already pushed by then; a failed PR must not fail the run."""
    from ozu import agent, db
    origin = make_origin(tmp_path)
    process_one(env, monkeypatch, claude_result())
    map_repo(monkeypatch, origin)
    monkeypatch.setattr(agent, "_run_claude", fake_run_claude)
    monkeypatch.setattr(agent, "_github_repo", lambda url: "example/kettle")

    def boom(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(agent, "_open_pr", boom)
    assert agent.work_pending() == 1
    con = db.connect(env.db_path)
    assert con.execute("SELECT claude FROM notes WHERE claude='done'").fetchone()
    companion = next((env.vault_dir / "topics" / "kettle").glob("*claude*.md"))
    assert "compare/" in companion.read_text()
