# Fixture corpus — the spec for the render preview harness

Every fixture here is **synthetic**: shaped like what a real vault holds (the
lengths, glyphs and structures that broke the renderer in practice), but
written for the test suite. No real notes, names or pages.

## Files

| file | what it is | provenance |
|---|---|---|
| `brief-900w.md` | an 899-word research brief | written for the suite: headings, numbered points, `€` figures, six long URLs on `example.org` |
| `ink-2000px.png` | a 1654×2339 "handwriting" page | drawn by `tools/fixture_page.py` |
| `vault-rows.json` | 28 note rows, 10 action rows | a fictional vault: an old farmhouse, a brewing app (`kettle`), a trip planner (`atlas`), a garden |
| `corpus.py` | the cases + payloads for every renderer | — |

## The cases

| case | kind | guards |
|---|---|---|
| `brief-900w` | research | page breaks, heading orphans, and `€`/circled-digit loss |
| `glyphs` | research | exact round-trip of `€ ② ③ ④ — → ✓ ô ä`, plus the `U+FB01`/`U+FB03` ligature trap |
| `ink-2000px` | mail | full-resolution ink crop placement — the "Needs review" path |
| `action-120` | digest | 120-char action: wrap must not orphan `☐` from its text (D-04) |
| `url-300` | research | 300-char URL: the writer must not hard-split mid-token |
| `week-empty` | digest | the zero state — every section absent |
| `week-40` | digest | pagination under load, and the visible cap on new notes |
| `build-propose` | build | the Build id on every page; decisions never printed on a `☐` |
| `build-result` | build | the `☐ Review` line survives extraction exactly |
| `edition` | edition | per-section headers, a fresh page per section, the outline, the delete list |

## Two gates, not one

- **chars** — nothing dropped or substituted. This is the D-01 data-loss gate.
- **intact** — long tokens still contiguous. A hard-split URL passes `chars`
  and fails `intact`: the characters survive, the URL does not.

`tools/render_sample.py` builds every case into `build/sample/` and checks both.
