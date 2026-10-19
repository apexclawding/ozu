"""One page grammar for every document the Scribe receives.

Replaces `digest_pdf._Writer`, which drove `page.insert_text` directly against
base-14 Helvetica and so substituted `·` for every glyph the font lacked — 14
characters lost across 16 delivered documents (`docs/glyph-loss.md`). This
module lays out with `fitz.Story`, which does real CSS layout in **Charis SIL**
(a Charter derivative, built into mupdf, excellent on e-ink) with **Noto Sans
Symbols** picking up `☐ ✓ ② ③ ④`. No new dependency: PyMuPDF was already here.

Four rules hold this together, and each has a test.

**The text layer is a contract (D-01).** `_build_annotation_prompt()` hands it
to the vision model as the printed-side reference a tick is matched against, so
every character typeset here must come back out of `textlayer.extract()`
exactly. That is why text is escaped through `html.escape` on the way in
(verified: `& < > " '` round-trip), and why nothing here uses CSS that would
rewrite it. `text-transform: uppercase` is the trap — measured on this box,
Story ignores it, but a renderer that ever got it working would silently
change what the model reads. Style with weight and size; never with a
transform.

**Greyscale first (D-03).** The old renderer printed actions in
`RED (0.72, 0.08, 0.08)`, which computes to ~0.22 luminance against body ink at
0.12 — on a monochrome panel the single most important element rendered
*lighter* than the text around it. Hierarchy here is weight, size, rule and
space only. There is no colour to lose.

**A background colour is painted, not styled (D-05).** MuPDF's Story repaints
the last `background-color` it saw as a one-line stub at the top of *every*
subsequent page — measured on PyMuPDF 1.28.2 with `Story.element_positions`
and `page.get_drawings()`: a header band on page 1 leaves a grey smear above
the first line of pages 2, 3, 4… of every document long enough to turn one.
It was on every multi-page digest, mail and brief this pipeline has ever
delivered. Moving the fill onto the paragraphs instead of the wrapper makes it
worse (mupdf then paints a box behind every inline run as well), so the fill
left the stylesheet entirely: the band is a rectangle `render()` draws
underneath the text, on the page Story says the band actually landed on. That
is why `head()` stamps an id — it is the handle `element_positions` reports
by — and why `BAND_PAD` must stay equal to the `.band` padding in the CSS.

**A document is found before it is read (D-06).** `cover()` gives every
document a first page built for the Scribe's library thumbnail — ~126px wide,
where the old page 1's largest element rendered at under 5px and every Ozu
document looked like every other one. The day, the kind of document and one
line of contents, plus a drawn mark per kind (`MARKS`). It is a label: nothing
appears only there. Two constraints it must respect — the dark block is
painted in `render()` for the D-05 reason above, and no mark may draw an
unfilled rectangle, because an outlined square on a page read by the
annotation pass is a checkbox (D-04).

**The page has two readers.** The owner writes on it, and then the vision pass
reads their ink back off it. So printed furniture stays inside a light tonal band
at the top, and the body of the page stays white: grey behind text the model
must separate ink from is a transcription problem, not a taste one. For the
same reason the right margin is a deliberate writing gutter (`GUTTER`), not
leftover whitespace — it is where a tick or a margin note goes.

**The cover block carries a colour per kind (D-08).** `COVER_TINTS` — see the
rules beside it; the short version is *never red*, because the cover is a page
the annotation pass reads and red is the action ink, and *bright with ink
type*, because the panel renders black at full strength and colour at half.

**A document is a sequence of sections, and a section is its own Story
(D-07).** An edition carries several documents' worth of content — the mail,
a Build result, the weekly digest — and a Build result's pages must each
print *that* Build's id, not the edition's. So `section()` closes the current
Story and opens a new one on a fresh page, with its own running header. It is
done with separate Stories and not with CSS because CSS cannot do it here:
measured on PyMuPDF 1.28.2, `page-break-before: always` makes `Story.place()`
return "more" forever (a 30-page cap was the only thing that stopped the
probe), and `page-break-after: always` on a spacer lands the next section two
pages on, leaving a blank sheet between — a page nobody wants to swipe past,
and a page the annotation pass would pay to read if it came back. A new
Story starts exactly where the writer says, every time.

The same pass records where every header band and every `heading()` landed
and writes them into the PDF outline, and stamps the document's name and its
kind into the PDF metadata: the Scribe's library sorts by Author, and a kind
in that field is the one grouping a Send-to-Kindle document can carry.

**No text is lost at a page break (D-12).** `Story.place()` can lay a block
past the page foot and count it as placed, so `draw()` clips it and the
document carries on without it. Every page is checked for that and placed
again on a shorter rect, and every finished file is read back against the
text that went in: a file missing any of it raises `LostTextError` and is
deleted, never sent. See the note beside `BLOCK_ID`;
`tests/test_pagebreak.py` holds both halves.
"""
from __future__ import annotations

import datetime as dt
import html
import html.parser
import logging
import re
from collections.abc import Mapping
from pathlib import Path

import fitz  # PyMuPDF

from .config import brand

log = logging.getLogger("ozu.layout")

# A5 portrait against the Scribe's ~157.5×210mm panel. Already near-optimal:
# page geometry is not the thing to tune.
PAGE_W, PAGE_H = 420, 595
MARGIN_L, MARGIN_T, MARGIN_B = 42, 40, 44
GUTTER = 78          # right margin: writing surface, not whitespace
COLUMN_W = PAGE_W - MARGIN_L - GUTTER          # 300pt ≈ 62 characters

