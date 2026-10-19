"""P0: config validation and every ledger transition."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from ozu_capture import config, ledger


def write_config(dir_: Path, **overrides) -> Path:
    base = {
        "share_url": '"smb://user@nas.local/VoiceInbox"',
        "inbox_override": '""',
        "quiesce": "60",
        "cmd": '["/bin/echo", "stub"]',
        "remote": '"https://github.com/you/vault.git"',
    }
    base.update(overrides)
    p = dir_ / "config.toml"
    p.write_text(f"""
[nas]
share_url = {base['share_url']}
processed_subdir = "processed"
inbox_override = {base['inbox_override']}
[bridge]
poll_quiesce_seconds = {base['quiesce']}
walled_prefixes = ["work"]
[whisper]
cmd = {base['cmd']}
cwd = {base.get('whisper_cwd', '""')}
extra_path = {base.get('whisper_extra_path', '[]')}
timeout_seconds = 60
[vault]
approved_remote = {base['remote']}
branch = "main"
topic = "voice"
[agent]
label = "test.ozu-capture"
start_interval = 120
""")
    return p


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        os.environ["OZU_CAPTURE_DATA"] = str(self.dir / "data")

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("OZU_CAPTURE_DATA", None)

    def test_repo_config_is_valid(self):
        cfg = config.load(config.REPO_ROOT / "config.toml")
        self.assertTrue(cfg.approved_remote.endswith("vault.git"))
        self.assertEqual(cfg.topic, "voice")
        self.assertIn("work", cfg.walled_prefixes)

    def test_missing_remote_refused(self):
        p = write_config(self.dir, remote='""')
        with self.assertRaises(config.ConfigError):
            config.load(p)

    def test_short_quiesce_refused(self):
        p = write_config(self.dir, quiesce="5")
        with self.assertRaises(config.ConfigError):
            config.load(p)

    def test_empty_whisper_cmd_refused(self):
        p = write_config(self.dir, cmd="[]")
        with self.assertRaises(config.ConfigError):
            config.load(p)


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = ledger.connect(Path(self.tmp.name) / "ledger.db")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_state_machine_roundtrip(self):
        ledger.upsert(self.con, "abc", path="/x/a.m4a", size=10, state="new")
        for state in ("committed", "pushed", "done"):
            ledger.set_state(self.con, "abc", state)
            self.assertEqual(ledger.get(self.con, "abc")["state"], state)
        self.assertIn("done", ledger.TERMINAL)
        self.assertEqual(ledger.counts(self.con), {"done": 1})

    def test_note_id_survives_state_updates(self):
        ledger.upsert(self.con, "abc", path="/x/a.m4a", size=10,
                      state="committed", note_id="20260829-a")
        ledger.upsert(self.con, "abc", path="/x/b.m4a", size=10, state="pushed")
        self.assertEqual(ledger.get(self.con, "abc")["note_id"], "20260829-a")

    def test_quiesce_gate(self):
        t0 = time.time()
        # first sighting: never stable
        self.assertFalse(ledger.observe(self.con, "/x/a.m4a", 100, 1.0, t0, 60))
        # unchanged but too soon
        self.assertFalse(ledger.observe(self.con, "/x/a.m4a", 100, 1.0, t0 + 30, 60))
        # changed size: clock resets
        self.assertFalse(ledger.observe(self.con, "/x/a.m4a", 200, 2.0, t0 + 30, 60))
        self.assertFalse(ledger.observe(self.con, "/x/a.m4a", 200, 2.0, t0 + 60, 60))
        # unchanged and old enough
        self.assertTrue(ledger.observe(self.con, "/x/a.m4a", 200, 2.0, t0 + 95, 60))

    def test_prune_observations(self):
        ledger.observe(self.con, "/x/a.m4a", 1, 1.0, 0.0, 60)
        ledger.observe(self.con, "/x/b.m4a", 1, 1.0, 0.0, 60)
        ledger.prune_observations(self.con, {"/x/b.m4a"})
        rows = self.con.execute("SELECT path FROM observations").fetchall()
        self.assertEqual([r["path"] for r in rows], ["/x/b.m4a"])


if __name__ == "__main__":
    unittest.main()


class TestLedgerMigration(unittest.TestCase):
    def test_pre_outcome_videos_table_is_altered_in_place(self):
        # a db from before videos grew the outcome column (the Mac's ledger)
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ledger.db"
            old = sqlite3.connect(db)
            old.execute("""CREATE TABLE videos(
                video_id TEXT PRIMARY KEY, title TEXT DEFAULT '',
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT DEFAULT '', note_id TEXT DEFAULT '',
                first_seen TEXT NOT NULL, updated TEXT NOT NULL)""")
            old.execute("INSERT INTO videos VALUES('v1','t','done',1,'','n',"
                        "'2026-09-01','2026-09-01')")
            old.commit()
            old.close()
            con = ledger.connect(db)
            self.assertEqual(ledger.video_counts(con), {"done": 1})
            self.assertEqual(con.execute(
                "SELECT outcome FROM videos WHERE video_id='v1'"
            ).fetchone()["outcome"], "")
            con.close()
