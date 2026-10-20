"""The learning-folder lint (lint.py).

Written as a unittest.TestCase so it runs under stdlib `unittest` as well
as pytest: it needs no fixture from test_pipeline, only a vault on disk.

Pinned: a healthy transcript + brief pair is silent; each broken shape the
loop can leave behind is reported once; a project note elsewhere in the
vault counts as a citation and a wiki concept page does not; a note the
loop did not write is reported; `[[…]]` inside code is not a link; and the
pass writes nothing.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ozu import lint, okf  # noqa: E402

BRIEF = """From [[{src}]] · Example Channel

## Takeaways
- An LLM wiki compounds.

## Techniques, tools, tips
- Lint the wiki weekly [12:10].

## Where it touches your work
- [[20260830-project-roadmap]]: the raw/wiki split.

## Try this week
Run one lint pass.

## Question for the page
What would you let the model rewrite without reading the diff?
"""


class LintTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)
        self.folder = self.vault / "topics" / "learning"
        self.folder.mkdir(parents=True)
        self.write(self.vault / "20260830-project-roadmap.md",
                   type="Note", id="20260830-project-roadmap",
                   source="scribe:Inbox/p1")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, path, body="", **fm):
        fm.setdefault("description", "d")
        path.write_text(okf.dump_frontmatter(fm) + "\n" + body)

    def transcript(self, nid, learn="done", video="v1"):
        self.write(self.folder / f"{nid}.md", type="Transcript", id=nid,
                   source=f"youtube:{video}", video_id=video, learn=learn)

    def brief(self, nid, src, body=None):
        self.write(self.folder / f"{nid}.md", body or BRIEF.format(src=src),
                   type="Learning", id=nid, source=f"learn:{src}", related=[src])

    def findings(self):
        return lint.lint(self.vault)

    def test_a_healthy_pair_is_silent(self):
        self.transcript("20260901-wiki")
        self.brief("20260902-wiki-learning", "20260901-wiki")
        self.assertEqual(self.findings(), [])

    def test_failed_and_lost_briefs_are_reported(self):
        self.transcript("20260901-failed", learn="error", video="a")
        self.transcript("20260901-lost", learn="done", video="b")
        self.transcript("20260901-queued", learn="requested", video="c")
        out = self.findings()
        self.assertEqual(len(out), 2, out)
        self.assertIn("20260901-failed.md: brief run failed", out[0])
        self.assertIn("20260901-lost.md: learn: done, but no Learning note", out[1])

    def test_a_brief_whose_transcript_is_gone(self):
        self.brief("20260902-wiki-learning", "20260901-wiki")
        out = self.findings()
        self.assertTrue(any("its source 20260901-wiki is not in the vault" in f
                            for f in out), out)

    def test_same_video_pinned_twice(self):
        self.transcript("20260901-wiki", learn="requested")
        self.transcript("20260905-wiki", learn="requested")
        self.assertEqual(len(self.findings()), 1)
        self.assertIn("same video (v1)", self.findings()[0])

    def test_missing_sections_are_named(self):
        self.transcript("20260901-wiki")
        body = BRIEF.format(src="20260901-wiki").replace(
            "## Question for the page", "## Afterthought")
        self.brief("20260902-wiki-learning", "20260901-wiki", body)
        self.assertEqual(len(self.findings()), 1)
        self.assertIn("missing 'question'", self.findings()[0])

    def test_entertainment_brief_is_takeaways_alone(self):
        self.transcript("20260901-cats")
        self.brief("20260902-cats-learning", "20260901-cats",
                   "From [[20260901-cats]]\n\n## Takeaways\nEntertainment; nothing to learn.\n")
        self.assertEqual(self.findings(), [])

    def test_empty_brief(self):
        self.transcript("20260901-wiki")
        self.brief("20260902-wiki-learning", "20260901-wiki",
                   "From [[20260901-wiki]]\n\n(no brief returned)\n")
        self.assertEqual(self.findings(),
                         ["topics/learning/20260902-wiki-learning.md: the run returned no brief"])

    def test_a_brief_that_touches_no_project(self):
        self.transcript("20260901-wiki")
        body = BRIEF.format(src="20260901-wiki").replace(
            "[[20260830-project-roadmap]]", "nothing in the vault relates")
        self.brief("20260902-wiki-learning", "20260901-wiki", body)
        self.assertEqual(len(self.findings()), 1)
        self.assertIn("touches no project", self.findings()[0])

    def test_a_brief_citing_only_wiki_pages_touches_no_project(self):
        self.transcript("20260901-wiki")
        (self.vault / "topics" / "wiki").mkdir()
        self.write(self.vault / "topics" / "wiki" / "wiki-llm-wiki.md",
                   type="Concept", id="wiki-llm-wiki", source="wiki:compiled")
        body = BRIEF.format(src="20260901-wiki").replace(
            "[[20260830-project-roadmap]]", "[[wiki-llm-wiki]]")
        self.brief("20260902-wiki-learning", "20260901-wiki", body)
        self.assertEqual(len(self.findings()), 1)
        self.assertIn("touches no project", self.findings()[0])

    def test_a_reply_that_is_not_a_brief(self):
        self.transcript("20260901-wiki")
        self.brief("20260902-wiki-learning", "20260901-wiki",
                   "From [[20260901-wiki]]\n\nBrief saved at `topics/learning/x-brief.md`.\n")
        self.assertEqual(self.findings(), [
            "topics/learning/20260902-wiki-learning.md: no brief sections at all "
            "— the run's reply was not a brief"])

    def test_a_note_the_loop_did_not_write(self):
        self.transcript("20260901-wiki")
        self.brief("20260902-wiki-learning", "20260901-wiki")
        self.write(self.folder / "20260901-wiki-brief.md", BRIEF.format(src="20260901-wiki"),
                   type="Learning", id="20260901-wiki-brief", source="brief:20260901-wiki")
        self.assertEqual(self.findings(), [
            "topics/learning/20260901-wiki-brief.md: not written by the learning loop "
            "(source: brief:20260901-wiki)"])

    def test_a_link_inside_code_is_not_a_link(self):
        self.transcript("20260901-wiki")
        body = BRIEF.format(src="20260901-wiki") + "\nMake every `[[note-id]]` resolve.\n"
        self.brief("20260902-wiki-learning", "20260901-wiki", body)
        self.assertEqual(self.findings(), [])

    def test_dead_link(self):
        self.transcript("20260901-wiki")
        body = BRIEF.format(src="20260901-wiki") + "\nSee [[20250101-gone]].\n"
        self.brief("20260902-wiki-learning", "20260901-wiki", body)
        self.assertEqual(self.findings(),
                         ["topics/learning/20260902-wiki-learning.md: link to unknown note 20250101-gone"])

    def test_notes_outside_the_folder_are_not_linted(self):
        self.write(self.vault / "topics" / "ideas.md", "[[20250101-gone]]",
                   type="Note", id="ideas")
        self.assertEqual(self.findings(), [])

    def test_the_pass_writes_nothing(self):
        self.transcript("20260901-failed", learn="error")
        before = {p: p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}
        self.findings()
        after = {p: p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