# Greyscale ramp. Body ink is the darkest thing printed; everything that is
# furniture rather than content sits measurably lighter, so the eye sorts the
# page by tone before it reads a word.
INK = "#1f1f1f"
MID = "#5c5c5c"
FAINT = "#8a8a8a"
RULE = "#c8c8c8"
BAND = "#efefef"
# Reversed out of the cover's dark block. `PAPER` is the page itself, so text
# in it is legible only where the block is actually painted — which is why
# `test_the_cover_block_is_painted_under_its_own_text` asserts the fill.
PAPER = "#ffffff"
DIM = "#c9c9c9"


CSS = f"""
* {{ font-family: Charis SIL; font-size: 9.5px; color: {INK};
     line-height: 1.5; }}

/* the header band: printed identity, grouped and kept off the writing area */
.kicker {{ font-size: 7.5px; color: {FAINT}; margin-bottom: 2px; }}
.title  {{ font-size: 16px; font-weight: bold; line-height: 1.2;
           margin-bottom: 3px; }}
.meta   {{ font-size: 8.5px; color: {FAINT}; margin-bottom: 0px; }}
/* No background-color here: see D-05 in the module docstring. The padding is
   still what shapes the band — `render()` paints a rectangle over exactly
   this box — so BAND_PAD below must move with these four values. */
.band   {{ padding-left: 8px; padding-right: 8px;
           padding-top: 8px; padding-bottom: 8px; margin-bottom: 14px; }}

h2 {{ font-size: 10.5px; font-weight: bold; margin-top: 16px;
      margin-bottom: 1px; border-bottom: 1px solid {RULE};
      padding-bottom: 3px; page-break-after: avoid; }}
h3 {{ font-size: 9.5px; font-weight: bold; margin-top: 10px;
      margin-bottom: 1px; page-break-after: avoid; }}

p {{ margin-top: 0px; margin-bottom: 5px; }}
.body    {{ margin-bottom: 6px; }}
.summary {{ color: {MID}; font-size: 8.5px; padding-left: 12px;
            margin-bottom: 4px; }}
/* hanging indent: a title that wraps must read as one entry, so the second
   line sits *inside* the first rather than starting back at the margin where
   the next entry's [topic] label begins. */
.note    {{ font-weight: bold; margin-bottom: 1px;
            padding-left: 14px; text-indent: -14px; }}
.hr      {{ border-top: 1px solid {RULE}; margin-top: 8px; margin-bottom: 8px; }}
.faint   {{ color: {FAINT}; font-size: 8px; padding-left: 12px; }}
.url     {{ color: {MID}; font-size: 7.5px; padding-left: 12px; }}
.bullet  {{ padding-left: 14px; text-indent: -8px; margin-bottom: 3px; }}

/* An action is the one thing that must be findable in a glance, and the one
   thing the owner writes *on*. Bold, boxed, and given room on both sides. The
   hanging indent keeps a wrapped line under the words rather than under the
   box, so ☐ never floats away from what it belongs to (D-04). */
.action {{ font-weight: bold; padding-left: 15px; text-indent: -15px;
           margin-top: 5px; margin-bottom: 2px; }}
.done   {{ color: {MID}; padding-left: 15px; text-indent: -15px;
           margin-top: 5px; margin-bottom: 2px; }}

.zero   {{ color: {MID}; font-style: italic; margin-top: 12px; }}
/* A ruled writing line: where the pen goes on a sheet that asks for writing. The rule is
   a border under an otherwise empty paragraph, so the text layer gains only
   whitespace (D-01 untouched) and the vision pass sees a line to write on,
   never a box to tick. */
.rule   {{ border-bottom: 1px solid {RULE}; margin-top: 16px; margin-bottom: 0px; }}
.footer {{ font-size: 7.5px; color: {FAINT}; margin-top: 18px;
           border-top: 1px solid {RULE}; padding-top: 6px; }}
/* A hairline around a handwriting crop: it says where the scan ends and the
   page resumes, which matters because the crop's own background is white.
   The border goes on a wrapper, not on the <img> — Story silently ignores
   `border` on an image (measured: zero drawings emitted), so styling the
   image directly looks correct in the CSS and does nothing on the page. */
img {{ margin-top: 0px; margin-bottom: 0px; }}
.crop {{ border: 1px solid {RULE}; padding: 3px;
         margin-top: 4px; margin-bottom: 6px; }}

/* --- the cover (D-06) ---------------------------------------------------
   Page 1 exists to be recognised at 120 pixels wide in the Scribe's library,
   where the old first page was an even grey smudge under a truncated
   filename. Everything here is sized for that thumbnail first and the held
   page second, which is why the day numeral is 96px: scaled to the library
   grid it is still ~8px tall, the smallest thing that survives.
   The dark block carries no background-color for the same reason the header
   band does not (D-05) — `render()` paints it, and the type inside it is
   PAPER, so if that fill were ever lost the words would vanish rather than
   quietly turn grey. `COVER_PAD` must stay equal to the `.cvr` padding. */
.cvr      {{ padding-left: 12px; padding-right: 12px;
             padding-top: 12px; padding-bottom: 14px; margin-bottom: 26px; }}
.cvr-k    {{ font-size: 11px; color: {DIM}; margin-bottom: 8px; }}
.cvr-day  {{ font-size: 96px; font-weight: bold; color: {PAPER};
             line-height: 1.0; margin-bottom: 4px; }}
.cvr-mon  {{ font-size: 27px; font-weight: bold; color: {PAPER};
             line-height: 1.1; margin-bottom: 3px; }}
.cvr-wd   {{ font-size: 12px; color: {DIM}; margin-bottom: 0px; }}
.cvr-word {{ font-size: 30px; font-weight: bold; line-height: 1.12;
             margin-bottom: 3px; }}
.cvr-sub  {{ font-size: 13px; color: {MID}; margin-bottom: 12px; }}
.cvr-line {{ font-size: 10.5px; color: {MID}; margin-bottom: 0px; }}
"""

