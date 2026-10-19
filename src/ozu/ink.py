"""Does this returning page carry any handwriting? (D-09, 2026-09-20)

Every "<brand> " document that comes back from the Scribe is one we generated and
still have on disk, so the question "is there ink on this page" does not need
a model to answer: render the copy we sent and the copy that came back at the
same low resolution, and count the pixels that are dark in the return and were
not dark in the original. That is free, deterministic, and it is the answer to
where the vision spend was going.

**Measured on the whole corpus of returning documents (19 documents, 110
pages, 2026-08-23 → 2026-09-20).** 74 of those pages — 67 % — carry no ink at
all, and the pipeline had paid a vision call for every one of them: every
cover page, every unannotated page, every time a document came back. At
$0.065 a page that is $4.81 of the $7.15 those documents cost.

Three things that measurement taught, each of which is a line of code here:

**A single tick is small.** The faintest real ink in the corpus on
2026-09-20 was a pair of handwritten `✓` marks on a Ozu Mail cover at
**235 px**; one tick alone is about half that — and one drawn over a printed
box, less still (72 px, 2026-09-30; see the tick check below). An earlier threshold of 300 px would have dropped it. So
`THRESHOLD` is 100 — below the faintest real mark by more than a factor of
two, and an order of magnitude above the largest artefact below.

**Antialiasing is not ink.** The Scribe's export re-rasterises the page, so
glyph edges land a shade darker than they did in the original and a naive
difference reports ~60 px of "ink" on a page of pure print. Ignoring anything
within `HALO` pixels of ink the original already had removes it: that same
page scores 9 px. This is why the comparison dilates the original's dark mask
instead of simply raising the threshold — a threshold high enough to swallow
glyph edges is also high enough to swallow a tick.

**Everything uncertain is read.** A false positive costs $0.065; a false
negative loses the owner's ink, which is the one unacceptable failure. So the
answer is "read it" whenever the original cannot be found, a returning page
has no counterpart in it, the geometry does not match, the difference is so
large it must be misalignment rather than writing (`MAX_FRACTION`), or
anything at all raises. Checked against every page the vision pass ever read
and found nothing on: 65 pages, zero disagreements.

The bottom 5 % of the page is excluded for the same reason `ingest._page_hash`
excludes it — Amazon stamps a page-position footer there that changes between
exports.

**Which printed ☐ carries a tick (2026-09-30).** The same comparison, read
per action line instead of per page: `locate_ticks` takes each ☐ from the
sent copy's text layer and asks whether fresh ink sits beside it. Found while
probing Mistral OCR, which rendered every ticked ☐ as ☑ — diffed against the
text layer, that located every tick with no model at all. This does the same
thing locally. Two jobs:

- **It keeps D-09 from dropping a lone tick.** A tick drawn *over* its box
  loses the part that overlaps the printed outline to `HALO`, so a single
  tick scores well under `THRESHOLD`. Site Audit 2026-09-26 p3 carried one
  tick — "apply fix HOST-REBOOT", 72 px, the whole page's fresh ink — and was
  filed `skipped`. `FAINTEST_REAL_INK` was measured on a *pair* of ticks. A
  page with a located tick is now always read.
- **It checks the reader's `completions`** (`reconcile`): a completion on a
  ☐ line with no ink beside it is withheld, and a tick the reader did not
  report is receipted. Neither ever closes an action — on a Site Audit a
  closed action is an approved fix that site-remediate applies that night.

Strokes, not windows. Counting pixels in a window round each box confused a
tick with the top of the next line's tick rising into the window (16 px of
somebody else's ink against a real tick at 15). So the fresh ink is split
into connected strokes and each stroke is given to the box beside its
*lowest* point — a tick's V sits on its own box. Measured over every
returning page with printed boxes (18 aligned pages, 94 boxes): 67 ticks at
15-113 px, 27 unticked boxes at exactly 0. `TICK_MIN` sits between them.
The rule has one known limit: a stroke rising more than a whole line pitch
crosses the printed line above, whose halo cuts it, and the cut-off top can
be credited to that line as well. No geometric rule separates it — a real
tick drawn over its own box is cut by the outline the same way. The error
runs in the harmless direction (a spurious `unread` receipt; the real line
keeps its tick, so nothing true is withheld) and none of the 94 boxes did it.

It only judges a page whose print survived: if under `KEPT_MIN` of the sent
page's printed pixels are still dark on return, the sent copy on disk is not
the one delivered (Site Audit 2026-09-10 was re-rendered after sending: 13.5 %
kept) and the answer is None — no opinion, the reader decides alone.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
import numpy as np

from . import textlayer

log = logging.getLogger("ozu.ink")

# A detector, not a reader: at 110 DPI an A5 page is ~642×910, which is plenty
# to see a tick and a quarter of the pixels of the 200 DPI page the vision
# call gets.
DPI = 110
# Dark enough in the returning page to be a pen stroke.
DARK = 160
# Light enough in the sent page to be bare paper. The gap between DARK and
# LIGHT is deliberate: a pixel in between is ambiguous and counts as neither.
LIGHT = 200
# How far the original's own ink reaches once re-rasterised (measured: 1-2 px).
HALO = 2
# Below this many new dark pixels, the page is clean. See the module docstring
# for where the number comes from; `test_threshold_sits_between_the_measured
# _artefact_and_the_faintest_real_ink` is what holds it.
THRESHOLD = 100
FAINTEST_REAL_INK = 235      # measured: two ✓ on a Ozu Mail cover
LARGEST_ARTEFACT = 9         # measured: one page of pure print, after HALO
# More than a quarter of the page "new" is not handwriting; it is a page that
# did not line up, or a document that was re-typeset. Read it.
MAX_FRACTION = 0.25
# Amazon's page-position footer, as in ingest._page_hash.
KEEP = 0.95
# A returning page with no counterpart in the copy we sent (the document came
# back longer than it left). Always read.
UNKNOWN = -1

_KINDLE_SUFFIX = re.compile(r"\s*-\s*kindle(\s*\(\d+\))?\s*$", re.I)


def sent_copy(notebook: str, dirs) -> Path | None:
    """The document we sent, by the name the notebook carries. `notebook` has
    already been through `ingest._notebook_name`, so it is the stem of the
    file we wrote — the two are the same string by construction."""
    stem = _KINDLE_SUFFIX.sub("", notebook).strip()
    for d in dirs:
        candidate = Path(d).expanduser() / f"{stem}.pdf"
        if candidate.is_file():
            return candidate
    return None


def _gray(page: "fitz.Page") -> np.ndarray:
    pix = page.get_pixmap(dpi=DPI, colorspace=fitz.csGRAY)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width)


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """Grow a mask by `radius` in the four directions. Written out rather than
    imported: scipy is not a dependency and this is four array ORs."""
    out = mask
    for _ in range(radius):
        grown = out.copy()
        grown[1:, :] |= out[:-1, :]
        grown[:-1, :] |= out[1:, :]
        grown[:, 1:] |= out[:, :-1]
        grown[:, :-1] |= out[:, 1:]
        out = grown
    return out


def _denoise(mask: np.ndarray) -> np.ndarray:
    """Drop specks: a pixel survives only with two dark neighbours. A pen
    stroke is a line and keeps all of itself; a stray sensor pixel does not."""
    counts = np.zeros(mask.shape, np.uint8)
    u = mask.astype(np.uint8)
    h, w = u.shape
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                counts[1:-1, 1:-1] += u[1 + dy:h - 1 + dy, 1 + dx:w - 1 + dx]
    return mask & (counts >= 2)


def _fresh(A: np.ndarray, B: np.ndarray) -> np.ndarray:
    """Dark in the return, bare paper in the original (beyond its HALO)."""
    return _denoise((B < DARK) & ~_dilate(A < LIGHT, HALO))


def score_pages(sent: Path, back: Path) -> list[int] | None:
    """New-dark-pixel count per page of the RETURNING document, or None when
    the two cannot be compared and the caller must read every page. Index 0
    is page 1; `UNKNOWN` marks a page with no counterpart to compare against.

    The page counts are allowed to differ. They do in practice — two of the
    nineteen documents in the corpus came back one page shorter than the copy
    on disk — and demanding equality threw away a tenth of the saving for no
    safety gained: a comparison that has slipped out of step shows up as a
    difference across most of the page, which `MAX_FRACTION` already catches
    and turns into "read all of it"."""
    try:
        with fitz.open(sent) as a, fitz.open(back) as b:
            if a.page_count != b.page_count:
                log.info("ink: %s came back with %d pages, we sent %d — "
                         "comparing the %d they share",
                         back.name, b.page_count, a.page_count,
                         min(a.page_count, b.page_count))
            out = [UNKNOWN] * b.page_count
            for n in range(min(a.page_count, b.page_count)):
                A, B = _gray(a[n]), _gray(b[n])
                h = min(A.shape[0], B.shape[0]) * int(KEEP * 100) // 100
                w = min(A.shape[1], B.shape[1])
                if h < 200 or w < 200:
                    return None
                count = int(_fresh(A[:h, :w], B[:h, :w]).sum())
                if count > MAX_FRACTION * h * w:
                    log.info("ink: %s p%d differs over %.0f%% of the page — "
                             "misalignment, not handwriting; reading it all",
                             back.name, n + 1, MAX_FRACTION * 100)
                    return None
                out[n] = count
            return out
    except Exception:
        log.warning("ink: could not compare %s with %s; reading every page",
                    back.name, sent.name, exc_info=True)
        return None


def has_ink(score: int, threshold: int = THRESHOLD) -> bool:
    """UNKNOWN pages read as inked: a page with nothing to compare against is
    a page nobody has looked at, and the only safe answer is to read it."""
    return score == UNKNOWN or score >= threshold


# --- which printed ☐ carries a tick -------------------------------------------

# The printed markers, as layout.BOX / layout.TICKED print them, plus the
# `[ ]` every document before 2026-08-31 carries (invariant 6).
OPEN_MARKERS = ("☐", "[ ]")
DONE_MARKER = "☑"
# Fresh-ink pixels (at DPI) that make a stroke a tick. Measured: faintest real
# tick 15 px (a squiggle on Ozu Mail 2026-09-08 p3), every unticked box 0.
TICK_MIN = 8
FAINTEST_TICK = 15
# Share of the sent page's print still dark on return, below which the pair
# is not the same document. Measured: 1.000 on every aligned page, 0.135 on
# the one re-rendered after sending.
KEPT_MIN = 0.9
PRINT_DARK = 100
# Where a tick's lowest point may sit relative to its marker, in points:
# slightly above the box (the V can land on the outline and lose its bottom
# to HALO), a little below it, and to its left where the stroke starts.
ABOVE, BELOW, LEFT, RIGHT = 3, 4, 14, 8


@dataclass
class Tick:
    """One printed action line and the fresh ink beside its marker."""
    text: str                 # the printed words, marker stripped, wrapped lines joined
    marker: str               # ☐, [ ] or ☑
    rect: tuple               # the marker's box, in PDF points
    pixels: int               # fresh ink attributed to it

    @property
    def open(self) -> bool:
        return self.marker != DONE_MARKER

    @property
    def ticked(self) -> bool:
        return self.pixels >= TICK_MIN


def action_lines(page) -> list[tuple[fitz.Rect, str, str]]:
    """`(marker_rect, marker, text)` for every printed action line on a page
    we sent. An action wraps onto indented lines in the same style; the faint
    lines printed under one — "matched no open action …" under a miss,
    "your ink: …" under a ☑ — are indented too, but set smaller and grey, so
    a continuation must match the action's face, size and colour."""
    rows = textlayer.lines(page)
    out = []
    for i, (bbox, text, spans) in enumerate(rows):
        stripped = text.lstrip()
        marker = next((m for m in (*OPEN_MARKERS, DONE_MARKER)
                       if stripped.startswith(m)), None)
        if marker is None or not spans:
            continue
        rect = fitz.Rect(spans[0]["bbox"])
        if marker == "[ ]":   # a span of "[ ] words": keep the bracket's width
            rect.x1 = rect.x0 + rect.width * 3 / max(1, len(spans[0]["text"]))
        style = lambda sps: next(((sp["font"], sp["size"], sp["color"])
                                  for sp in sps if sp["text"].strip()
                                  and not sp["text"].strip().startswith(
                                      (*OPEN_MARKERS, DONE_MARKER))), None)
        face = style(spans)
        words, prev = [stripped[len(marker):].strip()], fitz.Rect(bbox)
        for bbox2, text2, spans2 in rows[i + 1:]:
            r2 = fitz.Rect(bbox2)
            if (r2.x0 > fitz.Rect(bbox).x0 + 2 and style(spans2) == face
                    and r2.y0 - prev.y1 < 6
                    and not text2.lstrip().startswith((*OPEN_MARKERS, DONE_MARKER))):
                words.append(text2.strip())
                prev = r2
            else:
                break
        out.append((rect, marker, " ".join(w for w in words if w)))
    return out


