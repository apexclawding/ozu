"""The D-01 gate: what goes into a delivered document comes back out of its
text layer, because the vision model reads that text layer as the printed-side
reference for tick-matching.

Three kinds of test here:

  * `fold()` tests — they pin the *shape* of the fold, in particular that it is
    not NFKC.
  * corpus round-trip tests — the two D-01 gates, `chars` and `intact`, over
    every research case. These were written while they still failed, as strict
    xfails, and the Story renderer turned them green; `strict` is what forced
    the markers to be removed rather than quietly outliving the bug.
  * printed-marker tests — the `☐` an ink tick is matched against, and the
    `[ ]` that older delivered documents still carry.

Why they were written first: the existing PDF assertion stayed green straight
through the live glyph loss, because it asserted on "Check competitor pricing",
which contains no `fi`, `fl` or `€`. A test that cannot fail is not a gate.
"""
from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import corpus  # noqa: E402
from ozu import textlayer  # noqa: E402
from ozu.digest_pdf import (build_research_pdf, build_learn_pdf,  # noqa: E402
                            build_pdf, build_mail_pdf,
                            build_propose_pdf, build_result_pdf,
                            build_edition_pdf)


# --- the fold itself ---------------------------------------------------------

def test_fold_undoes_every_latin_ligature():
    assert textlayer.fold("Conﬁrm the oﬃcial fee") == "Confirm the official fee"
    assert textlayer.fold("aﬄuent oﬃce waﬄe ﬂow oﬀ") == "affluent office waffle flow off"
    # the whole U+FB00–U+FB06 block, so a face that shapes ﬅ/ﬆ is covered too
    for lig in "ﬀﬁﬂﬃﬄﬅﬆ":
        assert lig not in textlayer.fold(f"x{lig}y"), f"U+{ord(lig):04X} survived"


def test_fold_leaves_alone_what_the_loss_was_about():
    """The NFKC guard. NFKC would fold the ligatures *and* flatten ② ③ ④ to
    bare digits — re-erasing the distinction that started this work, since the
    delivered brief argued in numbered points and referred back to them."""
    keep = "€ ② ③ ④ — → ✓ ô ä ≤ … "
    assert textlayer.fold(keep) == keep

    # and prove the alternative really would have broken it, so this test
    # documents the decision rather than merely asserting the current code
    damaged = [c for c in keep
               if unicodedata.normalize("NFKC", c) != c and c not in "ﬀﬁﬂﬃﬄﬅﬆ"]
    assert "②" in damaged and "③" in damaged and "④" in damaged, (
        "NFKC no longer flattens circled digits — re-check whether the "
        "narrow fold is still the right call")


def test_fold_handles_empty_and_none():
    assert textlayer.fold("") == ""
    assert textlayer.fold(None) == ""