# The printed marker a tick is drawn on. `☐` is verified extractable through
# textlayer.extract, so the annotation prompt still has an exact printed
# reference to match against (D-04 — never a drawn vector box, which would
# strip the marker from the text layer entirely).
BOX = "☐"
TICKED = "☑"

# The header band, drawn rather than styled (D-05). BAND_ID is what
# `Story.element_positions` reports the band's box under; BAND_PAD is the
# `.band` padding above, which turns that reported *content* box back into the
# painted box. BAND_RGB is BAND as MuPDF wants it.
BAND_ID = "ozuband"
BAND_PAD = 8
HEADING_ID = "ozuh"      # `heading()` ids, for the outline

# Text lost at a page break (D-12). Measured on PyMuPDF 1.28.2: at a few
# exact positions near the foot of a page, `Story.place()` lays the next
# block(s) *below* the rect it was given and counts them as placed, so
# `draw()` clips them away and the next page starts after them (or the
# document ends early) — no warning, no exception. In real use it cut a
# heading and its whole section out of a brief; a 1-pt sweep reproduces it
# after a plain paragraph too (2-4 offsets in every 140). Story reports
# positions only for elements that carry an id, so every block gets one
# (BLOCK_ID); a page whose blocks reach past the rect is placed again on a
# shorter rect (REFLOW_STEP at a time, up to REFLOW_TRIES), which pushes the
# stray block onto the next page. Then `_verify_text` reads the finished
# file back and refuses it if any text that went in is missing — the
# guarantee, whatever MuPDF does next.
BLOCK_ID = "ozux"
_BLOCK_TAG = re.compile(
    r"<(p|h[1-6]|div|li|ul|ol|table|tr|td|th|blockquote|pre)\b(?![^>]*\sid=)",
    re.I)
# The running header is stamped on this baseline, in the top margin, after
# layout (see render). Nothing laid out by a Story starts above MARGIN_T.
STAMP_Y = 24
REFLOW_STEP = 2.0        # pt
REFLOW_TRIES = 40        # 80 pt: more than any single line, less than a page


class LostTextError(RuntimeError):
    """A rendered PDF is missing text its HTML carried. Raised instead of
    writing a document that silently drops part of what it was asked to
    print; the file is deleted so nothing can send it."""


def _rgb(hexcolor: str) -> tuple[float, float, float]:
    """A palette entry as MuPDF wants it, so a drawn fill and the CSS it
    matches are never written down as two different numbers."""
    return tuple(int(hexcolor[i:i + 2], 16) / 255 for i in (1, 3, 5))


BAND_RGB = _rgb(BAND)

# The cover's dark date block, drawn rather than styled for the same reason the
# header band is (D-05). It bleeds to the top edge of the sheet — a thumbnail
# is cropped and scaled by the library, and an element that touches the trim is
# the one that still reads after that.
COVER_ID = "ozucover"
COVER_PAD = 12          # = the `.cvr` padding above
COVER_TOP = 56          # where the cover story starts; the block fills upwards
COVER_RGB = _rgb(INK)
MID_RGB = _rgb(MID)
PAPER_RGB = _rgb(PAPER)

# The cover block's colour, per kind (D-08, 2026-09-12). The Scribe is a
# Colorsoft and its library thumbnails render in colour, so the one big
# shape on the cover can say what kind of document this is before the type
# can. Three rules, and a test holds each:
#   - **never red**: red ink is the only thing that creates an action
#     (invariant 5), the cover is read by the annotation pass like any page,
#     and printed-vs-handwritten is the first distinction that read loses
#     (5b) — so no tint may have a dominant red channel;
#   - off the ink language: pure blue (idea) and green (personal) are taken,
#     so the palette is gold, purple and teal;
#   - **bright, with ink type on it**. The first palette was dark and
#     saturated with white type, and on the device (2026-09-12) gold and
#     olive were "almost identical": Kaleido paints colour at roughly half a
#     screen's strength and drags dark tones toward brown-grey. Black is the
#     one thing it renders at full strength, so the block is light and the
#     type on it is ink — the reverse of the ink-black cover.
# Body pages stay greyscale (D-03): colour is a library aid, not a reading
# aid. Research briefs (and the site audit, which borrows the mark) keep the
# ink block on purpose: four kinds, four looks, and black is the fourth.
COVER_TINTS = {
    "morning": "#f2c12e",     # gold — the sun mark
    "evening": "#a06be8",     # purple — the crescent (violet, not the idea blue)
    "build": "#3fcfbf",       # teal — a question in flight
    "result": "#3fcfbf",      # the same thread, answered
}


def cover_rgb(mark: str) -> tuple[float, float, float]:
    """The block fill for a kind: its tint, or ink for the rest."""
    return _rgb(COVER_TINTS[mark]) if mark in COVER_TINTS else COVER_RGB


def cover_type_rgb(mark: str) -> tuple[float, float, float]:
    """What the type on the block is set in: ink on a tint, paper on ink."""
    return _rgb(INK) if mark in COVER_TINTS else PAPER_RGB


# Overrides for the cover Story on a tinted block: the reversed-out classes
# become ink and the dimmed ones mid-grey. CSS, not a second stylesheet —
# the base rules still apply and only the colours move.
COVER_TINT_CSS = f"""
.cvr-day, .cvr-mon {{ color: {INK}; }}
.cvr-k, .cvr-wd   {{ color: {MID}; }}
"""


