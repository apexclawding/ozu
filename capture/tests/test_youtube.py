"""The youtube source (Amendment 1 §6.1): VTT transform, caption
choice, and the tick's state machine against a throwaway bare origin with
a fake yt-dlp. No real network, no real remote.

Covers Amendment 1 AC 2 (re-pin no-dup), AC 3 (ledger loss +
vault-side backstop), AC 4's logic (no captions → hand-off → the voice tick
files a learning transcript), AC 5 (too long → skipped once), AC 6 (the
golden VTT), plus fetch failures, the listing-failure path, the poll gate,
push deferral and the retry-by-deleting-the-row gesture.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from ozu_capture import config, ledger, note, run, youtube

FAKE_YTDLP = Path(__file__).parent / "fake_ytdlp.py"
FAKE_WHISPER = Path(__file__).parent / "fake_whisper.py"
FIXTURES = Path(__file__).parent / "fixtures"

INDEX_HEADER = """# Knowledge Base — Index

## How to use this KB (for agents)
1. Read this index first.

## Registry
"""


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=False)


def seed_origin(root: Path) -> Path:
    origin = root / "origin.git"
    git(["init", "--bare", "-q", "-b", "main", str(origin)], root)
    seed = root / "seed"
    git(["clone", "-q", str(origin), str(seed)], root)
    (seed / "INDEX.md").write_text(INDEX_HEADER)
    git(["add", "-A"], seed)
    git(["commit", "-q", "-m", "scaffold"], seed)
    git(["push", "-q", "origin", "main"], seed)
    return origin


def write_yt_config(root: Path, *, remote: str, handoff: str,
                    fallback: str = "true", max_minutes: str = "180",
                    max_per_tick: str = "3",
                    playlist_url: str = "https://youtube.com/playlist?list=TESTLIST"
                    ) -> Path:
    p = root / "config.toml"
    p.write_text(f"""
