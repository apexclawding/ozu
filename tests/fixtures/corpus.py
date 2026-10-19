"""The fixture corpus for the render preview harness.

The corpus IS the spec: each case names a way the renderer is known to fail or
is untested. Every case is synthetic, but shaped like what a real vault holds
— the lengths, glyphs and structures that broke the renderer in practice.
Two consumers:

  * `tools/render_sample.py` — build every document type to local PDFs.
  * the round-trip test — assert that what goes in comes back out of the text
    layer byte-for-byte, because `understand._build_annotation_prompt()` feeds
    that text layer to the vision model as the printed-side reference (D-01).

Provenance is recorded per case: what it is shaped like, and where a stress
size goes beyond the ordinary one, the ordinary size is kept alongside.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
_ROWS = json.loads((HERE / "vault-rows.json").read_text())

BRIEF_900W = (HERE / "brief-900w.md").read_text()
INK_2000PX = HERE / "ink-2000px.png"

# --- the glyph set that must survive ----------------------------------------
# € ② ③ ④ ô ä are what a real brief carries; → and ✓ are in `_SAFE` today (mapped to
# "->" and "[x]"), so they never reach the font — but a Story renderer will
# emit them literally, and ✓ in particular is what the annotation prompt looks
# for. They belong in the corpus from the start.
HOSTILE_GLYPHS = "€ ② ③ ④ — → ✓ ô ä"

GLYPH_TORTURE = (
    "The pellet boiler quote exceeds €18,000, so the grant decides it.\n"
    "Point ② also cuts against a winter install; point ③ puts the payback at\n"
    "7-9 years out, and point ④'s \"contrôle\" visit is the only evidence either way.\n"
    "Measure → insulate → size. Confirm the official fee ✓ and the meter\n"
    "rating — the ô in contrôle and the ä in Zähler must both survive."
)

# --- strings the vault could not supply at the size the spec asks for --------
# The longest fixture action is 61 chars; the longest fixture URL is 208. Both
# are kept so a regression at ordinary sizes is visible beside the stress sizes.
ACTION_REAL = _ROWS["actions"][0]["text"]                      # 61 chars
ACTION_120 = ("Call the roofer about the north slope before filing the grant, "
              "then confirm the official fee with the installer by May ✓")
assert len(ACTION_120) == 120, len(ACTION_120)

URL_REAL = ("https://example.org/renovation/help-centre/grants/improving-the-"
            "energy-performance-of-an-old-house/can-i-claim-the-insulation-"
            "grant-and-the-heating-grant-in-the-same-year-if-the-house-was-"
            "built-before-1950/en")                             # 208 chars
URL_300 = (URL_REAL + "?utm_source=ozu-scribe&utm_medium=research-brief&ref="
                      "heat-pump-or-pellets-for-the-farmhouse-running-costs")[:300]
assert len(URL_300) == 300, len(URL_300)

NOTES = _ROWS["notes"]
ACTIONS = _ROWS["actions"]


# --- the learning sheet ------------------------------------------------------
# A synthetic brief about a fictional video. It carries the hostile glyphs, a
# wikilink, timestamps and inline emphasis so both gates have something to
# catch.
LEARN_BRIEF = """## Takeaways
- A lab notebook **compounds**: write the question before the result, and the page argues with you later [02:10].
- Retrieval beats memory when the notes are yours: forty dated pages answered what a year of memory could not.
- The value is in the links between pages, not the pages — the same claim the vault makes about edges.

## Techniques, tools, tips
- Date every page and never tear one out [06:45].
- Number claims ② ③ ④ so a later page can cite a point, not a paragraph — the €180 example at [11:30] does exactly this.

## Where it touches your work
- **kettle** — [[20260818-kettle-new-design-two-timers]]: a brew log is a lab notebook with a timer; the video argues for one page per batch ✓.

## Try this week
Write one brew-day page in the notebook format — fifteen minutes, in the vault.