# The type mark: one drawn silhouette per kind of document, low and right,
# where the eye lands after the date. It is a *drawing*, so it adds nothing to
# the text layer (D-01 untouched) — and deliberately no shape that could be
# mistaken for the printed `☐` an ink tick closes (D-04, invariant 5): no
# empty square, and no tick.
MARK_BOX = 92
MARK_RECT = fitz.Rect(PAGE_W - MARGIN_L - MARK_BOX, PAGE_H - MARGIN_B - MARK_BOX,
                      PAGE_W - MARGIN_L, PAGE_H - MARGIN_B)


# `Shape.finish()` closes the path by DEFAULT — a stroked polyline gets a
# segment from its last point back to its first, which drew the two branch
# marks below as filled wedges the first time they were rendered. Every
# stroke-only finish here passes `closePath=False` deliberately.


def _pt(r: fitz.Rect, fx: float, fy: float) -> fitz.Point:
    """A point inside the mark box in fractions of its own size, so every mark
    below is written once and scales with MARK_BOX."""
    return fitz.Point(r.x0 + fx * r.width, r.y0 + fy * r.height)


def _mark_digest(sh, r) -> None:
    """Seven bars — a week, which is what a digest covers."""
    for i, h in enumerate((0.34, 0.62, 0.44, 0.86, 0.52, 1.0, 0.72)):
        x = 0.02 + i * 0.145
        sh.draw_rect(fitz.Rect(_pt(r, x, 1 - h), _pt(r, x + 0.10, 1.0)))
    sh.finish(color=None, fill=COVER_RGB)


def _mark_mail(sh, r) -> None:
    """An envelope: a filled body with the flap cut out of it in paper, never
    an outlined rectangle — an empty box on a page is a checkbox."""
    sh.draw_rect(fitz.Rect(_pt(r, 0.0, 0.18), _pt(r, 1.0, 0.82)))
    sh.finish(color=None, fill=COVER_RGB)
    sh.draw_line(_pt(r, 0.0, 0.18), _pt(r, 0.5, 0.55))
    sh.draw_line(_pt(r, 0.5, 0.55), _pt(r, 1.0, 0.18))
    sh.finish(color=PAPER_RGB, width=7, closePath=False)


def _mark_research(sh, r) -> None:
    """A magnifier."""
    sh.draw_circle(_pt(r, 0.40, 0.40), 0.33 * r.width)
    sh.finish(color=COVER_RGB, width=11, closePath=False)
    sh.draw_line(_pt(r, 0.66, 0.66), _pt(r, 0.97, 0.97))
    sh.finish(color=COVER_RGB, width=14, closePath=False)


def _mark_learn(sh, r) -> None:
    """An open book."""
    for edge, spine in ((0.0, 0.47), (1.0, 0.53)):
        sh.draw_polyline([_pt(r, edge, 0.28), _pt(r, spine, 0.38),
                          _pt(r, spine, 0.92), _pt(r, edge, 0.82)])
        sh.finish(color=None, fill=COVER_RGB, closePath=True)
    sh.draw_line(_pt(r, 0.5, 0.36), _pt(r, 0.5, 0.90))
    sh.finish(color=PAPER_RGB, width=4, closePath=False)



def _mark_build(sh, r) -> None:
    """A branch that has not merged: the stem forks, and the far end is open —
    this Build is a question waiting for an answer in ink."""
    sh.draw_line(_pt(r, 0.22, 1.0), _pt(r, 0.22, 0.30))
    sh.draw_line(_pt(r, 0.22, 0.52), _pt(r, 0.80, 0.52))
    sh.draw_line(_pt(r, 0.80, 0.52), _pt(r, 0.80, 0.30))
    sh.finish(color=COVER_RGB, width=10, closePath=False)
    for c in (_pt(r, 0.22, 0.20), _pt(r, 0.80, 0.20)):
        sh.draw_circle(c, 0.10 * r.width)
    # filled in paper, not left transparent: the heads read as two open ends
    # rather than two rings with the stem showing through them
    sh.finish(color=COVER_RGB, fill=PAPER_RGB, width=9)


def _mark_result(sh, r) -> None:
    """The same branch, merged: the side line comes back into the stem under a
    solid head. Drawn as an arrow rather than a loop on purpose — a closed
    rectangle sitting on a page is a checkbox to anything reading it, and this
    page is read by the annotation pass (invariant 5)."""
    sh.draw_line(_pt(r, 0.30, 1.0), _pt(r, 0.30, 0.26))
    sh.draw_line(_pt(r, 0.92, 0.30), _pt(r, 0.92, 0.66))
    sh.draw_line(_pt(r, 0.92, 0.66), _pt(r, 0.52, 0.66))
    sh.finish(color=COVER_RGB, width=10, closePath=False)
    sh.draw_polyline([_pt(r, 0.30, 0.66), _pt(r, 0.58, 0.52),
                      _pt(r, 0.58, 0.80)])
    sh.finish(color=None, fill=COVER_RGB, closePath=True)
    sh.draw_circle(_pt(r, 0.30, 0.16), 0.15 * r.width)
    sh.finish(color=COVER_RGB, fill=COVER_RGB, width=2)


def _mark_morning(sh, r) -> None:
    """A sun on the horizon: a filled half-disc over a rule, three rays. The
    disc is a sector, not a circle under a paper rectangle — a rectangle of
    any fill is one more shape for a reader of the image to wonder about."""
    centre = _pt(r, 0.5, 0.74)
    sh.draw_sector(centre, _pt(r, 0.86, 0.74), 180, fullSector=True)
    sh.finish(color=None, fill=COVER_RGB, closePath=True)
    sh.draw_line(_pt(r, 0.0, 0.74), _pt(r, 1.0, 0.74))
    sh.finish(color=COVER_RGB, width=6, closePath=False)
    for a, b in (((0.5, 0.26), (0.5, 0.06)), ((0.18, 0.40), (0.04, 0.26)),
                 ((0.82, 0.40), (0.96, 0.26))):
        sh.draw_line(_pt(r, *a), _pt(r, *b))
    sh.finish(color=COVER_RGB, width=7, closePath=False)