def test_extract_folds_what_the_shaper_actually_emitted(tmp_path):
    """Not a synthetic ligature — one produced by a real shaper. This is the
    regression Stage 2 would otherwise introduce: Story emits U+FB01/U+FB03
    for ordinary English words."""
    story = fitz.Story(html="<p>Confirm the official fee</p>",
                       user_css="* { font-family: Charis SIL; font-size: 11px; }")
    out = tmp_path / "shaped.pdf"
    writer = fitz.DocumentWriter(str(out))
    more = 1
    while more:
        dev = writer.begin_page(fitz.Rect(0, 0, 420, 595))
        more, _ = story.place(fitz.Rect(42, 42, 378, 553))
        story.draw(dev)
        writer.end_page()
    writer.close()

    with fitz.open(out) as doc:
        page = doc[0]
        raw = page.get_text()
        folded = textlayer.extract(page)

    assert "ﬁ" in raw, "shaper stopped emitting ligatures; this test is now blind"
    assert "Confirm the official fee" in folded
    assert not any(0xFB00 <= ord(c) <= 0xFB06 for c in folded)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A throwaway OZU_HOME. Mirrors test_pipeline's fixture; no config from
    ~/ozu-data and no secrets are read."""
    monkeypatch.setenv("OZU_HOME", str(tmp_path / "ozu"))
    monkeypatch.setenv("OZU_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("OZU_WORK_VAULT_DIR", str(tmp_path / "work-vault"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    import importlib
    import ozu.config as config
    config._config = None
    for m in ("ozu.db", "ozu.ingest"):
        if m in sys.modules:
            importlib.reload(sys.modules[m])
    return config.get_config()


def test_ingest_reads_the_text_layer_through_the_fold(env):
    """End-to-end wiring, not a stub: a real shaped PDF lands in raw/, and the
    ocr_text the ledger stores — which is what reaches the vision model — must
    already be folded. Catches `ingest` reverting to a bare `page.get_text()`."""
    from ozu import ingest, db

    story = fitz.Story(html="<p>Confirm the official fee before the filing</p>",
                       user_css="* { font-family: Charis SIL; font-size: 11px; }")
    out = env.raw_dir / "Inbox" / "shaped.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    writer = fitz.DocumentWriter(str(out))
    more = 1
    while more:
        dev = writer.begin_page(fitz.Rect(0, 0, 420, 595))
        more, _ = story.place(fitz.Rect(42, 42, 378, 553))
        story.draw(dev)
        writer.end_page()
    writer.close()

    with fitz.open(out) as doc:
        assert "ﬁ" in doc[0].get_text(), "fixture is not actually shaped"

    ingest.ingest()
    con = db.connect(env.db_path)
    row = con.execute("SELECT ocr_text FROM pages ORDER BY id DESC LIMIT 1").fetchone()
    assert "Confirm the official fee" in row["ocr_text"]
    assert not any(0xFB00 <= ord(c) <= 0xFB06 for c in row["ocr_text"])


# --- the corpus round trip ---------------------------------------------------

RESEARCH_CASES = [n for n, c in corpus.sample().items() if c["kind"] == "research"]

# Every case, both gates, green. Until the Story renderer landed these were
# strict xfails naming which case failed which gate; they are gone because the
# renderer fixed them, which is what `strict` was there to force.
MD_MARKERS = ("#", "- ", "* ")


def _content(markdown: str) -> str:
    """The source text minus the Markdown syntax the renderer is allowed to
    consume. `## Brief` is typeset as a heading reading `Brief`, so the `##`
    not arriving is correct behaviour, not a dropped character.

    Written independently of `layout.inline()` on purpose: this is the
    *specification* of what may disappear. If the renderer ever starts eating
    something not listed here, gate 1 must fail — which it cannot do if both
    sides call the same function."""
    out = []
    for line in markdown.splitlines():
        s = line.strip()
        if re.fullmatch(r"([-*_])\1{2,}", s):      # a horizontal rule
            continue
        if s.startswith("#"):
            s = s.lstrip("#").strip()
        elif s.startswith(("- ", "* ")):
            s = s[2:]
        s = re.sub(r"\*\*(\S(?:.*?\S)?)\*\*", r"\1", s)           # bold
        s = re.sub(r"(?<![*\w])\*(\S(?:.*?\S)?)\*(?![*\w])", r"\1", s)  # italic
        out.append(s)
    return "\n".join(out)


def _no_separators(text: str) -> str:
    """Drop the ` · ` the header, meta and bullet lines legitimately print,
    so what is left is only the substitution signature of a missing glyph.

    Matched across whitespace rather than as a literal " · ": a separator
    that falls at a line break arrives from the text layer as " ·\n", and a
    literal `.replace(" · ", "")` calls that a lost character. Found on the
    research cover, whose one-line label wrapped exactly there —
    `tools/render_sample.py` had already learned this and the test had not."""
    return re.sub(r"\s·\s", "", text, flags=re.S)


def _render(case, tmp_path) -> str:
    out = tmp_path / "case.pdf"
    build_research_pdf(out_path=out, **case["payload"])
    with fitz.open(out) as doc:
        return "\n".join(textlayer.extract(p) for p in doc)


@pytest.mark.parametrize("name", RESEARCH_CASES)
def test_roundtrip_chars(name, tmp_path):
    """Gate 1 — nothing dropped or substituted. This is the data-loss gate,
    and the one the 14 measured losses failed."""
    case = corpus.sample()[name]
    got = _render(case, tmp_path)
    report = textlayer.fold(case["payload"]["report"])

    lost = sorted({c for c in report if ord(c) > 127} - set(got))
    assert not lost, f"characters never arrived: {' '.join(lost)}"
    assert "·" not in _no_separators(got), (
        "a bare '·' in the body is the substitution signature of a font "
        "that lacks the glyph")
    assert "".join(_content(report).split()) in "".join(got.split())


def _reconstruct(token: str, text: str):
    """Match `token` against `text` allowing line breaks anywhere inside it.

    Returns `(found, mid_word_breaks)`. A break is "mid-word" when it falls
    between two alphanumeric characters — `for-l | ess`. A break after a
    hyphen or a slash — `improving-energy- | performance` — is an ordinary
    typographic break opportunity that any book or browser would take, and
    rejoining the lines reproduces the token exactly."""
    for start in range(len(text)):
        i, j, mid = start, 0, []
        while j < len(token) and i < len(text):
            if text[i] == "\n":
                if 0 < j and token[j - 1].isalnum() and token[j].isalnum():
                    mid.append(f"{token[max(0, j - 10):j]}|{token[j:j + 10]}")
                i += 1
                continue
            if text[i] != token[j]:
                break
            i += 1
            j += 1
        if j == len(token):
            return True, mid
    return False, None


@pytest.mark.parametrize("name", RESEARCH_CASES)
def test_roundtrip_intact(name, tmp_path):
    """Gate 2 — a long token may wrap, but only where wrapping is lossless.

    Separate from gate 1 on purpose: `url-300` is pure ASCII and loses no
    character, so a whitespace-insensitive comparison passes it however badly
    it is broken.

    The gate is *not* "the token appears contiguously" — that is unsatisfiable
    and always was. A 300-character URL does not fit on an A5 line at any
    readable size, so every renderer must break it somewhere; the old gate
    could only ever have been passed by clipping the URL, which is worse. What
    distinguishes the renderers is *where*. Measured on this box: `_Writer`
    broke at whatever character the width ran out on, mid-word
    (`...for-l` / `ess-than...`), so you cannot tell whether a hyphen belongs
    at the seam. Story breaks only at hyphens, and rejoining the lines returns
    the URL character-for-character."""
    case = corpus.sample()[name]
    got = _render(case, tmp_path)
    report = case["payload"]["report"]

    long_tokens = [textlayer.fold(w) for w in report.split() if len(w) > 60]
    for token in long_tokens:
        found, mid = _reconstruct(token, got)
        assert found, f"token not reconstructible from the text layer: {token[:60]}…"
        assert not mid, f"broken mid-word (unrecoverable seam): {mid}"



# --- the learning sheet, same two gates ---------------------------------------

LEARN_CASES = [n for n, c in corpus.sample().items() if c["kind"] == "learn"]


def _learn_source(payload: dict) -> str:
    bits = []
    for item in payload.get("new") or []:
        bits += [item["title"], item["brief"]]
    for d in payload.get("due") or []:
        bits += [d["title"], d.get("cue", ""), d.get("question", "")]
    for s_ in payload.get("skipped") or []:
        bits += [s_["title"], s_.get("reason", "")]
    return "\n".join(b for b in bits if b)


@pytest.mark.parametrize("name", LEARN_CASES)
def test_learn_sheet_roundtrip(name, tmp_path):
    """The "Ozu Learn" sheet is a fifth document type and gets both gates.
    Two things are specific to it: the ruled writing lines may add nothing
    but whitespace to the text layer, and no `☐` may appear — the sheet is
    not allowed to print anything a tick could close (invariant 5)."""
    from ozu import layout
    case = corpus.sample()[name]
    out = tmp_path / "learn.pdf"
    build_learn_pdf(case["payload"], out)
    with fitz.open(out) as doc:
        got = "\n".join(textlayer.extract(p) for p in doc)
    src = textlayer.fold(_learn_source(case["payload"]))

    lost = sorted({c for c in src if ord(c) > 127} - set(got))
    assert not lost, f"characters never arrived: {' '.join(lost)}"
    assert "·" not in _no_separators(got)
    # the renderer prefixes bullets with "•" — furniture that is allowed to
    # *appear*; what the gate forbids is anything from the brief disappearing
    for item in case["payload"]["new"]:
        assert ("".join(_content(item["brief"]).split())
                in "".join(got.replace("•", "").split()))
    for token in [textlayer.fold(w) for w in src.split() if len(w) > 60]:
        found, mid = _reconstruct(token, got)
        assert found and not mid, token[:60]

    assert layout.BOX not in got and layout.TICKED not in got
    assert "In your own words" in got and "Due again" in got


def test_ruled_lines_add_only_whitespace_to_the_text_layer(tmp_path):
    from ozu import layout
    doc = layout.Document()
    doc.para("before")
    doc.rules(3)
    doc.para("after")
    out = tmp_path / "r.pdf"
    doc.render(out)
    with fitz.open(out) as d:
        text = "\n".join(textlayer.extract(p) for p in d)
        drawings = d[0].get_drawings()
    assert "".join(text.split()) == "beforeafter"
    assert len(drawings) >= 3, "the rules were not drawn"


def test_the_intact_gate_would_have_caught_the_old_renderer():
    """Guards the guard. The gate was loosened from "contiguous" to
    "losslessly rejoinable", so it has to be shown that the loosened version
    still fails the layout it was written to catch — otherwise this is just a
    test that was weakened until it passed."""
    old_output = ("https://example.com/improving-energy-performance/claim-the-insulation\n"
                  "-grant/can-i-claim-it-if-the-house-was-built-before-the-w\n"
                  "ar-years")
    token = old_output.replace("\n", "")
    found, mid = _reconstruct(token, old_output)
    assert found
    assert mid, "the gate no longer rejects _Writer's mid-word split"


def test_the_glyph_case_still_carries_every_hostile_character():
    """Guards the guard: if the fixture stops containing the characters the
    survey measured as lost, the round-trip tests above go quietly blind."""
    report = corpus.sample()["glyphs"]["payload"]["report"]
    for ch in "€②③④—→✓ôä":
        assert ch in report, f"{ch} fell out of the glyph fixture"


# --- subsetting --------------------------------------------------------------

def test_every_document_ships_subsetted(tmp_path):
    """`Document.render` subsets. Two things worth asserting, because
    subsetting is the one step that could drop a glyph *after* the round-trip
    otherwise looked fine: it actually ran, and nothing fell out of the text
    layer when it did."""
    case = corpus.sample()["glyphs"]
    out = tmp_path / "g.pdf"
    build_research_pdf(out_path=out, **case["payload"])

    with fitz.open(out) as doc:
        names = [f[3] for page in doc for f in page.get_fonts(full=True)]
        text = "\n".join(textlayer.extract(p) for p in doc)

    assert names, "no embedded font at all — the base-14 fallback is back"
    # a subset font is renamed "ABCDEF+Family" by the subsetter
    assert all("+" in n for n in names), f"not subsetted: {names}"
    for ch in "€②③④—→✓ôä":
        assert ch in text, f"{ch} did not survive subsetting"


def test_the_renderer_embeds_a_real_face_not_base_14(tmp_path):
    """The glyph loss was base-14 Helvetica substituting `·`. Assert the cause
    is gone, not just the symptom — a future change that reverts to a
    non-embedded font would otherwise only show up as characters going
    missing again."""
    case = corpus.sample()["brief-900w"]
    out = tmp_path / "b.pdf"
    build_research_pdf(out_path=out, **case["payload"])
    with fitz.open(out) as doc:
        names = {f[3].split("+")[-1] for page in doc
                 for f in page.get_fonts(full=True)}
    assert any("Charis" in n for n in names), names
    assert not any(n.startswith("Helvetica") for n in names), names


# --- the printed marker contract --------------------------------------------

def test_action_lines_print_a_box_that_survives_extraction(tmp_path):
    """D-04: a checkbox must be a *character*, never a drawn rectangle. A
    vector box looks identical on the page and is invisible to the annotation
    prompt, which would leave every tick unmatchable."""
    from ozu import layout
    doc = layout.Document()
    doc.action("Confirm the official fee")
    doc.action("Already handled", done=True)
    out = tmp_path / "a.pdf"
    doc.render(out)
    with fitz.open(out) as d:
        text = "\n".join(textlayer.extract(p) for p in d)
    assert f"{layout.BOX} Confirm the official fee" in text
    assert f"{layout.TICKED} Already handled" in text


def test_annotation_prompt_still_matches_documents_already_delivered():
    """The printed marker changed from `[ ]` to `☐` on 2026-08-31. Every
    document sent before that date is still on the device and can come back
    annotated at any time, so the prompt has to recognise both. Dropping the
    old marker would silently stop closing actions on older pages — a failure
    that produces no error, just ticks that do nothing."""
    from ozu import understand
    text = understand.ANNOTATION_PROMPT_PATH.read_text()
    assert "☐" in text, "new marker missing from the annotation prompt"
    assert "[ ]" in text, "legacy marker dropped; older documents stop closing"


def _band_fills(page) -> list:
    """Fills in the band's own grey. Filtering on colour matters: `h2`,
    `.hr`, `.rule` and `.footer` borders are emitted as fills too, so a bare
    "any fill" check would call a section rule on page 2 a stray band."""
    from ozu.layout import BAND_RGB
    out = []
    for g in page.get_drawings():
        fill = g.get("fill")
        if fill and max(abs(a - b) for a, b in zip(fill, BAND_RGB)) < 0.01:
            out.append(g)
    return out


def test_the_header_band_is_painted_once_and_never_repeats(tmp_path):
    """The D-05 gate. MuPDF's Story repaints the last `background-color` it
    saw as a one-line stub at the top of every subsequent page, so a styled
    header band smeared grey across the top of pages 2, 3, 4… of every
    multi-page document this pipeline delivered. The fix is that the band
    carries no fill in the stylesheet at all — `render()` draws it, on the
    page `Story.element_positions` says it landed on.

    Both halves are asserted, because either alone passes for the wrong
    reason: a band that stopped being drawn would satisfy "no stub", and a
    band still styled in CSS would satisfy "page 1 has a band"."""
    from ozu.layout import BAND_RGB, Document

    doc = Document()
    doc.head("kicker", "A title", "meta")
    for i in range(90):
        doc.para(f"paragraph {i} lorem ipsum dolor sit amet consectetur")
    out = doc.render(tmp_path / "band.pdf")

    with fitz.open(out) as pdf:
        assert pdf.page_count > 1, "the fixture must turn a page to test this"
        first = _band_fills(pdf[0])
        assert len(first) == 1, f"expected exactly one band, got {first}"
        assert first[0]["fill"] == pytest.approx(BAND_RGB, abs=0.01)
        # the painted box must sit at the top of the page, over the header
        assert first[0]["rect"].y0 < 60, first[0]["rect"]
        for n, page in enumerate(pdf):
            if n == 0:
                continue
            assert not _band_fills(page), f"page {n + 1} carries a stray band"


def test_the_stylesheet_carries_no_background_colour(tmp_path):
    """The other half of D-05, stated where a reader will look for it: any
    `background-color` in the CSS comes back as a per-page stub. If a future
    change needs a filled element, it has to be painted in `render()` too."""
    from ozu.layout import CSS

    # Comments stripped first: the stylesheet explains the rule in prose, and
    # matching that prose would make this test pass by talking about itself.
    declarations = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    assert "background-color" not in declarations


# --- the cover (D-06) --------------------------------------------------------

# Every document builder, keyed as the corpus keys its cases. Kept here rather
# than imported from `tools/render_sample.py`: the harness is a tool, and a
# test that depends on a tool stops running the day the tool is rewritten.
_BUILDERS = {
    "research": lambda p, out: build_research_pdf(out_path=out, **p),
    "learn": lambda p, out: build_learn_pdf(p, out),
    "digest": lambda p, out: build_pdf(p, out),
    "mail": lambda p, out: build_mail_pdf(p, out),
    "build-propose": lambda p, out: build_propose_pdf(p, out),
    "build-result": lambda p, out: build_result_pdf(p, out),
    "edition": lambda p, out: build_edition_pdf(p, out),
}
COVER_CASES = sorted(corpus.sample())


def _cover_fills(page):
    """The cover block's own fill. Filtered on colour for the same reason
    `_band_fills` is: the type mark is drawn on this page too, and it is
    filled black — a bare "any fill" check would count a mountain."""
    from ozu import layout
    blocks = [layout.COVER_RGB] + [layout._rgb(t) for t in layout.COVER_TINTS.values()]
    out = []
    for g in page.get_drawings():
        fill = g.get("fill")
        if fill and any(max(abs(a - b) for a, b in zip(fill, rgb)) < 0.01
                        for rgb in blocks):
            out.append(g)
    return out


@pytest.mark.parametrize("name", COVER_CASES)
def test_every_document_opens_with_a_cover(name, tmp_path, env):
    """D-06. The cover is the page the Scribe's library shows as a thumbnail,
    so what is asserted is what survives being 120 pixels wide: a dark block
    that reaches the top edge and both sides of the sheet, the day and the
    month in it, and a drawn type mark below.

    The block is asserted by its *fill* and not by its text, because the type
    in it is white: a cover that lost the fill would still extract perfectly
    and be a blank sheet on the device."""
    case = corpus.sample()[name]        # `env`: the mail and digest builders
    out = tmp_path / f"{name}.pdf"      # read config, and must not read ~/ozu-data
    _BUILDERS[case["kind"]](case["payload"], out)

    with fitz.open(out) as doc:
        assert doc.page_count >= 2, "the cover must not swallow the document"
        page = doc[0]
        text = textlayer.extract(page)
        blocks = [g for g in _cover_fills(page) if g["rect"].width > 300]
        drawings = len(page.get_drawings())

    assert len(blocks) == 1, f"expected one cover block, got {blocks}"
    rect = blocks[0]["rect"]
    assert rect.y0 <= 0.5 and rect.x0 <= 0.5 and rect.x1 >= 419.5, (
        f"the block must bleed to the trim on three sides: {rect}")
    assert 120 < rect.y1 < 320, f"block height out of proportion: {rect}"
    assert drawings > len(blocks), "no type mark was drawn on the cover"
    # the date, as the library grid will show it
    assert re.search(r"\n\d{2}\n[A-Z]{3,}", text), repr(text)


@pytest.mark.parametrize("name", COVER_CASES)
def test_the_cover_prints_nothing_a_tick_could_close(name, tmp_path, env):
    """Invariant 5 on the one page that is pure furniture. Two halves: no
    printed marker in the text layer, and no *drawn* empty rectangle either —
    the type marks are silhouettes, and an outlined square among them would
    be a checkbox to a reader who has only the image (D-04)."""
    from ozu import layout
    case = corpus.sample()[name]
    out = tmp_path / f"{name}.pdf"
    _BUILDERS[case["kind"]](case["payload"], out)

    with fitz.open(out) as doc:
        text = textlayer.extract(doc[0])
        stroked = [g for g in doc[0].get_drawings() if not g.get("fill")]

    assert layout.BOX not in text and layout.TICKED not in text
    assert "[ ]" not in text and "[x]" not in text.lower()
    for g in stroked:
        assert not any(i[0] == "re" for i in g["items"]), (
            f"an unfilled rectangle on a cover reads as a checkbox: {g}")


def test_the_cover_type_is_reversed_out_of_the_block(tmp_path):
    """The other half of the fill assertion, stated from the type's side: the
    date is white, so it is legible only where the block was painted. If a
    future change moves the fill back into the stylesheet (D-05 forbids it,
    and the paint would then land on page 2 as well) this is what fails."""
    from ozu import layout
    doc = layout.Document()
    doc.cover("digest", date="2026-09-05", kicker="Ozu",
              wordmark="WEEKLY DIGEST", line="12 new notes")
    doc.para("body")
    out = doc.render(tmp_path / "c.pdf")

    with fitz.open(out) as pdf:
        page = pdf[0]
        white = [s for b in page.get_text("dict")["blocks"]
                 for line in b.get("lines", []) for s in line["spans"]
                 if s["color"] == 0xFFFFFF]
        block = _cover_fills(page)[0]["rect"]
        # and nothing of the sort on the body page
        assert not _cover_fills(pdf[1]), "the cover block repeated on page 2"

    assert {"05", "SEPTEMBER"} <= {s["text"] for s in white}, white
    for span in white:
        assert fitz.Rect(span["bbox"]) in block, (
            f"white type outside the painted block: {span['text']}")


def test_a_build_cover_still_prints_its_id_where_it_can_be_read(tmp_path):
    """The annotation prompt tells the model that every page of a Build
    document prints its id, and routes the ink by it. The cover is now the
    first page a returning document offers, so the running stamp has to be on
    it *and* be visible — furniture grey on the block extracts perfectly and
    shows nothing. Since D-08 the Build block is a teal tint, so the stamp
    is ink there rather than paper."""
    case = corpus.sample()["build-propose"]
    out = tmp_path / "b.pdf"
    build_propose_pdf(case["payload"], out)
    bid = case["payload"]["id"]

    with fitz.open(out) as doc:
        for page in doc:
            assert bid in textlayer.extract(page), f"page {page.number + 1}"
        stamp = [s for b in doc[0].get_text("dict")["blocks"]
                 for line in b.get("lines", []) for s in line["spans"]
                 if bid in s["text"]]
    # ink on the teal block (D-08): the colour that contrasts with the fill,
    # never the furniture grey the body pages use
    from ozu import layout
    assert stamp and all(s["color"] == int(layout.INK[1:], 16) for s in stamp), stamp


def test_cover_dates_survive_every_shape_the_payloads_carry(tmp_path):
    """`date` is read straight off payloads written for other purposes: the
    digest carries an ISO day, Ozu Mail stamps the hour onto it, and a caller
    may pass a real date. Junk prints no numeral rather than a wrong one."""
    from ozu.layout import _as_date
    import datetime as dt

    assert _as_date("2026-09-05") == dt.date(2026, 9, 5)
    assert _as_date("2026-08-30 12:36") == dt.date(2026, 8, 30)
    assert _as_date(dt.date(2026, 9, 5)) == dt.date(2026, 9, 5)
    assert _as_date(dt.datetime(2026, 9, 5, 7, 0)) == dt.date(2026, 9, 5)
    assert _as_date("Saturday 5 September 2026") is None
    assert _as_date(None) is None


def test_an_unknown_mark_is_refused_rather_than_drawn_blank(tmp_path):
    """A typo in a builder must not ship a cover with an empty corner."""
    from ozu import layout
    with pytest.raises(ValueError):
        layout.Document().cover("newsletter", date="2026-09-05")


def test_no_cover_prints_a_build_directive(tmp_path, env):
    """The words `@approved`, `@input` and `@drop` are printed in exactly one
    place — the decision box on a proposal's body page, where the annotation
    prompt is told to expect them and to ignore them unless they are
    handwritten. The cover is the emptiest page in the document and therefore
    the one most likely to be written on; a printed directive there asks the
    vision pass to tell print from ink on the page where it has the least
    context, and printed-vs-handwritten is the first distinction it loses
    (invariant 5b)."""
    for name, case in corpus.sample().items():
        out = tmp_path / f"{name}.pdf"
        _BUILDERS[case["kind"]](case["payload"], out)
        with fitz.open(out) as doc:
            cover = textlayer.extract(doc[0]).lower()
        for tag in ("@approved", "@input", "@drop"):
            assert tag not in cover, f"{name} prints {tag} on its cover"


def test_no_cover_tint_is_red_and_every_tinted_cover_is_painted(tmp_path):
    """D-08. Red is the action ink and the cover is read by the annotation
    pass, so no tint may lead with its red channel. A tint is bright — the
    type on it is ink, and Kaleido drags dark colour to brown-grey — and it
    must actually be what `render()` paints, or a wrong palette entry would
    ship as an ink-black cover and nobody would notice. The type on a tinted
    block is ink, and on the ink block paper: both are asserted by colour."""
    from ozu import layout
    ink = int(layout.INK[1:], 16)
    for mark, hexcolor in layout.COVER_TINTS.items():
        r, g, b = layout._rgb(hexcolor)
        assert r <= max(g, b) + 0.35 and not (r > 0.6 and max(g, b) < 0.4), (
            f"{mark} tint {hexcolor} reads as red")
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        assert lum > 0.35, f"{mark} tint {hexcolor} too dark for ink type"
        doc = layout.Document()
        doc.cover(mark, date="2026-09-12", kicker="Ozu", wordmark="X")
        doc.para("body")
        out = doc.render(tmp_path / f"{mark}.pdf", page_header="Ozu Build B-260912-0000 r1")
        with fitz.open(out) as pdf:
            fills = [d["fill"] for d in pdf[0].get_drawings()
                     if d.get("fill") and d["rect"].width > 300]
            spans = [s for bl in pdf[0].get_text("dict")["blocks"]
                     for line in bl.get("lines", []) for s in line["spans"]]
        assert fills and max(abs(a - b) for a, b in zip(fills[0], (r, g, b))) < 0.01
        day = [s for s in spans if s["text"] == "12"]
        stamp = [s for s in spans if "B-260912-0000" in s["text"]]
        assert day and all(s["color"] == ink for s in day), "type on a tint is ink"
        assert stamp and all(s["color"] == ink for s in stamp), "the id stamp too"


def test_emoji_no_font_draws_are_dropped_not_fatal(tmp_path):
    """2026-10-19/20: a Learn title with 📱💡 made the D-12 gate refuse the
    whole evening edition, two nights running. What no font draws is left
    out before typesetting; what the fonts do draw — the action marker and
    the tick above all — is never touched."""
    from ozu import layout
    doc = layout.Document()
    doc.head("Learn", "Phone pricing 📱💡#shorts")
    doc.para("☐ Call the bank ✓ ② ✅ — café €12 中文 🚀")
    out = doc.render(tmp_path / "emoji.pdf")
    pdf = fitz.open(out)
    text = "\n".join(textlayer.extract(p) for p in pdf)
    assert "Phone pricing #shorts" in text
    assert "📱" not in text and "🚀" not in text
    for kept in ("☐", "✓", "②", "✅", "café", "€12", "中文"):
        assert kept in text, kept
    assert layout.drawable("☐") and not layout.drawable("💡")
