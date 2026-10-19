"""P4: plist rendering and the remount backoff (no launchctl calls)."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from ozu_capture import agentctl, config, run
from tests.test_config_ledger import write_config


class TestAgentAndBackoff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["OZU_CAPTURE_DATA"] = str(root / "data")
        self.cfg = config.load(write_config(root, inbox_override='""'))
        self.cfg.ensure_dirs()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("OZU_CAPTURE_DATA", None)

    def test_plist_renders_absolute_and_per_login(self):
        p = agentctl.render(self.cfg)
        self.assertEqual(p["Label"], "test.ozu-capture")
        self.assertEqual(p["StartInterval"], 120)
        self.assertTrue(p["RunAtLoad"])
        self.assertTrue(Path(p["ProgramArguments"][0]).is_absolute())
        self.assertEqual(p["ProgramArguments"][1], "run-once")

    def test_launchd_logs_stay_out_of_tcc_protected_folders(self):
        p = agentctl.render(self.cfg)
        protected = [Path.home() / d for d in ("Documents", "Desktop",
                                               "Downloads")]
        for key in ("StandardOutPath", "StandardErrorPath"):
            path = Path(p[key])
            self.assertTrue(path.is_absolute())
            for d in protected:
                self.assertFalse(path.is_relative_to(d), f"{key} under {d}")

    def test_remount_backoff(self):
        self.assertTrue(run._remount_allowed(self.cfg))     # no flag yet
        run._log_mount_state(self.cfg, mounted=False)       # down: flag set
        self.assertFalse(run._remount_allowed(self.cfg))    # fresh flag gates
        old = time.time() - run.REMOUNT_BACKOFF_SECONDS - 5
        os.utime(run._mount_flag(self.cfg), (old, old))
        self.assertTrue(run._remount_allowed(self.cfg))     # aged out
        run._log_mount_state(self.cfg, mounted=False)       # failed again:
        self.assertFalse(run._remount_allowed(self.cfg))    # clock restamped
        run._log_mount_state(self.cfg, mounted=True)        # back: flag gone
        self.assertTrue(run._remount_allowed(self.cfg))
        self.assertFalse(run._mount_flag(self.cfg).exists())


if __name__ == "__main__":
    unittest.main()