def _mark_evening(sh, r) -> None:
    """A crescent: a filled disc with a second disc cut out of it in paper,
    and two stars. The cut is a circle, so nothing rectangular is drawn."""
    sh.draw_circle(_pt(r, 0.42, 0.56), 0.36 * r.width)
    sh.finish(color=None, fill=COVER_RGB)
    sh.draw_circle(_pt(r, 0.60, 0.46), 0.32 * r.width)
    sh.finish(color=None, fill=PAPER_RGB)
    for c, rad in ((_pt(r, 0.86, 0.16), 0.055), (_pt(r, 0.96, 0.44), 0.035)):
        sh.draw_circle(c, rad * r.width)
        sh.finish(color=None, fill=COVER_RGB)


COVER_LINE_MAX = 120


def _as_date(value) -> "dt.date | None":
    """A date out of whatever the payload happens to carry: a `date`, a
    `datetime`, `"2026-09-05"`, or `"2026-08-30 12:36"` (Ozu Mail stamps the
    hour). Anything else is no date rather than a wrong one — a cover with no
    numeral is a cosmetic loss; a cover dated 1900 is a lie about the vault."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        log.warning("cover: unparseable date %r; printing none", value)
        return None


MARKS = {
    "digest": _mark_digest,
    "mail": _mark_mail,
    "research": _mark_research,
    "learn": _mark_learn,
    "build": _mark_build,
    "result": _mark_result,
    "morning": _mark_morning,
    "evening": _mark_evening,
}

# What the Scribe's library shows under "Author" for a document of each
# kind, and therefore what "sort by author" groups. Send-to-Kindle carries
# no folder and no tag; the PDF Author field is the one piece of metadata
# that survives the trip and that the device can sort on. Each value is
# `<brand> <Kind>`, the brand read at call time (OZU_BRAND).
class _Authors(Mapping):
    _KINDS = {
        "edition": "Edition",
        "build": "Build",
        "research": "Research",
        "digest": "Digest",
        "learn": "Learn",
        "mail": "Mail",
    }

    def __getitem__(self, kind: str) -> str:
        return f"{brand()} {self._KINDS[kind]}"

    def __iter__(self):
        return iter(self._KINDS)

    def __len__(self) -> int:
        return len(self._KINDS)


AUTHORS = _Authors()


def esc(s) -> str:
    """Text into HTML. Every string from the vault or the model goes through
    here — an unescaped `&` in a URL would reach the text layer as `&amp;`
    and break the round trip that D-01 depends on."""
    return html.escape("" if s is None else str(s), quote=True)


# Inline Markdown. Research briefs are model prose and lean on `**emphasis**`
# heavily — the old renderer printed the asterisks, so a brief arrived littered
# with `**The running-cost arithmetic favours the heat pump:**`. That is a
# large part of "reads like a machine wrote it": the emphasis was not just
# unstyled, it was visible punctuation in the middle of a sentence.
#
# Escaping happens first and the patterns run on the escaped string, which is
# safe because `html.escape` does not produce or consume `*`.
_MD_BOLD = re.compile(r"\*\*(\S(?:.*?\S)?)\*\*", re.S)
_MD_ITALIC = re.compile(r"(?<![*\w])\*(\S(?:.*?\S)?)\*(?![*\w])", re.S)
_MD_RULE = re.compile(r"^([-*_])\1{2,}$")


def inline(text: str) -> str:
    """Escape, then turn `**bold**` and `*italic*` into real weight and slope.

    The asterisks leave the text layer, exactly as `##` does — they are syntax,
    not content, and the printed page never showed them as characters anyway.
    `tests/test_textlayer.py::_content` states independently which markers may
    be consumed, so this cannot quietly start eating something else."""
    s = esc(text)
    s = _MD_BOLD.sub(r"<b>\1</b>", s)
    s = _MD_ITALIC.sub(r"<i>\1</i>", s)
    return s


class Document:
    """Accumulates HTML, then lays it out over as many A5 pages as it needs.

    Content is kept as a list of *sections* (D-07). A plain document is one
    section; an edition is one per part, each opened with `section()`. Every
    section is laid out by its own Story on a fresh page and may carry its
    own running header. `_parts` is the current section's HTML, which is why
    every builder below can keep appending to it unchanged."""

    def __init__(self) -> None:
        self._sections: list[dict] = [{"parts": [], "header": ""}]
        self._archive = fitz.Archive()
        self._n_images = 0
        self._cover: dict | None = None
        # (element id, outline level, text) in document order — the PDF
        # outline is built from where each of these ids lands.
        self._outline: list[tuple[str, int, str]] = []
        self._n_ids = 0

    @property
    def _parts(self) -> list[str]:
        return self._sections[-1]["parts"]

    def _next_id(self, prefix: str) -> str:
        self._n_ids += 1
        return f"{prefix}{self._n_ids}"

    # --- sections -------------------------------------------------------
    def section(self, page_header: str = "") -> "Document":
        """Start a new section: a fresh page and its own Story, with
        `page_header` stamped on every page it occupies (ASCII, for the
        same base-14 reason as `render()`'s). An empty current section is
        reused rather than left as a blank page."""
        if page_header and not page_header.isascii():
            raise ValueError(f"page_header must be ASCII (base-14 font): "
                             f"{page_header!r}")
        if self._sections[-1]["parts"]:
            self._sections.append({"parts": [], "header": page_header})
        else:
            self._sections[-1]["header"] = page_header
        return self

    # --- cover ----------------------------------------------------------
    def cover(self, mark: str, *, date=None, kicker: str = "",
              wordmark: str = "", subtitle: str = "",
              line: str = "") -> "Document":
        """Give the document a cover page (D-06).

        `mark` names a drawing in `MARKS`; `wordmark` is the two or three
        words that say what this is, and `line` is the single sentence of
        what is inside. Nothing here is content that exists nowhere else —
        the cover is a label, so a reader who skips it loses nothing.

        `date` takes a `date`, a `datetime` or any string starting with
        `YYYY-MM-DD` (the payloads carry all three), because the day is the
        one thing that must be legible in the library grid."""
        if mark not in MARKS:
            raise ValueError(f"unknown cover mark {mark!r}; "
                             f"have {sorted(MARKS)}")
        day = _as_date(date)
        self._cover = {
            "mark": mark,
            "kicker": kicker,
            "day": f"{day.day:02d}" if day else "",
            "month": day.strftime("%B").upper() if day else "",
            "weekday": (f"{day.strftime('%A')} · {day.year}" if day else ""),
            "wordmark": wordmark,
            # a cover line that wraps three times is a paragraph, and a
            # paragraph on a cover is the smudge this page exists to replace.
            # Both of these carry document content (a title, a headline), so
            # both are capped — the body prints them in full anyway.
            "subtitle": subtitle[:COVER_LINE_MAX].rstrip(),
            "line": line[:COVER_LINE_MAX].rstrip(),
        }
        return self

    def _cover_html(self) -> str:
        c = self._cover
        rows = []
        if c["kicker"]:
            rows.append(f'<p class="cvr-k">{esc(c["kicker"])}</p>')
        if c["day"]:
            rows.append(f'<p class="cvr-day">{esc(c["day"])}</p>')
            rows.append(f'<p class="cvr-mon">{esc(c["month"])}</p>')
            rows.append(f'<p class="cvr-wd">{esc(c["weekday"])}</p>')
        block = f'<div id="{COVER_ID}" class="cvr">{"".join(rows)}</div>'
        tail = []
        if c["wordmark"]:
            tail.append(f'<p class="cvr-word">{esc(c["wordmark"])}</p>')
        if c["subtitle"]:
            tail.append(f'<p class="cvr-sub">{esc(c["subtitle"])}</p>')
        if c["line"]:
            tail.append(f'<p class="cvr-line">{esc(c["line"])}</p>')
        return block + "".join(tail)

    # --- header ---------------------------------------------------------
    def head(self, kicker: str, title: str, meta: str = "", *,
             outline: str = "") -> "Document":
        """The header band. Its id is what `render()` paints the band under
        (D-05) and files it in the outline by — one id per band, because an
        edition has one band per section. `outline` is the entry's text in
        the PDF outline when the band's title alone would not say what the
        section is (a band titled with a date, say)."""
        rows = []
        if kicker:
            rows.append(f'<p class="kicker">{esc(kicker)}</p>')
        rows.append(f'<p class="title">{esc(title)}</p>')
        if meta:
            rows.append(f'<p class="meta">{esc(meta)}</p>')
        el_id = self._next_id(BAND_ID)
        self._outline.append((el_id, 1, outline or title))
        self._parts.append(
            f'<div id="{el_id}" class="band">{"".join(rows)}</div>')
        return self

    # --- structure ------------------------------------------------------
    def heading(self, text: str) -> "Document":
        el_id = self._next_id(HEADING_ID)
        self._outline.append((el_id, 2, text))
        self._parts.append(f'<h2 id="{el_id}">{esc(text)}</h2>')
        return self

    def subheading(self, text: str) -> "Document":
        self._parts.append(f"<h3>{esc(text)}</h3>")
        return self

    def para(self, text: str, cls: str = "body") -> "Document":
        if text:
            self._parts.append(f'<p class="{cls}">{esc(text)}</p>')
        return self

    def note(self, title: str, summary: str = "", label: str = "") -> "Document":
        head = f"[{esc(label)}]  {esc(title)}" if label else esc(title)
        self._parts.append(f'<p class="note">{head}</p>')
        if summary:
            self._parts.append(f'<p class="summary">{esc(summary)}</p>')
        return self

    def action(self, text: str, *, done: bool = False,
               source: str = "") -> "Document":
        mark, cls = (TICKED, "done") if done else (BOX, "action")
        self._parts.append(f'<p class="{cls}">{mark}  {esc(text)}</p>')
        if source:
            self._parts.append(f'<p class="faint">{esc(source)}</p>')
        return self

    def zero(self, text: str) -> "Document":
        self._parts.append(f'<p class="zero">{esc(text)}</p>')
        return self

    def footer(self, text: str) -> "Document":
        self._parts.append(f'<p class="footer">{esc(text)}</p>')
        return self

    def rules(self, n: int = 2) -> "Document":
        """`n` ruled writing lines. Content is a single space per line, so
        extraction returns whitespace and nothing else — the sheet must not
        print anything a tick could be matched against (invariant 5)."""
        self._parts.extend('<p class="rule"> </p>' for _ in range(max(0, n)))
        return self

    # --- images ---------------------------------------------------------
    def image(self, path: Path, width_pct: int = 100) -> "Document":
        """Place a handwriting crop. Bytes go into the Story archive under a
        generated name: two vault assets can share a basename, and referencing
        them by their own filename would silently render the same crop twice."""
        try:
            data = Path(path).read_bytes()
        except Exception:
            self.para(f"(ink image missing: {Path(path).name})", cls="faint")
            return self
        name = f"img{self._n_images}{Path(path).suffix or '.png'}"
        self._n_images += 1
        self._archive.add(data, name)
        self._parts.append(f'<div class="crop">'
                           f'<img src="{name}" style="width:{width_pct}%">'
                           f'</div>')
        return self

    # --- markdown -------------------------------------------------------
    def markdown(self, text: str) -> "Document":
        """The light Markdown briefs and mail bodies actually use: headings,
        bullets, bare URLs, paragraphs. Deliberately not a full parser — this
        renders model prose, and anything it does not recognise must fall
        through as body text rather than be dropped."""
        for line in (text or "").splitlines():
            s = line.strip()
            if not s:
                continue
            if _MD_RULE.match(s):
                self._parts.append('<p class="hr"> </p>')
            elif s.startswith("#"):
                self._parts.append(f"<h3>{inline(s.lstrip('#').strip())}</h3>")
            elif s.startswith(("- ", "* ")):
                self._parts.append(f'<p class="bullet">•  {inline(s[2:])}</p>')
            elif s.startswith("http"):
                self.para(s, cls="url")
            else:
                self._parts.append(f'<p class="body">{inline(s)}</p>')
        return self

    # --- output ---------------------------------------------------------
    def render(self, out_path: Path, page_header: str = "", *,
               author: str = "", title: str = "") -> Path:
        """`page_header` stamps one small line of running identity into the
        top margin of every page that has no section header of its own —
        the Build documents use it to print the Build id on each page,
        because matching a returning page must work whichever page of the
        PDF the ink lands on. A section's own header (see `section()`) wins
        on the pages of that section. Either goes through `page.insert_text`
        (base-14 Helvetica), so it must stay ASCII: the glyph-loss lesson
        (docs/glyph-loss.md) is about exactly what that font does to anything
        else. The stamp joins the text layer, which is the point —
        `textlayer.extract` is how the pipeline reads it back.

        `author` and `title` go into the PDF metadata. The title defaults to
        the file's own stem so the device names the document exactly as the
        file is named; the author is the kind (`AUTHORS`), which is what the
        Scribe's sort-by-author groups on."""
        if page_header and not page_header.isascii():
            raise ValueError(f"page_header must be ASCII (base-14 font): "
                             f"{page_header!r}")
        where = fitz.Rect(MARGIN_L, MARGIN_T, MARGIN_L + COLUMN_W,
                          PAGE_H - MARGIN_B)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        writer = fitz.DocumentWriter(str(out_path))
        pages = 0
        bands: dict[int, dict[str, fitz.Rect]] = {}   # page → band id → box
        landed: dict[str, int] = {}          # outline id → page index
        headers: dict[int, str] = {}         # page index → running header
        block: fitz.Rect | None = None

        def note_element(el) -> None:
            """Where a header band or a heading actually landed, per page.
            Bands are painted there (D-05); both go into the outline. Story
            reports every element twice per place() (measured), so both
            maps are keyed by id — a band is one box, not two on top of
            each other."""
            if el.id and el.id.startswith(BAND_ID):
                bands.setdefault(pages, {})[el.id] = (
                    fitz.Rect(el.rect)
                    + (-BAND_PAD, -BAND_PAD, BAND_PAD, BAND_PAD))
                landed.setdefault(el.id, pages)
            elif el.id and el.id.startswith(HEADING_ID):
                landed.setdefault(el.id, pages)

        def note_block(el) -> None:
            """Where the cover's dark block landed. It is grown to the sheet's
            top and both edges: the fill is what makes the reversed type
            readable, so it may not stop short of any character in it."""
            nonlocal block
            if el.id == COVER_ID:
                block = fitz.Rect(0, 0, PAGE_W,
                                  fitz.Rect(el.rect).y1 + COVER_PAD)

        if self._cover:
            dev = writer.begin_page(fitz.Rect(0, 0, PAGE_W, PAGE_H))
            tinted = self._cover["mark"] in COVER_TINTS
            cover = fitz.Story(html=self._cover_html(),
                               user_css=CSS + (COVER_TINT_CSS if tinted else ""))
            # The mark owns the bottom-right corner, so the cover's text
            # column stops above it — a wordmark that ran into the drawing
            # would be the one element on the page nobody could read.
            spill, _ = cover.place(fitz.Rect(MARGIN_L, COVER_TOP,
                                             PAGE_W - MARGIN_L,
                                             MARK_RECT.y0 - 10))
            if spill:
                log.warning("cover overflows its page in %s; the overflow is "
                            "dropped — shorten the cover line", out_path)
            cover.element_positions(note_block)
            cover.draw(dev)
            writer.end_page()
            pages = 1

        printed = [self._cover_html()] if self._cover else []
        for section in self._sections:
            if not section["parts"]:
                continue
            body = _tag_blocks("".join(section["parts"]))
            printed.append(body)
            story = fitz.Story(html=body, user_css=CSS, archive=self._archive)
            more = 1
            while more:
                more, _ = _place_page(story, where)
                dev = writer.begin_page(fitz.Rect(0, 0, PAGE_W, PAGE_H))
                # Must run between place() and draw(): place() computes the
                # positions, and the next place() overwrites them.
                story.element_positions(note_element)
                story.draw(dev)
                writer.end_page()
                headers[pages] = section["header"] or page_header
                pages += 1
                if pages > 200:      # a runaway Story would fill the disk
                    log.error("layout exceeded 200 pages; truncating %s",
                              out_path)
                    break
            if pages > 200:
                break
        writer.close()

        # Subsetting is worth real bytes now that a face is actually embedded:
        # measured 199 KB → 72 KB on a Story/Charis page, extraction identical.
        # PyMuPDF refuses to rewrite a file it has open, so subset via a
        # sibling and move it into place. The same reopen is where the
        # running header lands on every page (above the content rect, inside
        # the top margin, in the furniture grey).
        tmp = out_path.with_suffix(out_path.suffix + ".subset")
        doc = fitz.open(out_path)
        if self._cover and doc.page_count:
            page = doc[0]
            if block is not None:
                page.draw_rect(block, color=None,
                               fill=cover_rgb(self._cover["mark"]),
                               overlay=False)
            shape = page.new_shape()
            MARKS[self._cover["mark"]](shape, MARK_RECT)
            shape.commit()
        for index, rects in bands.items():
            if index < doc.page_count:
                for rect in rects.values():
                    # overlay=False prepends the fill, so it lands *under*
                    # the text Story already drew — a background, not a
                    # redaction.
                    doc[index].draw_rect(rect, color=None, fill=BAND_RGB,
                                         overlay=False)
        if self._cover and page_header:
            # The cover belongs to the whole document, so it carries the
            # document-wide header (a Build proposal's id), never a
            # section's. The stamp lands inside the dark block, where
            # furniture grey is invisible. It has to stay *visible*, not
            # merely present: the annotation prompt tells the model every
            # page of a Build prints its id, and a page it cannot read the
            # id off is a page whose ink routes nowhere.
            headers[0] = page_header
        for n, page in enumerate(doc):
            stamp = headers.get(n, "")
            if stamp:
                on_cover = bool(self._cover) and n == 0
                page.insert_text((MARGIN_L, STAMP_Y), stamp, fontsize=7,
                               color=cover_type_rgb(self._cover["mark"])
                               if on_cover else (0.54, 0.54, 0.54))
        toc = [[level, text, landed[el_id] + 1]
               for el_id, level, text in self._outline
               if el_id in landed and landed[el_id] < doc.page_count]
        if toc:
            # a level-2 entry needs a level-1 parent above it; a heading
            # before any band is promoted rather than dropped
            fixed, seen_l1 = [], False
            for level, text, page in toc:
                if level == 1:
                    seen_l1 = True
                fixed.append([1 if not seen_l1 else level, text, page])
            doc.set_toc(fixed)
        doc.set_metadata({"title": title or out_path.stem,
                          "author": author, "creator": "ozu",
                          "producer": "PyMuPDF"})
        doc.subset_fonts()
        doc.save(tmp, garbage=3, deflate=True)
        doc.close()
        tmp.replace(out_path)
        _verify_text(out_path, printed)
        return out_path

def _tag_blocks(body: str) -> str:
    """Give every block element without an id one, so `Story.element_
    positions` reports it and `_place_page` can see it land (D-12). The ids
    are only ever read by layout; nothing styles or links them."""
    n = iter(range(1, 1 << 30))
    return _BLOCK_TAG.sub(lambda m: f'{m.group(0)} id="{BLOCK_ID}{next(n)}"',
                          body)


def _overflow(story: "fitz.Story", rect: "fitz.Rect") -> bool:
    """True when the last place() laid any block below `rect` (D-12)."""
    over = []
    story.element_positions(
        lambda el: over.append(1) if el.rect[3] > rect.y1 + 0.5 else None)
    return bool(over)


def _place_page(story: "fitz.Story", where: "fitz.Rect") -> tuple[int, "fitz.Rect"]:
    """Place one page, never past its rect (D-12). A Story advances only
    when a page is *drawn* (measured: place() twice without a draw lays out
    the same page twice), so an overflowing page is simply placed again on
    a rect REFLOW_STEP shorter — nothing to rewind. If no rect helps, the
    last attempt stands and `_verify_text` refuses the file."""
    rect = fitz.Rect(where)
    for attempt in range(REFLOW_TRIES + 1):
        if attempt:
            rect = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y1 - REFLOW_STEP)
        more, _ = story.place(rect)
        if not _overflow(story, rect):
            if attempt:
                log.info("reflowed a page %d pt short to keep a block that "
                         "Story placed past the page foot", round(where.y1 -
                                                                  rect.y1))
            return more, rect
    log.error("a page still overflows after %d reflows", REFLOW_TRIES)
    return more, rect


