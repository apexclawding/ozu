# Glyph loss in delivered documents — 2026-08-31

The question was how much damage `digest_pdf._SAFE` has actually done: it is a
hand-maintained allowlist of ~11 transliterations, and base-14 Helvetica
substitutes `·` for every glyph it lacks. So a `·` in a delivered text layer is
either renderer furniture or a character that never arrived.

**Answer: 14 characters lost across 16 delivered documents, all of them in
research briefs.** Mail has never lost a character. The losses are not cosmetic
— in one brief they collapsed `②`, `③` and `④` into the same anonymous `·`,
which dismantled an argument that referred back to its own numbered points.

Run with a one-off survey script against every document delivered so far
(research briefs, digests and mail). PyMuPDF 1.28.2. Read-only; walled `work-vault`
excluded from the comparison corpus.

## What was lost

Every `·` is accounted for: 28 total = 14 renderer furniture + 14 real losses.
All 14 losses were recovered to a concrete source character by aligning the
surrounding context against the vault original.

| dir | document | `_SAFE` | pg | `·` | furn | LOST | chars lost |
|---|---|---|---|---|---|---|---|
| research | brief A | post | 3 | 7 | 2 | **5** | `€③②④€` |
| research | brief B (A's follow-up) | post | 3 | 7 | 1 | **6** | `≤€€€€€` |
| research | brief C | post | 3 | 1 | 1 | 0 | |
| research | brief D | post | 3 | 2 | 1 | **1** | `€` |
| digests | Ozu Digest 2026-08-22 | **pre** | 1 | 4 | 2 | **2** | `——` |
| digests | Ozu Digest 2026-08-23 | post | 1 | 2 | 2 | 0 | |
| digests | Ozu Digest 2026-08-30 | post | 3 | 1 | 1 | 0 | |
| mail | ×9, 2026-08-24 → 08-30 | post | 9 | 5 | 5 | 0 | |
| | **16 documents** | | **26** | **28** | **14** | **14** | |

By character: `€`×8, `—`×2, `②` `③` `④` `≤` ×1 each.

## Three things the numbers say

**The damage is concentrated in research briefs — 12 of 14.** Briefs are the
only documents that typeset free model prose; digests and mail render vault
fields and template strings, which are already ASCII in practice. That is why
mail has never lost anything, and it is also why the fix is worth shipping
before any redesign: the briefs are the documents the owner actually reads at
length.

**The 08-22 digest is a different era.** `_SAFE` shipped 2026-08-23 in
`bc6fe7a`. Before that, every em dash, curly quote, bullet and arrow was lost
too. The two `—` in that digest are pre-allowlist, not a new leak. Everything
since is the allowlist-gap failure.

**`≤` (U+2264) is the finding that settles the design argument.** It is not a
dash, not currency, not a circled digit — it is a comparison operator, and it
appeared in a threshold of the form `budget ≤ €2,500`. It reached the Scribe as `budget · ·2,500`,
which reads as noise where the original stated a threshold. Nobody would have
put `≤` on a hand-maintained allowlist in advance. The set of characters a
model may reach for is not enumerable, so an allowlist is the wrong shape of
solution regardless of how many entries it has.

## The control: is anything lost *without* leaving a dot?

No. Counting dots could undercount if Helvetica dropped a glyph silently, so
the full non-ASCII character set of each brief was diffed against its delivered
text layer. It found exactly the same losses and no others.

What survives is precisely Latin-1 — `é è à ä ô É ×` all round-trip — which is
consistent with base-14 Helvetica's coverage. So the defect surface is exact:

> **anything outside Latin-1 that is not one of `_SAFE`'s 11 entries.**

## A second defect, found while building the fixture corpus

`_Writer.text()` hard-splits any token wider than the page, with no hyphen and
no marker. A 208-char URL from the vault arrives in the text layer as three
separate lines:

```
https://example.org/renovation/help-centre/grants/improving-the-energy-performance-of-an-old
-house/can-i-claim-the-insulation-grant-and-the-heating-grant-in-the-same-year-if-the-house-
was-built-before-1950/en
```

No characters are lost — this one is not data loss — but the URL is no longer a
URL. It matters for D-01 for a specific reason: the round-trip gate must be
**two** assertions, not one. A whitespace-insensitive comparison passes this
case, because whitespace-insensitivity is exactly what hides it:

- `chars` — nothing dropped or substituted (the data-loss gate)
- `intact` — long tokens still contiguous (the usability gate)

The first version of `tools/render_baseline.py` had only `chars`, and reported
the URL case green. A green test that asserts on the wrong thing.

## Fixed — 2026-08-31, Stage 2

The renderer now lays out with `fitz.Story` in Charis SIL (`src/ozu/layout.py`).
Re-rendering the three briefs above **from their own vault originals** loses
**0 characters** and prints **0 substitution dots**, against the 12 lost
before. `tools/render_sample.py` reports 7/7 fixture cases clean.

The `≤` case is the one to keep in mind, because it is the argument against
ever going back: it was never on the allowlist and could not have been, and it
now survives for the same reason every future character will — the font has it,
rather than the code having anticipated it.

## A third defect, found while building a daily brief — 2026-09-06 (D-05)

The header band was grey on **every** page, not just the one it belonged to.
MuPDF's `Story` repaints the last `background-color` it placed as a one-line
stub at the top of each subsequent page, so a document that turned a page
carried a smear of `#efefef` above its first line — behind live text, which is
why it read as a rendering glitch rather than as a stray rectangle.

Measured on PyMuPDF 1.28.2 / MuPDF 1.28.2, by counting filled paths per page:

```
html                         p1                  p2                  p3
band div + 80 paragraphs     (52,50,332,85)      (52,32,332,48)      (52,32,332,48)
band in the MIDDLE           —                   (52,344,332,360)    (52,32,332,48)
no background-color at all   —                   —                   —
```

Three things the table says:

1. It is the background, not the band: remove `background-color` and every
   fill goes, on every page.
2. It is not "the first element repeating" — put the band in the middle and
   the stub starts on the page *after* it, wherever that is.
3. The stub is one line high and sits at y≈32, partly *above* the content
   rect (`MARGIN_T` is 40), so it is not the element re-laying out. It is the
   fill being re-emitted at a reset position.

Two workarounds that do not work, tried first: putting the fill on the inner
paragraphs instead of the wrapper makes it *worse* (mupdf then paints a box
behind every inline run — 28 filled paths on page 1 of a three-line band, and
the stub survives), and following the band with an explicitly white element
does not mask it.

**Fixed** by taking the fill out of the stylesheet entirely and drawing it:
`head()` stamps the band with `id="ozuband"`, `render()` collects its box from
`Story.element_positions` between `place()` and `draw()`, and paints a
rectangle there with `draw_rect(..., overlay=False)` so it lands under the
text. `BAND_PAD` in `layout.py` must equal the `.band` padding in the CSS —
the reported rect is the content box, and the padding is what turns it back
into the painted one.

Two gates in `tests/test_textlayer.py`: the band is painted exactly once and
never on a continuation page, and the stylesheet contains no
`background-color` at all — because any new one would bring the stub back.
Verified across all eleven `tools/render_sample.py` cases: one band-coloured
fill on page 1 of each, zero on every continuation page.

## The cover — 2026-09-08 (D-06)

Not a defect: a document that could not be found. On the Scribe's home screen
a PDF is a page-1 thumbnail about 126px wide (an A5 page at ~30%) under a
truncated filename. Page 1 of every Ozu document was body text — the largest
thing on it was the 16px title, which lands at **4.8px** in the grid, and the
grey header band renders as a featureless smudge. `Ozu Mail 2026-09-05 0730`
and the morning brief of 2026-09-05 were the same picture, so telling
yesterday's mail from this morning's brief meant opening both.

Every document now opens with a cover, sized for that thumbnail first:

| element | on the page | in the library grid |
|---|---|---|
| day numeral | 96px | ~29px |
| month | 27px | ~8px |
| wordmark (`MAIL`, `WEEKLY DIGEST`, …) | 30px | ~9px |
| type mark (drawn) | 92pt square | ~28px |

The dark block bleeds to the top and both edges of the sheet, because a
thumbnail is scaled and cropped by the library and an element that touches the
trim is the one that still reads afterwards. The bottom half of the cover is
left white: it is the emptiest page in the document, so it is also the easiest
place to write a Build decision.

Four things it inherits from the defects above:

1. **The fill is painted, not styled (D-05).** Same mechanism as the header
   band — `id="ozucover"`, `Story.element_positions` between `place()` and
   `draw()`, `draw_rect(..., overlay=False)`. It matters more here: the type
   inside the block is white, so a cover that lost its fill would extract
   perfectly and arrive as a blank sheet. Both halves are asserted — the fill
   exists, and every white span sits inside it.
2. **No unfilled rectangle among the type marks (D-04, invariant 5).** The
   marks are drawings, so they add nothing to the text layer; an outlined
   square among them would be a checkbox to a reader that has only the image.
   The envelope is a filled body with a paper-coloured flap, and the Build
   result mark is a merge arrow rather than the loop it started as. The test
   walks every mark's paths and fails on any stroke-only `re`.
3. **Nothing is only on the cover (D-01).** It is a label: the date, the kind
   of document, and one line of what is inside — all of it printed again in
   the body. A reader who skips the page loses nothing, which is what makes it
   safe to truncate the two content lines at 120 characters.
4. **The running Build stamp is white on the cover.** `page_header` prints the
   Build id on every page in furniture grey; on the dark block that extracts
   perfectly and shows nothing, and the annotation prompt tells the model the
   id is *printed* on every page. It is drawn in paper colour on page 1.

**Why the cover is a second `Story` and not hand-placed type.** A cover is a
handful of strings at fixed positions, which argues for `page.insert_text` and
exact coordinates — and `insert_text` means base-14 Helvetica, the font this
whole document is about. There is no way around that on this box: MuPDF's
builtin face list under PyMuPDF 1.28.2 is the base-14 plus `cjk` (Droid Sans
Fallback), and `charis`, `charissil`, `sans` and `serif` all raise `cannot find
builtin font with name`. Charis SIL reaches a page only through a Story's CSS.
So the cover is a second Story, placed on page 1 before the body's, positioned
in CSS rather than in coordinates — and the one thing still drawn with
`insert_text`, the running Build stamp, stays ASCII for exactly the reason
`render()` already enforces.

**What it costs.** A returning annotated document has one more page, so a
re-export costs one more vision call even if the cover was never touched: at
200 DPI an A5 page is ~2.6k image tokens, and with the prompt around 5k in and
a few hundred out — roughly **$0.03** at Opus 5 list. The cover is deliberately
still read rather than skipped by hash: it is the emptiest page in a Build, so
it is exactly where a decision is likely to be written, and a page the
pipeline skipped would drop that ink silently.

## Baseline for Stage 1 (historical)

`tools/render_baseline.py` ran the *pre-Story* renderer over the fixture corpus
and is kept here only as the record of what it reported; the script itself is
gone with the renderer it measured, replaced by `tools/render_sample.py`. All
three research cases failed:

```
case          chars in  ·  out  lig  verdict
brief-900w        6461       6    0  FAIL  lost – — € ② ③ ④  chars dropped  3/8 long tokens split
glyphs             355       5    0  FAIL  lost — € → ② ③ ④ ✓  chars dropped
url-300            527       1    0  FAIL  2/2 long tokens split
```

The `lig` column reads 0 everywhere because this is the *pre-Story* baseline.
Story is what introduces `ﬁ`/`ﬃ` (U+FB01/U+FB03); the column exists so that
regression is visible the moment Stage 2 lands, rather than being discovered in
a delivered brief.

Digest and mail cases are not rendered by the baseline: `build_pdf` and
`build_mail_pdf` call `get_config()`, and the baseline deliberately loads no
config and no secrets. Wiring those needs a throwaway config.
