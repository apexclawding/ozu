"""_git_commit must leave the tree clean even where git has no identity.

Caught by the first CI run (2026-09-05): on a bare runner `git commit`
refuses without user.email, _git_commit swallowed the refusal, and the
staged deletions sat in the tree — which on the live loop is what wedges
the next `git pull --rebase`. The VPS never showed it because
~/.gitconfig has an identity.
"""
import subprocess

from ozu import vault


def _status(repo):
    return subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                          capture_output=True, text=True).stdout


def test_git_commit_without_identity(tmp_path, monkeypatch):
    # Hide global/system config so the environment matches a bare runner.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    repo = tmp_path / "vault"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "note.md").write_text("x\n")

    vault._git_commit(repo, "pipeline: test")

    assert _status(repo) == ""
    author = subprocess.run(["git", "log", "-1", "--format=%ae"], cwd=repo,
                            capture_output=True, text=True).stdout.strip()
    assert author == "ozu@localhost"


def test_git_commit_keeps_an_existing_identity(tmp_path):
    repo = tmp_path / "vault"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "s@example.com"],
                   cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Owner"], cwd=repo,
                   check=True)
    (repo / "note.md").write_text("x\n")

    vault._git_commit(repo, "pipeline: test")

    assert _status(repo) == ""
    author = subprocess.run(["git", "log", "-1", "--format=%ae"], cwd=repo,
                            capture_output=True, text=True).stdout.strip()
    assert author == "s@example.com"


def test_git_commit_with_nothing_to_commit_is_quiet(tmp_path, caplog):
    repo = tmp_path / "vault"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "s@example.com"],
                   cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Owner"], cwd=repo,
                   check=True)

    with caplog.at_level("WARNING", logger="ozu.vault"):
        vault._git_commit(repo, "pipeline: nothing")
    assert not caplog.records