class _Text(html.parser.HTMLParser):
    """The text nodes of an HTML fragment, in order — what the page must
    show. `convert_charrefs` turns `&#x27;` back into the apostrophe that
    prints."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self.chunks.append(data)


_INVISIBLE = re.compile(r"[\s­​‌‍⁠﻿]+")


def _squash(text: str) -> str:
    """Compare characters, not layout: line breaks and spacing are the
    renderer's to choose. Ligatures fold exactly as the text layer folds
    them (invariant 10)."""
    from .textlayer import fold
    return _INVISIBLE.sub("", fold(text))


def _verify_text(out_path: Path, printed: list[str]) -> None:
    """Read the finished PDF back and require every text node that went in
    to come out, in order (D-12). The running header is stamped into the top
    margin of each page after layout, so the read starts below it — by
    position, not by matching its text: a header that repeats a title on the
    page must not take the title's place."""
    from .textlayer import extract
    below_stamp = fitz.Rect(0, STAMP_Y + 6, PAGE_W, PAGE_H)
    doc = fitz.open(out_path)
    try:
        pages = [_squash(extract(page, clip=below_stamp)) for page in doc]
    finally:
        doc.close()
    have = "".join(pages)
    missing, at = [], 0
    for fragment in printed:
        parser = _Text()
        parser.feed(fragment)
        parser.close()
        for chunk in parser.chunks:
            want = _squash(chunk)
            if not want:
                continue
            found = have.find(want, at)
            if found < 0:
                missing.append(chunk.strip())
            else:
                at = found + len(want)
    if missing:
        out_path.unlink(missing_ok=True)
        raise LostTextError(
            f"{out_path.name}: {len(missing)} piece(s) of text never reached "
            f"the page, first {missing[0][:80]!r}; the PDF was not written")
