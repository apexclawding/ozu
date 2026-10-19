"""P1: accept list, quiesce gate, markers, wall prefixes, processed moves."""
import tempfile
import unittest
from pathlib import Path

from ozu_capture import config, ledger, scan
from tests.test_config_ledger import write_config


class ScanWorld(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.inbox = root / "inbox"
        self.inbox.mkdir()
        import os
        os.environ["OZU_CAPTURE_DATA"] = str(root / "data")
        self.cfg = config.load(write_config(
            root, inbox_override=f'"{self.inbox}"'))
        self.con = ledger.connect(self.cfg.ledger_path)

    def tearDown(self):
        import os
        self.con.close()
        self.tmp.cleanup()
        os.environ.pop("OZU_CAPTURE_DATA", None)

    def drop(self, name: str, data: bytes = b"x" * 100) -> Path:
        p = self.inbox / name
        p.write_bytes(data)
        return p

    def test_slow_upload_not_picked_up_until_stable(self):
        p = self.drop("memo.m4a")
        t0 = 1000.0
        s = scan.sweep_inbox(self.inbox, self.con, self.cfg, now=t0)
        self.assertEqual(s.eligible, [])          # first sighting
        self.assertEqual(s.waiting, [p])
        p.write_bytes(b"x" * 200)                 # still uploading
        s = scan.sweep_inbox(self.inbox, self.con, self.cfg, now=t0 + 120)
        self.assertEqual(s.eligible, [])          # size changed: clock reset
        s = scan.sweep_inbox(self.inbox, self.con, self.cfg, now=t0 + 180)
        self.assertEqual(s.eligible, [p])         # stable for >= 60s

    def test_unsupported_extension_marked_once(self):
        p = self.drop("android.amr")
        t = 1000.0
        scan.sweep_inbox(self.inbox, self.con, self.cfg, now=t)
        marker = scan.marker_path(p)
        self.assertTrue(marker.exists())
        self.assertIn("m4a", marker.read_text())
        first = marker.stat().st_mtime_ns
        scan.sweep_inbox(self.inbox, self.con, self.cfg, now=t + 120)
        self.assertEqual(marker.stat().st_mtime_ns, first)   # not rewritten
        # marker gates re-pickup; deleting it retries (here: re-marks)
        marker.unlink()
        s = scan.sweep_inbox(self.inbox, self.con, self.cfg, now=t + 240)
        self.assertTrue(scan.marker_path(p).exists())
        self.assertNotIn(p, s.eligible)

    def test_marker_excludes_audio_from_pickup(self):
        p = self.drop("memo.m4a")
        scan.write_marker(p, "transcription failed")
        t = 1000.0
        s = scan.sweep_inbox(self.inbox, self.con, self.cfg, now=t)
        self.assertEqual(s.eligible, [])
        self.assertEqual(s.marked, [p])
        s = scan.sweep_inbox(self.inbox, self.con, self.cfg, now=t + 120)
        self.assertEqual(s.eligible, [])          # still gated

    def test_hidden_zero_byte_and_marker_files_ignored(self):
        self.drop("._resourcefork.m4a")
        self.drop("empty.m4a", b"")
        (self.inbox / "old.m4a.failed.txt").write_text("marker itself")
        s = scan.sweep_inbox(self.inbox, self.con, self.cfg, now=1000.0)
        self.assertEqual((s.eligible, s.waiting, s.marked), ([], [], []))

    def test_walled_prefixes(self):
        for name, walled in [("work-standup.m4a", True), ("WORK notes.m4a", True),
                             ("Work call.m4a", True), ("workout plan.m4a", True),
                             ("garden ideas.m4a", False), ("groceries.m4a", False)]:
            self.assertEqual(
                scan.is_walled(Path(name), self.cfg.walled_prefixes), walled,
                name)

    def test_move_to_processed_with_collision(self):
        a = self.drop("memo.m4a", b"first")
        d1 = scan.move_to_processed(a, self.inbox, self.cfg)
        b = self.drop("memo.m4a", b"second")
        d2 = scan.move_to_processed(b, self.inbox, self.cfg)
        self.assertEqual(d1.name, "memo.m4a")
        self.assertEqual(d2.name, "memo-2.m4a")
        self.assertEqual(d2.read_bytes(), b"second")

    def test_oldest_first(self):
        import os
        a = self.drop("newer.m4a")
        b = self.drop("older.m4a")
        os.utime(a, (2000, 2000))
        os.utime(b, (1000, 1000))
        t = 5000.0
        scan.sweep_inbox(self.inbox, self.con, self.cfg, now=t)
        s = scan.sweep_inbox(self.inbox, self.con, self.cfg, now=t + 120)
        self.assertEqual([p.name for p in s.eligible],
                         ["older.m4a", "newer.m4a"])


if __name__ == "__main__":
    unittest.main()