## Question for the page
What would you want a stranger to understand from your log without asking you — and what never?"""

LEARN_SHEET = {
    "date": "2026-09-02", "weekday": "Wednesday 2 September 2026",
    "new": [{
        "id": "20260901-how-to-keep-a-lab-notebook-learning",
        "title": "How to Keep a Lab Notebook",
        "channel": "Example Workshop", "duration": "00:14:35",
        "pinned": "2026-09-01", "brief": LEARN_BRIEF,
    }],
    "due": [{
        "id": "20260826-sourdough-from-first-principles-learning",
        "title": "Sourdough from First Principles", "stage": 1, "nth": "2nd",
        "learned": "2026-08-31",
        "cue": "Fermentation is a clock you set with temperature — the ä in Zähler counts the hours.",
        "question": "Which of your recipes would change if you logged the temperature?",
    }],
    "skipped": [{
        "id": "20260901-a-four-hour-lecture",
        "title": "A Four-Hour Lecture",
        "reason": "too long (4h 01m) — pin a shorter cut or raise the cap",
    }],
}


def _notes(n: int) -> list[dict]:
    """n note rows. The fixture holds 28; beyond that, rows cycle — the
    point of the 40-note case is pagination and heading orphans, not novelty."""
    return [dict(NOTES[i % len(NOTES)], id=f"{NOTES[i % len(NOTES)]['id']}-{i}")
            for i in range(n)]


def _digest(**over) -> dict:
    """A digest data dict with every key `build_pdf` reads, empty by default."""
    base = {
        "date": "2026-08-30", "new": [], "review": [], "built": [],
        "r_built": [], "r_queued": [], "r_failed": [], "queued": [],
        "failed": [], "actions": [], "links": [], "gem": None, "repos": {},
    }
    base.update(over)
    return base


# --- the eight cases ---------------------------------------------------------
# Each: what it renders, and the failure it exists to catch.

CASES = {
    "brief-900w": {
        "kind": "research",
        "guards": "a full-length brief: page breaks, heading orphans, and the "
                  "€/circled-digit loss measured in the delivered PDF",
        "source": "synthetic: a 899-word brief shaped like a real one — "
                  "headings, numbered points, € figures and six long URLs",
        "payload": dict(title="Research: Heat pump or pellets for the farmhouse",
                        source_id="20260823-heat-pump-or-pellets",
                        topic="house", report=BRIEF_900W),
    },
    "glyphs": {
        "kind": "research",
        "guards": f"exact round-trip of {HOSTILE_GLYPHS} — and the ligature "
                  "trap: 'Confirm the official fee' must not extract as "
                  "'Conﬁrm the oﬃcial fee' (U+FB01/U+FB03)",
        "source": "phrases in the style of the brief; → and ✓ added because "
                  "_SAFE hides them from the font today",
        "payload": dict(title="Glyph round-trip", source_id="fixture-glyphs",
                        topic="ideas", report=GLYPH_TORTURE),
    },
    "ink-2000px": {
        "kind": "mail",
        "guards": "a full-resolution handwriting crop placed by the renderer — "
                  "the 'Needs review' path, which must survive the migration "
                  "to Story/fitz.Archive",
        "source": "synthetic handwriting page (1654×2339), drawn by a script",
        "payload": None,   # built by sample() below, needs the image path
    },
    "action-120": {
        "kind": "digest",
        "guards": "an action longer than one line: the wrap must not orphan the "
                  "☐ from its text, and ☐ must stay in the text layer (D-04)",
        "source": f"a 61-char action extended; fixture maximum is "
                  f"{len(ACTION_REAL)} chars",
        "payload": None,
    },
    "url-300": {
        "kind": "research",
        "guards": "a URL wider than the page — `_Writer` hard-splits mid-token "
                  "with no hyphen, so the text layer silently gains a break "
                  "inside the URL",
        "source": f"a {len(URL_REAL)}-char URL plus query string; "
                  "fixture maximum is that 208-char URL",
        "payload": dict(title="Sources", source_id="fixture-url", topic="ideas",
                        report=f"Reference:\n{URL_300}\n\nAlso:\n{URL_REAL}"),
    },
    "week-empty": {
        "kind": "digest",
        "guards": "the zero state — every section absent. Today this prints "
                  "'Quiet week · the Scribe misses you.' and nothing else",
        "source": "constructed: no rows",
        "payload": _digest(),
    },
    "build-propose": {
        "kind": "build-propose",
        "guards": "the Build proposal: the id must reach the text layer of "
                  "every page (the running header is the matching key), and "
                  "the decision box prints @approved/@input/@drop as plain "
                  "lines — never on a ☐, which stays reserved for actions",
        "source": "constructed: a two-page proposal with the hostile é/№ mix "
                  "briefs actually carry",
        "payload": dict(
            id="B-260906-7f3a", title="Weekly Sunday “week ahead” note",
            topic="kettle", rev=2, date="2026-09-06",
            history="### r1 proposed — 2026-09-05\n\n### input — 2026-09-06\n\n"
                    "> keep it under 10 lines",
            proposal="# What I understood\nA weekly résumé note, № 1 priority."
                     "\n\n# Requirement\nEvery Sunday a “week ahead” note.\n\n"
                     "# Plan\n" + "\n".join(
                         f"{i}. Step {i} — " + "détail " * 18
                         for i in range(1, 14)) +
                     "\n\n# Risks\n- Timezone of “Sunday”.\n\n"
                     "# What I will not do\n- No UI.\n\n# Open questions\nNone."),
    },
    "build-result": {
        "kind": "build-result",
        "guards": "the Build result: the ☐ Review line must survive "
                  "extraction exactly (it is a closeable action), beside the "
                  "PR URL and the decision trail",
        "source": "constructed",
        "payload": dict(
            id="B-260906-7f3a", title="Weekly Sunday “week ahead” note",
            topic="kettle", model="sonnet", date="2026-09-07",
            review_url="https://github.com/example/kettle/pull/12",
            report="Built the generator as approved; tests pass (unittest, "
                   "4 green). Could not verify the Sunday cron on this box.",
            history="### approved — 2026-09-07 · sonnet",
            actions=["Review PR #12 — Weekly Sunday “week ahead” note"]),
    },
    "learn-evening": {
        "kind": "learn",
        "guards": "the learning sheet: a full brief under ruled writing "
                  "lines, a due-again item, a skipped transcript. The rules "
                  "must add only whitespace to the text layer, and no ☐ may "
                  "appear anywhere on the page",
        "source": "synthetic: a fictional video, with a brief written to "
                  "carry the hostile glyphs",
        "payload": LEARN_SHEET,
    },
    "week-40": {
        "kind": "digest",
        "guards": "pagination under load: 40 notes, 10 actions, review images. "
                  "`build_pdf` slices new[:12] — the other 28 are dropped "
                  "silently, which the harness should make visible",
        "source": "28 fixture notes, cycled to 40",
        "payload": _digest(
            new=_notes(40),
            actions=[{"text": a["text"], "title": a["title"]} for a in ACTIONS],
            built=NOTES[:2], r_built=NOTES[2:4],
            queued=[dict(NOTES[4], topic="kettle")], failed=NOTES[5:6],
            gem=dict(NOTES[6], created="2026-07-01"),
            repos={"kettle": "git@github.com:example/kettle.git"},
        ),
    },
}


# The edition: every kind of section in one document (D-07). The guards are
# the ones a single-Story document could not fail — a Build result's pages
# stamped with its own id while the mail's are not, a section never sharing
# a page with the one before, no blank sheet between them, and the outline.
CASES["edition"] = {
    "kind": "edition",
    "guards": "per-section running headers, fresh page per section with no "
              "blank page, a PDF outline, and the delete list — plus every "
              "section's own gates, since the bodies are shared",
    "source": "assembled from the mail and build-result cases",
    "payload": {
        "kind": "morning", "date": "2026-09-11",
        "weekday": "Friday 11 September 2026", "stamp": "2026-09-11 07:00",
        "mail": {
            "stamp": "2026-09-11 07:00", "work": [], "research": [
                {"title": "Research: Heat pump grants for old houses"}],
            "receipts": [{"closed": ACTION_REAL, "line": "✓ " + ACTION_REAL,
                          "candidates": []},
                         {"closed": None, "line": "Fixed the gate",
                          "candidates": [ACTION_120]}],
            "builds": ["B-260905-e5f1 (Weekly note): dropped — nothing was executed."],
            "plain": [{"topic": "ideas", "title": "Inbox page 6",
                       "summary": "Low-confidence transcription — check the ink.",
                       "actions": [ACTION_120], "image": None,
                       "body": BRIEF_900W[:300]}],
        },
        "results": [CASES["build-result"]["payload"]],
        "learn": None, "digest": None,
        "dead": {"recent": [
            {"name": "Ozu Build r1 - Weekly Sunday “week ahead” note (B-260906-7f3a)",
             "kind": "build", "reason": "superseded by r2", "dead_on": None},
            {"name": "Ozu Mail 2026-09-07 1634", "kind": "mail",
             "reason": "an old Ozu Mail — everything on it is in the vault",
             "dead_on": None}], "older": 12, "new_names": []},
    },
}


def sample() -> dict:
    """Every case with its payload resolved, ready to hand to the renderers."""
    out = {}
    for name, case in CASES.items():
        c = dict(case)
        if name == "ink-2000px":
            c["payload"] = {
                "stamp": "2026-08-30 12:36", "work": [], "research": [],
                "receipts": [{"closed": ACTION_REAL, "line": "✓ " + ACTION_REAL,
                              "candidates": []}],
                "plain": [{"topic": "ideas", "title": "Inbox page 6",
                           "summary": "Low-confidence transcription — check the ink.",
                           "actions": [ACTION_120],
                           "image": str(INK_2000PX),
                           "body": BRIEF_900W[:500]}],
            }
        elif name == "action-120":
            c["payload"] = _digest(
                new=_notes(3),
                actions=[{"text": ACTION_120, "title": NOTES[0]["title"]},
                         {"text": ACTION_REAL, "title": NOTES[1]["title"]}])
        out[name] = c
    return out


# Everything that must survive a round trip, for the D-01 gate.
def roundtrip_strings() -> list[str]:
    return [HOSTILE_GLYPHS, GLYPH_TORTURE, ACTION_REAL, ACTION_120,
            URL_REAL, URL_300, BRIEF_900W, LEARN_BRIEF, "☐", "✓"]


if __name__ == "__main__":
    s = sample()
    print(f"{'case':14} {'kind':9} {'payload':>9}  guards")
    print("-" * 100)
    for name, c in s.items():
        size = len(json.dumps(c["payload"], default=str))
        print(f"{name:14} {c['kind']:9} {size:8}B  {c['guards'][:60]}")
    print("-" * 100)
    print(f"{len(s)} cases · brief {len(BRIEF_900W.split())} words · "
          f"ink {INK_2000PX.stat().st_size // 1024} KB · "
          f"action {len(ACTION_120)} chars · URL {len(URL_300)} chars · "
          f"empty + {len(s['week-40']['payload']['new'])}-note weeks")
