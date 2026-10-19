"""P3: the publish state machine against a throwaway bare origin.

Covers AC 2 (re-drop no-dup), AC 4 (crash resume, both windows), AC 5
(ledger loss + FR-22), AC 6 (walled never reaches the vault), AC 8
(no speech), and FR-33 (remote guard). No real remote is touched.
"""
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from ozu_capture import config, ledger, note, run, scan, vaultops
from unittest import mock

FAKE_WHISPER = Path(__file__).parent / "fake_whisper.py"

INDEX_HEADER = """# Knowledge Base — Index

## How to use this KB (for agents)
1. Read this index first.

## Registry
"""


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=False)


class PublishWorld(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.inbox = root / "inbox"
        self.inbox.mkdir()
        os.environ["OZU_CAPTURE_DATA"] = str(root / "data")
        os.environ.update({
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com",
        })
        # bare origin seeded with a scaffolded vault
        self.origin = root / "origin.git"
        git(["init", "--bare", "-q", "-b", "main", str(self.origin)], root)
        seed = root / "seed"
        git(["clone", "-q", str(self.origin), str(seed)], root)
        (seed / "INDEX.md").write_text(INDEX_HEADER)
        git(["add", "-A"], seed)
        git(["commit", "-q", "-m", "scaffold"], seed)
        git(["push", "-q", "origin", "main"], seed)

        from tests.test_config_ledger import write_config
        self.cfg = config.load(write_config(
            root,
            inbox_override=f'"{self.inbox}"',
            quiesce="10",
            cmd=f'["{sys.executable}", "{FAKE_WHISPER}"]',
            remote=f'"{self.origin}"'))
        self.cfg.ensure_dirs()

    def tearDown(self):
        self.tmp.cleanup()
        for k in ("OZU_CAPTURE_DATA", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                  "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            os.environ.pop(k, None)

    # -- helpers ---------------------------------------------------------

    def drop_stable(self, name: str, data: bytes) -> Path:
        """Drop a file and pre-age its observation so the next sweep sees
        it stable (the integration tests should not sleep 60s)."""
        p = self.inbox / name
        p.write_bytes(data)
        st = p.stat()
        con = ledger.connect(self.cfg.ledger_path)
        ledger.observe(con, str(p), st.st_size, st.st_mtime,
                       time.time() - 100, self.cfg.quiesce_seconds)
        con.close()
        return p

    def origin_commits(self) -> list[str]:
        r = git(["log", "--format=%s", "main"], self.origin)
        return r.stdout.strip().splitlines() if r.returncode == 0 else []

    def state_of(self, sha: str) -> str:
        con = ledger.connect(self.cfg.ledger_path)
        row = ledger.get(con, sha)
        con.close()
        return row["state"] if row else "(absent)"

    # -- the tests -------------------------------------------------------

    def test_end_to_end_and_redrop(self):
        p = self.drop_stable("memo one.m4a", b"AUDIO-1" * 100)
        sha = run.sha256_file(p)
        self.assertEqual(run.run_once(self.cfg), 0)

        self.assertEqual(self.origin_commits()[0],
                         "voice: memo one (memo one.m4a)")
        self.assertEqual(self.state_of(sha), "done")
        self.assertTrue((self.inbox / "processed" / "memo one.m4a").exists())
        note_path = next((self.cfg.vault_clone / "topics/voice").glob("*-memo-one.md"))
        text = note_path.read_text()
        self.assertIn(f"source_sha256: {sha}", text)
        self.assertIn("**[00:00:00]**", text)
        self.assertEqual((self.cfg.vault_clone / "INDEX.md").read_text(), INDEX_HEADER)  # never touched

        # AC 2: same bytes, different name -> nothing new anywhere
        n_commits = len(self.origin_commits())
        self.drop_stable("renamed copy.m4a", b"AUDIO-1" * 100)
        run.run_once(self.cfg)
        self.assertEqual(len(self.origin_commits()), n_commits)
        self.assertEqual(self.state_of(sha), "done")
        self.assertTrue((self.inbox / "processed" / "renamed copy.m4a").exists())

    def test_ledger_loss_cannot_duplicate(self):
        p = self.drop_stable("memo.m4a", b"AUDIO-2" * 100)
        sha = run.sha256_file(p)
        run.run_once(self.cfg)
        self.assertEqual(self.state_of(sha), "done")

        self.cfg.ledger_path.unlink()          # AC 5
        self.drop_stable("memo again.m4a", b"AUDIO-2" * 100)
        n_commits = len(self.origin_commits())
        run.run_once(self.cfg)
        self.assertEqual(len(self.origin_commits()), n_commits)
        con = ledger.connect(self.cfg.ledger_path)
        row = ledger.get(con, sha)
        con.close()
        self.assertEqual(row["state"], "done")
        self.assertIn("already in vault", row["detail"])

    def test_push_failure_keeps_commit_and_resumes(self):
        # first e2e so the clone exists
        self.drop_stable("first.m4a", b"AUDIO-3" * 100)
        run.run_once(self.cfg)
        # origin rejects writes
        for d, _, files in os.walk(self.origin):
            os.chmod(d, 0o555)
            for f in files:
                os.chmod(Path(d) / f, 0o444)
        try:
            p = self.drop_stable("second.m4a", b"AUDIO-4" * 100)
            sha = run.sha256_file(p)
            run.run_once(self.cfg)
            self.assertEqual(self.state_of(sha), "committed")   # AC 4, half 2
            self.assertTrue(p.exists())                          # not tidied
        finally:
            for d, _, files in os.walk(self.origin):
                os.chmod(d, 0o755)
                for f in files:
                    os.chmod(Path(d) / f, 0o644)
        run.run_once(self.cfg)                                   # resume tick
        self.assertEqual(self.state_of(sha), "done")
        self.assertEqual(
            [c for c in self.origin_commits() if "second" in c],
            ["voice: second (second.m4a)"])                      # exactly once

    def test_crash_mid_transcription_reprocesses_once(self):
        p = self.drop_stable("memo.m4a", b"AUDIO-5" * 100)
        sha = run.sha256_file(p)
        # simulate the kill: ledger says new, a partial tmp file exists
        con = ledger.connect(self.cfg.ledger_path)
        ledger.upsert(con, sha, path=str(p), size=p.stat().st_size,
                      state="new")
        con.close()
        (self.cfg.tmp_dir / f"{sha}.md").write_text("partial garbage")
        run.run_once(self.cfg)                                   # AC 4, half 1
        self.assertEqual(self.state_of(sha), "done")
        self.assertEqual(
            [c for c in self.origin_commits() if "memo" in c],
            ["voice: memo (memo.m4a)"])

    def test_walled_never_reaches_vault(self):
        p = self.drop_stable("work standup.m4a", b"AUDIO-6" * 100)
        sha = run.sha256_file(p)
        n_commits = len(self.origin_commits())
        run.run_once(self.cfg)
        self.assertEqual(self.state_of(sha), "walled")           # AC 6
        walled = list(self.cfg.walled_dir.glob("*.md"))
        self.assertEqual([w.name for w in walled], ["work standup.md"])
        self.assertEqual(len(self.origin_commits()), n_commits)  # no commit
        self.assertFalse((self.cfg.vault_clone / "topics/voice").exists()
                         and any((self.cfg.vault_clone / "topics/voice")
                                 .glob("work*")))
        self.assertTrue((self.inbox / "processed" / "work standup.m4a").exists())

    def test_no_speech_produces_no_note(self):
        p = self.drop_stable("silent room.m4a", b"AUDIO-7" * 100)
        sha = run.sha256_file(p)
        n_commits = len(self.origin_commits())
        run.run_once(self.cfg)
        self.assertEqual(self.state_of(sha), "no_speech")        # AC 8
        self.assertEqual(len(self.origin_commits()), n_commits)
        self.assertTrue((self.inbox / "processed" / "silent room.m4a").exists())

    def test_cli_failure_leaves_marker_and_audio(self):
        p = self.drop_stable("boom take.m4a", b"AUDIO-8" * 100)
        sha = run.sha256_file(p)
        run.run_once(self.cfg)
        self.assertEqual(self.state_of(sha), "failed")
        self.assertTrue(p.exists())                              # audio stays
        marker = scan.marker_path(p)
        self.assertIn("engine exploded", marker.read_text())
        # marker gates the next tick entirely
        run.run_once(self.cfg)
        self.assertEqual(self.state_of(sha), "failed")

    def test_finished_transcript_reused_after_vault_failure(self):
        # 'boom' would make the stub CLI fail — so if this passes, the
        # pre-existing finished transcript was reused, not re-transcribed.
        p = self.drop_stable("boom reuse.m4a", b"AUDIO-13" * 100)
        sha = run.sha256_file(p)
        self.cfg.ensure_dirs()
        from tests.fake_whisper import GOLDEN
        (self.cfg.tmp_dir / f"{sha}.md").write_text(
            GOLDEN.format(title="boom reuse", name="boom reuse.m4a"))
        run.run_once(self.cfg)
        self.assertEqual(self.state_of(sha), "done")
        self.assertFalse((self.cfg.tmp_dir / f"{sha}.md").exists())
        self.assertIn("voice: boom reuse (boom reuse.m4a)",
                      self.origin_commits())

    def test_remote_guard_refuses_wrong_origin(self):
        self.drop_stable("first.m4a", b"AUDIO-9" * 100)
        run.run_once(self.cfg)
        git(["remote", "set-url", "origin", "https://github.com/evil/other.git"],
            self.cfg.vault_clone)
        p = self.drop_stable("second.m4a", b"AUDIO-10" * 100)
        sha = run.sha256_file(p)
        n_commits = len(self.origin_commits())
        run.run_once(self.cfg)                                   # FR-33
        self.assertEqual(self.state_of(sha), "new")              # queued, not lost
        self.assertEqual(len(self.origin_commits()), n_commits)
        self.assertTrue(p.exists())

    def test_dirty_clone_is_reset_and_processing_continues(self):
        self.drop_stable("first.m4a", b"AUDIO-11" * 100)
        run.run_once(self.cfg)
        (self.cfg.vault_clone / "stray-crash-artifact.md").write_text("junk")
        p = self.drop_stable("second.m4a", b"AUDIO-12" * 100)
        sha = run.sha256_file(p)
        run.run_once(self.cfg)
        self.assertEqual(self.state_of(sha), "done")
        self.assertFalse(
            (self.cfg.vault_clone / "stray-crash-artifact.md").exists())


if __name__ == "__main__":
    unittest.main()


class NoAutoMaintenanceTest(unittest.TestCase):
    """Every git the bridge runs disables auto-maintenance.

    `git fetch` ends by spawning `git maintenance run --auto --detach`, which
    inherits our stdout/stderr pipes; capture_output then blocks until that
    detached child exits, and the call dies on its timeout with the push or
    pull already done. Observed on the NAS 2026-09-02. The flags are the fix,
    so they are what this pins — the hang itself needs a slow box to show.
    """

    def test_flags_precede_every_subcommand(self):
        seen = []

        def fake_run(args, **kw):
            seen.append((args, kw))
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(vaultops.subprocess, "run", fake_run):
            vaultops._git(["status", "--porcelain"], Path("/tmp"))

        args, kw = seen[0]
        self.assertEqual(args[:5], ["git", "-c", "gc.auto=0",
                                    "-c", "maintenance.auto=false"])
        self.assertEqual(args[5:], ["status", "--porcelain"])
        # A detached child must not be able to hold a pipe we read, and no
        # git may ever sit waiting for input that cannot come.
        self.assertIs(kw["stdin"], subprocess.DEVNULL)


class RemoteStallTest(unittest.TestCase):
    """A stalled remote defers; it never crashes a tick or loses a commit.

    The NAS's path to github.com:22 goes away for minutes at a time (measured
    2026-09-02: five consecutive `ls-remote` timeouts between two 1.4 s ones).
    The state machine already knows how to wait — these paths just have to
    reach it instead of raising TimeoutExpired at the loop.
    """

    def setUp(self):
        self.cfg = mock.Mock(branch="main", approved_remote="git@github.com:x/y.git")

    def _timeout(self, *a, **kw):
        raise subprocess.TimeoutExpired(["git"], vaultops.NETWORK_TIMEOUT)

    def test_stalled_pull_defers(self):
        with mock.patch.object(vaultops, "_git", side_effect=self._timeout):
            with self.assertRaises(vaultops.SyncDeferred) as e:
                vaultops.sync(self.cfg, Path("/tmp"))
        self.assertIn("stalled", str(e.exception))

    def test_stalled_push_defers(self):
        with mock.patch.object(vaultops, "_guard"), \
             mock.patch.object(vaultops, "_git", side_effect=self._timeout):
            with self.assertRaises(vaultops.PushDeferred) as e:
                vaultops.push(self.cfg, Path("/tmp"))
        self.assertIn("stalled", str(e.exception))

    def test_stall_window_is_longer_than_a_minute(self):
        # Two minutes aborted ticks that were seconds from succeeding.
        self.assertGreaterEqual(vaultops.NETWORK_TIMEOUT, 300)