def _strokes(mask: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    """8-connected components of a sparse mask, as (ys, xs). Written out for
    the same reason as `_dilate`: a returning page has a few hundred fresh
    pixels, and scipy is not a dependency."""
    todo = set(zip(*(a.tolist() for a in np.nonzero(mask))))
    out = []
    while todo:
        stack, ys, xs = [todo.pop()], [], []
        while stack:
            y, x = stack.pop()
            ys.append(y)
            xs.append(x)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    q = (y + dy, x + dx)
                    if q in todo:
                        todo.remove(q)
                        stack.append(q)
        out.append((np.array(ys), np.array(xs)))
    return out


def locate_ticks(sent_page, back_page) -> list[Tick] | None:
    """Every printed action line on `sent_page` and the fresh ink beside it
    on `back_page`. [] when the page prints no marker; None when the two
    pages cannot be compared (then nobody but the reader has an opinion)."""
    markers = action_lines(sent_page)
    if not markers:
        return []
    A, B = _gray(sent_page), _gray(back_page)
    h = min(A.shape[0], B.shape[0]) * int(KEEP * 100) // 100
    w = min(A.shape[1], B.shape[1])
    A, B = A[:h, :w], B[:h, :w]
    printed = A < PRINT_DARK
    if printed.sum() and ((B < LIGHT) & printed).sum() / printed.sum() < KEPT_MIN:
        log.info("ink: sent copy and return do not line up; no tick check")
        return None
    k = w / sent_page.rect.width          # pixels per point
    got = [0] * len(markers)
    for ys, xs in _strokes(_fresh(A, B)):
        bottom, left, right = ys.max() / k, xs.min() / k, xs.max() / k
        for i, (r, _, _) in enumerate(markers):
            if (r.y0 - ABOVE <= bottom <= r.y1 + BELOW
                    and right >= r.x0 - LEFT and left <= r.x1 + RIGHT):
                got[i] += len(ys)
                break
    return [Tick(text, marker, tuple(r), n)
            for (r, marker, text), n in zip(markers, got)]


def locate(sent: Path, back: Path, page_no: int) -> list[Tick] | None:
    """`locate_ticks` for page `page_no` (1-based) of a returning document.
    None on anything unexpected — the check is advice; it never fails a page."""
    try:
        with fitz.open(sent) as a, fitz.open(back) as b:
            if not (0 < page_no <= min(a.page_count, b.page_count)):
                return None
            return locate_ticks(a[page_no - 1], b[page_no - 1])
    except Exception:
        log.warning("ink: tick check failed on %s p%d", back.name, page_no,
                    exc_info=True)
        return None


def _norm(text: str) -> str:
    """Compare without whitespace or hyphens. The print wraps a long action
    wherever the line ends — `wa-` / `gateway.service`, `data/` / `library`
    — so the joined lines carry a space the reader, rightly, does not;
    replayed over the journal, a space-sensitive match reported three ticks
    the reader had read as unread."""
    text = re.sub(r"^\s*(☐|☑|✓|\[ \]|\[x\])\s*", "", text).rstrip(" .…")
    return re.sub(r"[\s\-\u00ad]+", "", text).casefold()


def _same_line(a: str, b: str) -> bool:
    """A completion is the printed words verbatim (annotation prompt), but a
    long line may be printed truncated with "…" — so a shared prefix of a
    real length counts too."""
    a, b = _norm(a), _norm(b)
    return a == b or (min(len(a), len(b)) >= 10 and (a.startswith(b) or b.startswith(a)))


def reconcile(completions: list[str], ticks: list[Tick]
              ) -> tuple[list[str], list[str], list[str]]:
    """Check the reader's completions against the ink.

    Returns `(keep, unseen, unread)`: the completions to process as before;
    those withheld because they name printed ☐ lines with no ink beside any
    of them (read off the print, not the pen — docs/effort-sweep.md); and the
    printed words of ticked ☐ lines no completion names. A completion that
    names no printed ☐ line at all is outside what the ink can judge — a ☑
    line, a hand-written ✓, an older document — and is kept unchanged."""
    keep, unseen = [], []
    for c in completions:
        hits = [t for t in ticks if _same_line(c, t.text)]
        if (hits and all(t.open for t in hits)
                and not any(t.ticked for t in hits)):
            unseen.append(c)
        else:
            keep.append(c)
    unread, seen = [], set()
    for t in ticks:
        if (t.open and t.ticked and _norm(t.text) not in seen
                and not any(_same_line(c, t.text) for c in completions)):
            unread.append(t.text)
            seen.add(_norm(t.text))
    return keep, unseen, unread
