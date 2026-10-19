"""The one place a PDF text layer is read.

The text layer is a contract, not a byproduct (D-01).
`understand._build_annotation_prompt()` hands it to the vision model as the
*printed-side* reference for tick-matching, so whatever this module returns is
what the model compares the ink against. Every reader of a text layer goes
through `extract()`, so there is exactly one definition of "what the printed
side says".

Why the fold lives here and not in the renderer (D-05): the shaper decides
ligatures, not us. A PDF typeset in a real book face emits U+FB01 for "fi" —
`Confirm the official fee` comes back out of the text layer as
`Conﬁrm the oﬃcial fee`, which no longer matches the action text it is
supposed to be a reference for. Nothing was lost; the characters are in a
presentation form. Undoing that is the reader's job, done once, here — a
renderer that folded at generation time would be arguing with its own shaper.

**The fold is deliberately not `unicodedata.normalize("NFKC", ...)`.** That is
the obvious implementation and it is wrong. Measured on this box against the
fixture corpus, NFKC folds the ligatures — and also rewrites:

    ② U+2461 → "2"      ③ U+2462 → "3"      ④ U+2463 → "4"
    U+00A0 no-break space → " "

Those circled digits are precisely the characters whose loss started this
work: a delivered brief argued in numbered points and referred back to them,
and base-14 Helvetica collapsed ②, ③ and ④ into the same anonymous "·". A fold
to bare digits would re-flatten the distinction the fix exists to protect,
only less visibly — and rewriting U+00A0 silently edits the author's
whitespace. `test_fold_leaves_alone_what_the_loss_was_about` pins this.

So: fold the Latin presentation-forms block and nothing else. Those seven
codepoints carry no authorial intent — nobody types U+FB03 — so folding them
cannot destroy information. Every other character is returned as written.
"""
from __future__ import annotations

# U+FB00–U+FB06, the Latin ligature presentation forms. U+FB05 (ſt, long s)
# folds to "st" along with U+FB06: the long s is itself a presentation form.
_LIGATURES = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "st",
    "ﬆ": "st",
}

_FOLD = str.maketrans(_LIGATURES)


def fold(text: str) -> str:
    """Undo shaper ligatures. Every other character is left exactly as written."""
    return (text or "").translate(_FOLD)


def extract(page, clip=None) -> str:
    """Read a page's text layer. The single consumption boundary — call this,
    never `page.get_text()` directly, or a ligature reaches the model as a
    mismatch against the very line it is meant to reference. `clip` limits
    the read to a rectangle (layout's text check reads below the running
    header)."""
    return fold(page.get_text(clip=clip))


def lines(page) -> list[tuple[tuple[float, float, float, float], str, list[dict]]]:
    """The same text layer as `extract()`, one entry per printed line with
    where it sits: `(bbox, text, spans)`, text folded exactly as `extract()`
    folds it. `ink.locate_ticks` needs positions — which printed ☐ a stroke
    sits beside — and a second, unfolded reading of the layer is how a
    ligature would come back in by the side door. Each span keeps its own
    `bbox`, `font`, `size`, `color` and folded `text`."""
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [{"bbox": tuple(s["bbox"]), "font": s["font"],
                      "size": round(s["size"], 1), "color": s["color"],
                      "text": fold(s["text"])} for s in line["spans"]]
            out.append((tuple(line["bbox"]), "".join(s["text"] for s in spans),
                        spans))
    return out