[bridge]
voice_enabled = false
[vault]
approved_remote = "{remote}"
branch = "main"
topic = "voice"
[youtube]
enabled = true
playlist_url = "{playlist_url}"
poll_minutes = 30
max_per_tick = {max_per_tick}
languages = ["en", "es", "fr", "pt"]
max_minutes = {max_minutes}
whisper_fallback = {fallback}
handoff_dir = "{handoff}"
topic = "learning"
ytdlp_cmd = ["{sys.executable}", "{FAKE_YTDLP}"]
extra_args = []
""")
    return p


class TestTransform(unittest.TestCase):
    def test_golden_vtt_dedups_and_paragraphs(self):
        text = (FIXTURES / "talk.head.vtt").read_text()
        cues = youtube.parse_vtt(text)
        lines = [l for _, l in cues]
        self.assertEqual(len(lines), len(set(lines)), "rolling repeats survived")
        self.assertEqual(cues[0], (0.0, "What you're looking at right over here"))
        transcript = youtube.to_transcript(cues)
        self.assertEqual(transcript + "\n",
                         (FIXTURES / "talk.head.md").read_text())
        marks = [youtube._seconds(m) for m in
                 re.findall(r"\*\*\[(\d\d:\d\d:\d\d)\]\*\*", transcript)]
        self.assertTrue(transcript.startswith("**[00:00:00]** "))
        for a, b in zip(marks, marks[1:]):
            self.assertGreaterEqual(b - a, youtube.PARAGRAPH_MIN_SECONDS)
        # a paragraph spans at most the max window (first cue → last cue)
        paras = transcript.split("\n\n")
        self.assertGreaterEqual(len(paras), 2)
        self.assertNotIn("<c>", transcript)
        self.assertNotIn("align:start", transcript)

    def test_paragraph_windows_and_rule_escape(self):
        cues = [(0.0, "one."), (30.0, "two"), (50.0, "three."), (60.0, "four"),
                (140.0, "five"), (141.0, "---"), (142.0, "six")]
        cues = [(s, "\\-\\-\\-" if l == "---" else l) for s, l in cues]
        t = youtube.to_transcript(cues)
        self.assertEqual(
            t.split("\n\n"),
            ["**[00:00:00]** one. two three.",       # sentence end after 45 s
             "**[00:01:00]** four",                   # 80 s gap → hard break
             "**[00:02:20]** five \\-\\-\\- six"])
        raw = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n---\n\n"
        self.assertEqual(youtube.parse_vtt(raw), [(1.0, "\\-\\-\\-")])

    def test_manual_vtt_without_repeats_passes_through(self):
        raw = ("WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\n<i>Hello</i> &amp; "
               "welcome\n\n2\n00:00:02.500 --> 00:00:04.000\nSecond line\n"
               "third line\n")
        self.assertEqual(youtube.parse_vtt(raw),
                         [(0.0, "Hello & welcome"), (2.5, "Second line"),
                          (2.5, "third line")])


class TestCaptionChoice(unittest.TestCase):
    def info(self, lang, subs, auto):
        return youtube.VideoInfo("x", "t", "c", 1, "", lang, "u",
                                 subtitles=subs, automatic_captions=auto)

    def test_own_language_manual_beats_auto(self):
        i = self.info("es", {"es": [], "en": []}, {"es-orig": [], "en": []})
        self.assertEqual(youtube.pick_captions(i, ("en",)), ("manual", "es"))

    def test_own_language_auto_prefers_orig_track(self):
        i = self.info("en-US", {}, {"en-orig": [], "en": [], "es": []})
        self.assertEqual(youtube.pick_captions(i, ("en",)), ("auto", "en-orig"))

    def test_preference_list_then_anything(self):
        i = self.info("de", {}, {"fr": [], "ja": []})
        self.assertEqual(youtube.pick_captions(i, ("en", "es", "fr")),
                         ("auto", "fr"))
        i = self.info("", {"ja": []}, {"ko-orig": []})
        self.assertEqual(youtube.pick_captions(i, ("en",)), ("manual", "ja"))
        i = self.info("", {}, {"ko": [], "ja-orig": []})
        self.assertEqual(youtube.pick_captions(i, ("en",)), ("auto", "ja-orig"))

    def test_nothing_at_all(self):
        self.assertIsNone(youtube.pick_captions(self.info("en", {}, {}), ("en",)))


class YoutubeWorld(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.handoff = self.root / "voiceinbox"
        self.handoff.mkdir()
        self.calls = self.root / "calls.log"
        os.environ["OZU_CAPTURE_DATA"] = str(self.root / "data")
        os.environ["FAKE_YTDLP_LOG"] = str(self.calls)
        os.environ.update({
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com",
        })
        self.origin = seed_origin(self.root)
        self.set_entries(["vid-cap"])
        self.cfg = self.load()
        self.cfg.ensure_dirs()

    def tearDown(self):
        self.tmp.cleanup()
        for k in ("OZU_CAPTURE_DATA", "FAKE_YTDLP_LOG", "FAKE_YTDLP_ENTRIES",
                  "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                  "GIT_COMMITTER_EMAIL"):
            os.environ.pop(k, None)

    # -- helpers ---------------------------------------------------------

    def load(self, **kw) -> config.Config:
        return config.load(write_yt_config(
            self.root, remote=str(self.origin), handoff=str(self.handoff),
            **kw))

    def set_entries(self, ids):
        os.environ["FAKE_YTDLP_ENTRIES"] = json.dumps(ids)

    def tick(self, cfg=None, force=True, now=None) -> int:
        return run.youtube_tick(cfg or self.cfg, force=force, now=now)

    def origin_commits(self) -> list[str]:
        r = git(["log", "--format=%s", "main"], self.origin)
        return r.stdout.strip().splitlines() if r.returncode == 0 else []

    def row(self, video_id):
        con = ledger.connect(self.cfg.ledger_path)
        r = ledger.video_get(con, video_id)
        con.close()
        return r

    def calls_matching(self, needle: str) -> int:
        if not self.calls.exists():
            return 0
        return sum(1 for l in self.calls.read_text().splitlines()
                   if needle in l)

    def note_text(self, name: str) -> str:
        """`name` is the slug part; the file is named `<date>-<slug>.md`."""
        d = self.cfg.vault_clone / "topics/learning"
        matches = sorted(d.glob(f"*-{name}"))
        self.assertEqual(len(matches), 1, f"{name}: {[m.name for m in matches]}")
        return matches[0].read_text()

    # -- the tests -------------------------------------------------------

    def test_transcript_note_end_to_end_then_dedup(self):
        self.assertEqual(self.tick(), 1)
        self.assertEqual(self.origin_commits()[0],
                         "youtube: Fake Talk: Captions [pilot] (vid-cap)")
        text = self.note_text("fake-talk-captions-pilot.md")
        for line in ("type: Transcript", "id: ", 'title: "Fake Talk: Captions [pilot]"',
                     "description: YouTube — Fake Channel, 14m 35s, English auto captions, fetched ",
                     "tags: [learning, youtube, transcript]",
                     "source: youtube:vid-cap", "video_id: vid-cap",
                     "url: https://www.youtube.com/watch?v=vid-cap",
                     "channel: Fake Channel", 'duration: "00:14:35"',
                     "published: 2026-07-03", "language: en-US",
                     "captions: auto", "learn: requested", "related: []",
                     "needs_review: false",
                     "generated: { by: ozu-capture/youtube, at: 20",
                     "sources:\n  - id: video\n    resource: https://www.youtube.com/watch?v=vid-cap",
                     "last_modified: 2026-07-03T00:00:00Z"):
            self.assertIn(line, text)
        self.assertNotIn("summary:", text)
        self.assertIn("**[00:00:00]** What you're looking at", text)
        # no registry is written: index.md is derived by the VPS (OKF)
        self.assertEqual((self.cfg.vault_clone / "INDEX.md").read_text(), INDEX_HEADER)
        self.assertFalse((self.cfg.vault_clone / "topics/learning/_topic.md").exists())
        self.assertFalse((self.cfg.vault_clone / "topics/learning/index.md").exists())
        r = self.row("vid-cap")
        self.assertEqual((r["state"], r["outcome"]), ("done", "transcript"))
        # the en-orig track was requested, as an auto track
        self.assertEqual(self.calls_matching('"--write-auto-subs"'), 1)
        self.assertEqual(self.calls_matching('"en-orig"'), 1)

        # AC 2: same playlist again → nothing fetched, nothing committed
        n = len(self.origin_commits())
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(self.origin_commits()), n)
        self.assertEqual(self.calls_matching('"-J", "--skip-download"'), 1)

    def test_ledger_loss_cannot_duplicate(self):
        self.tick()
        n = len(self.origin_commits())
        self.cfg.ledger_path.unlink()                    # AC 3
        self.assertEqual(self.tick(), 1)                 # looked at it…
        self.assertEqual(len(self.origin_commits()), n)  # …wrote nothing
        r = self.row("vid-cap")
        self.assertEqual(r["state"], "done")
        self.assertIn("already in vault", r["last_error"])
        self.assertEqual(self.calls_matching('"--write-auto-subs"'), 1)

    def test_too_long_is_skipped_once_and_retry_rewrites_in_place(self):
        self.set_entries(["vid-long"])
        self.tick()
        text = self.note_text("four-hour-lecture.md")
        self.assertIn("learn: skipped", text)
        self.assertIn("skip_reason: too long (4h 01m; cap 180 min)", text)
        self.assertIn("captions: none", text)
        self.assertEqual(self.calls_matching('"--write-auto-subs"'), 0)
        r = self.row("vid-long")
        self.assertEqual((r["state"], r["outcome"]), ("done", "skipped"))
        n = len(self.origin_commits())
        self.tick()                                       # AC 5: once
        self.assertEqual(len(self.origin_commits()), n)

        # the retry gesture: raise the cap, delete the row → same note,
        # same id, rewritten in place; registries not duplicated
        con = ledger.connect(self.cfg.ledger_path)
        con.execute("DELETE FROM videos WHERE video_id='vid-long'")
        con.commit()
        con.close()
        old_id = re.search(r"^id: (.+)$", text, re.M).group(1)
        cfg2 = self.load(max_minutes="300")
        self.assertEqual(self.tick(cfg2), 1)
        text2 = self.note_text("four-hour-lecture.md")
        self.assertIn(f"id: {old_id}", text2)
        self.assertIn("learn: requested", text2)
        self.assertNotIn("skip_reason", text2)
        self.assertIn("**[00:00:00]**", text2)
        # rewritten in place: still exactly one file for this video
        self.assertEqual(len(list((self.cfg.vault_clone / "topics/learning").glob("*four-hour-lecture*.md"))), 1)
        self.assertEqual(len(self.origin_commits()), n + 1)

    def test_no_captions_hands_off_audio_with_sidecar(self):
        self.set_entries(["vid-nocap"])
        n = len(self.origin_commits())
        self.tick()
        audio = self.handoff / "yt-vid-nocap.m4a"
        sidecar = self.handoff / "yt-vid-nocap.json"
        self.assertTrue(audio.exists() and sidecar.exists())
        meta = json.loads(sidecar.read_text())
        self.assertEqual(meta["video_id"], "vid-nocap")
        self.assertEqual(meta["title"], "Silent Film Restored")
        self.assertEqual(meta["topic"], "learning")
        self.assertEqual(self.row("vid-nocap")["state"], "handed_off")
        self.assertEqual(len(self.origin_commits()), n)  # nothing pushed
        self.tick()                                        # terminal: no re-download
        self.assertEqual(self.calls_matching('"bestaudio[ext=m4a]/bestaudio"'), 1)

    def test_no_captions_with_fallback_off_is_skipped(self):
        self.set_entries(["vid-nocap"])
        cfg = self.load(fallback="false")
        self.tick(cfg)
        text = self.note_text("silent-film-restored.md")
        self.assertIn("learn: skipped", text)
        self.assertIn("skip_reason: no captions", text)
        self.assertFalse(list(self.handoff.iterdir()))

    def test_three_failed_fetches_then_skipped_stub(self):
        self.set_entries(["vid-fail"])
        for i in (1, 2, 3):
            self.tick()
            r = self.row("vid-fail")
            self.assertEqual((r["state"], r["attempts"]), ("failed", i))
            self.assertIn("not a bot", r["last_error"])
        self.assertEqual(self.origin_commits()[0], "scaffold")
        self.tick()                                        # 4th: give up
        r = self.row("vid-fail")
        self.assertEqual((r["state"], r["outcome"]), ("done", "skipped"))
        text = self.note_text("vid-fail.md")
        self.assertIn('skip_reason: "fetch failed 3 times', text)   # quoted: the reason carries a colon
        self.assertEqual(self.calls_matching('"-J", "--skip-download"'), 3)

    def test_listing_failure_is_quiet_and_poll_gate_holds(self):
        cfg = self.load(playlist_url="https://youtube.com/playlist?list=broken")
        self.assertEqual(self.tick(cfg), 0)
        con = ledger.connect(cfg.ledger_path)
        self.assertEqual(ledger.meta_get(con, "listing_failures"), "1")
        con.close()
        self.assertEqual(self.calls_matching('"--flat-playlist"'), 1)
        # not forced and polled just now → no listing at all
        self.assertEqual(self.tick(cfg, force=False), 0)
        self.assertEqual(self.calls_matching('"--flat-playlist"'), 1)
        # 31 minutes later → polls again
        self.tick(cfg, force=False, now=time.time() + 31 * 60)
        self.assertEqual(self.calls_matching('"--flat-playlist"'), 2)

    def test_max_per_tick_and_carry_over(self):
        self.set_entries(["vid-cap", "vid-manual", "vid-long"])
        cfg = self.load(max_per_tick="2")
        self.assertEqual(self.tick(cfg), 2)
        self.assertIsNone(self.row("vid-long"))
        self.assertEqual(self.tick(cfg), 1)
        self.assertEqual(self.row("vid-long")["state"], "done")
        # the Spanish video took its manual track
        self.assertIn("captions: manual", self.note_text("charla-con-subt-tulos.md"))
        self.assertEqual(self.calls_matching('"--write-subs", "--sub-langs", "es"'), 1)

    def test_push_deferred_keeps_commit_and_resumes(self):
        self.tick()                                        # clone exists now
        for d, _, files in os.walk(self.origin):
            os.chmod(d, 0o555)
            for f in files:
                os.chmod(Path(d) / f, 0o444)
        try:
            self.set_entries(["vid-cap", "vid-manual"])
            self.tick()
            self.assertEqual(self.row("vid-manual")["state"], "committed")
        finally:
            for d, _, files in os.walk(self.origin):
                os.chmod(d, 0o755)
                for f in files:
                    os.chmod(Path(d) / f, 0o644)
        self.tick()                                        # resume
        self.assertEqual(self.row("vid-manual")["state"], "done")
        self.assertEqual(
            [c for c in self.origin_commits() if "vid-manual" in c],
            ["youtube: Charla con subtítulos (vid-manual)"])

    def test_remote_guard_refuses_wrong_origin(self):
        self.tick()
        git(["remote", "set-url", "origin", "https://github.com/evil/other.git"],
            self.cfg.vault_clone)
        self.set_entries(["vid-cap", "vid-manual"])
        n = len(self.origin_commits())
        self.assertEqual(self.tick(), 0)
        self.assertIsNone(self.row("vid-manual"))
        self.assertEqual(len(self.origin_commits()), n)


class SidecarInVoiceInbox(unittest.TestCase):
    """AC 4, the Mac half: the voice tick meets a yt-*.m4a + yt-*.json pair
    and files a learning transcript instead of a voice memo."""

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
        self.origin = seed_origin(root)
        from tests.test_config_ledger import write_config
        self.cfg = config.load(write_config(
            root, inbox_override=f'"{self.inbox}"', quiesce="10",
            cmd=f'["{sys.executable}", "{FAKE_WHISPER}"]',
            remote=f'"{self.origin}"'))
        self.cfg.ensure_dirs()

    def tearDown(self):
        self.tmp.cleanup()
        for k in ("OZU_CAPTURE_DATA", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                  "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            os.environ.pop(k, None)

    def drop(self, name: str, data: bytes) -> Path:
        p = self.inbox / name
        p.write_bytes(data)
        st = p.stat()
        con = ledger.connect(self.cfg.ledger_path)
        ledger.observe(con, str(p), st.st_size, st.st_mtime,
                       time.time() - 100, self.cfg.quiesce_seconds)
        con.close()
        return p

    def origin_commits(self):
        return git(["log", "--format=%s", "main"], self.origin).stdout.splitlines()

    def test_sidecar_pair_becomes_learning_transcript(self):
        meta = {"video_id": "vid-nocap", "title": "Work Habits Explained",
                "channel": "Archive", "duration": 300, "upload_date": "20240202",
                "language": "", "url": "https://www.youtube.com/watch?v=vid-nocap",
                "topic": "learning"}
        audio = self.drop("yt-vid-nocap.m4a", b"AUDIO-YT" * 100)
        (self.inbox / "yt-vid-nocap.json").write_text(json.dumps(meta))
        self.assertEqual(run.run_once(self.cfg), 0)

        self.assertEqual(self.origin_commits()[0],
                         "youtube: Work Habits Explained (vid-nocap)")
        text = next((self.cfg.vault_clone / "topics/learning").glob("*-work-habits-explained.md")
                ).read_text()
        self.assertIn("captions: whisper", text)
        self.assertIn("learn: requested", text)
        self.assertIn("language: es", text)          # WHISPER's detection
        self.assertIn("**[00:00:00]** Primer párrafo", text)
        self.assertIn("transcribed on-device", text)
        self.assertFalse((self.cfg.vault_clone / "topics/voice").exists())
        # tidy: audio archived, sidecar gone, and .json never got a marker
        self.assertTrue((self.inbox / "processed/yt-vid-nocap.m4a").exists())
        self.assertFalse((self.inbox / "yt-vid-nocap.json").exists())
        self.assertFalse(list(self.inbox.glob("*.failed.txt")))
        # a second hand-off of the same video (e.g. re-pin) is a no-op
        n = len(self.origin_commits())
        self.drop("yt-vid-nocap.m4a", b"AUDIO-YT-2" * 100)
        (self.inbox / "yt-vid-nocap.json").write_text(json.dumps(meta))
        run.run_once(self.cfg)
        self.assertEqual(len(self.origin_commits()), n)
        self.assertFalse((self.inbox / "yt-vid-nocap.json").exists())

    def test_work_prefix_does_not_wall_a_handoff_but_plain_json_is_ignored(self):
        (self.inbox / "notes.json").write_text("{}")
        audio = self.drop("yt-vid-x.m4a", b"AUDIO-X" * 50)
        # no sidecar → an ordinary voice memo (nothing yt- about the content)
        run.run_once(self.cfg)
        self.assertTrue(list((self.cfg.vault_clone / "topics/voice").glob("*-yt-vid-x.md")))
        self.assertFalse(list(self.inbox.glob("*.failed.txt")))


class TestConfigSections(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["OZU_CAPTURE_DATA"] = str(self.root / "data")

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("OZU_CAPTURE_DATA", None)

    def test_repo_config_has_youtube_off(self):
        cfg = config.load(config.REPO_ROOT / "config.toml")
        self.assertFalse(cfg.youtube_enabled)
        self.assertTrue(cfg.voice_enabled)
        self.assertEqual(cfg.youtube_topic, "learning")
        self.assertIn("YOUR_PLAYLIST_ID", cfg.playlist_url)

    def test_container_config_needs_no_nas_or_whisper(self):
        p = write_yt_config(self.root, remote="git@github.com:x/y.git",
                            handoff="/voiceinbox")
        cfg = config.load(p)
        self.assertTrue(cfg.youtube_enabled and not cfg.voice_enabled)
        self.assertEqual(cfg.whisper_cmd, ())

    def test_enabled_without_playlist_or_handoff_refused(self):
        p = write_yt_config(self.root, remote="r", handoff="/h", playlist_url="")
        with self.assertRaises(config.ConfigError):
            config.load(p)
        p = write_yt_config(self.root, remote="r", handoff="")
        with self.assertRaises(config.ConfigError):
            config.load(p)

    def test_nas_template_is_valid(self):
        cfg = config.load(config.REPO_ROOT / "deploy/nas/config.nas.toml")
        self.assertTrue(cfg.youtube_enabled and not cfg.voice_enabled)
        self.assertEqual(cfg.approved_remote,
                         "git@github.com:you/vault.git")
        self.assertEqual(cfg.handoff_dir, "/voiceinbox")
        self.assertTrue(cfg.ytdlp_upgrade)


if __name__ == "__main__":
    unittest.main()
