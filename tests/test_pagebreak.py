"""D-12: no text is lost at a page break.

On PyMuPDF 1.28.2 `Story.place()` sometimes lays the block after a page's
last line *below* the rect it was given and counts it as placed; `draw()`
then clips it and the document carries on without it. In real use it cut a
heading and its whole section out of a brief, and nothing complained.

Three kinds of test here, in the spirit of test_textlayer.py — a gate that
cannot fail is not a gate:

  * the bug itself, on bare MuPDF, so these tests keep proving there is
    something to guard against (and tell us when an upgrade fixes it);
  * a 1-pt sweep of the page foot through `Document`, which must keep every
    line at every offset, behind headings, paragraphs, rules and bullets;
  * the backstop: with the reflow switched off, `render()` must refuse to
    write a file that lost text, rather than write it quietly.
"""
from __future__ import annotations

import fitz
import pytest

from ozu import layout, textlayer
from ozu.digest_pdf import build_research_pdf
from ozu.layout import CSS, Document, LostTextError

# The spacer pushes what follows towards the page foot one point at a time.
# 380–520 covers the last ~170 pt of the column, where every loss was found.
OFFSETS = range(380, 521)
AFTER = [f"AFTER{i} body line" for i in range(4)]

LEADS = {
    "h2": "<h2>Lead heading</h2>",
    "h3": "<h3>Lead heading</h3>",
    "p": '<p class="body">Lead paragraph</p>',
    "rule+h2": '<p class="rule"> </p><p class="hr"> </p><h2>Lead heading</h2>',
    "bullet": '<p class="bullet">•  Lead bullet</p>',
}


def _parts(px: int, lead: str) -> list[str]:
    return ([f'<p class="body" style="margin-bottom:{px}px">start</p>', lead]
            + [f'<p class="body">{a}</p>' for a in AFTER]
            + ['<h2>Next heading</h2>', '<p class="body">TAIL line</p>'])


def _text(path) -> str:
    doc = fitz.open(path)
    try:
        return " ".join(textlayer.extract(p) for p in doc)
    finally:
        doc.close()


def _bare_render(parts: list[str], path) -> None:
    """What layout.render did before D-12: place, draw, repeat."""
    where = fitz.Rect(layout.MARGIN_L, layout.MARGIN_T,
                      layout.MARGIN_L + layout.COLUMN_W,
                      layout.PAGE_H - layout.MARGIN_B)
    story = fitz.Story(html="".join(parts), user_css=CSS)
    writer = fitz.DocumentWriter(str(path))
    more = 1
    while more:
        dev = writer.begin_page(fitz.Rect(0, 0, layout.PAGE_W, layout.PAGE_H))
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()


def _lost(text: str) -> list[str]:
    return [w for w in AFTER + ["Next heading", "TAIL line"] if w not in text]


# --- the bug is real ---------------------------------------------------------

@pytest.mark.parametrize("lead", sorted(LEADS))
def test_bare_story_loses_text_somewhere_on_the_page_foot(tmp_path, lead):
    """If this ever fails, MuPDF fixed the bug: D-12 can then be revisited,
    but the guard costs nothing and should stay."""
    losses = []
    for px in OFFSETS:
        _bare_render(_parts(px, LEADS[lead]), tmp_path / "bare.pdf")
        if _lost(_text(tmp_path / "bare.pdf")):
            losses.append(px)
    assert losses, f"bare Story kept every line behind {lead!r}"


# --- Document keeps everything ----------------------------------------------

@pytest.mark.parametrize("lead", sorted(LEADS))
def test_every_line_survives_every_page_foot_offset(tmp_path, lead):
    out = tmp_path / "doc.pdf"
    for px in OFFSETS:
        doc = Document()
        doc._parts.extend(_parts(px, LEADS[lead]))
        doc.render(out)            # raises LostTextError on any loss
        assert not _lost(_text(out)), f"{lead} at {px}pt"


def test_a_real_brief_keeps_every_heading_and_line(tmp_path):
    """The shape that lost a section: many short sections, each a heading, a
    few paragraphs and a bullet list, long enough to put headings at many
    different page-foot positions."""
    report = []
    for n in range(1, 41):
        report.append(f"## Section {n} heading")
        report += [f"Paragraph {n}.{k}: " + "word " * (5 + (n * k) % 23)
                   for k in range(1, 1 + n % 4)]
        report += [f"- bullet {n}.{k}" for k in range(n % 3)]
    out = tmp_path / "Ozu Research brief.pdf"
    build_research_pdf(title="Long brief", source_id="x", topic="t",
                       report="\n".join(report), out_path=out)
    text = _text(out)
    for n in range(1, 41):
        assert f"Section {n} heading" in text
        for k in range(1, 1 + n % 4):
            assert f"Paragraph {n}.{k}:" in text


def test_a_paragraph_split_across_pages_under_a_running_header(tmp_path):
    """The verifier takes the running header off each page before it reads,
    so a paragraph broken by a page turn is not mistaken for lost text."""
    doc = Document()
    doc.para("long " * 900)
    out = doc.render(tmp_path / "split.pdf", page_header="Ozu Build B-000000-0000 r1")
    assert fitz.open(out).page_count > 1


# --- the backstop -----------------------------------------------------------

def test_render_refuses_a_file_that_lost_text(tmp_path, monkeypatch):
    """With the reflow off, the bug comes back; `render` must notice, raise,
    and leave no file behind for a sender to pick up."""
    monkeypatch.setattr(layout, "REFLOW_TRIES", 0)
    out = tmp_path / "lost.pdf"
    for px in OFFSETS:
        doc = Document()
        doc._parts.extend(_parts(px, LEADS["h2"]))
        try:
            doc.render(out)
        except LostTextError as e:
            assert "never reached the page" in str(e)
            assert not out.exists()
            return
    pytest.fail("no offset reproduced the loss with the reflow off")


def test_verifier_reads_entities_and_ligatures_as_printed(tmp_path):
    """`&#x27;` prints as an apostrophe and `fi` may come back as U+FB01:
    neither is lost text."""
    doc = Document()
    doc.para("The owner's office files: Confirm the official fee — €40")
    doc.render(tmp_path / "chars.pdf")


def test_a_running_header_that_repeats_a_title_is_not_lost_text(tmp_path):
    """A document may stamp its title as the running header and print the
    same words in its band. The check reads below the stamp by position, so
    the band's copy is matched as itself, not mistaken for missing text."""
    doc = Document()
    doc.head("Ozu Report", "Weekly report — 5 October 2026", "every project")
    doc.markdown("\n".join(f"Line {i} of the report" for i in range(80)))
    doc.render(tmp_path / "report.pdf", page_header="Ozu Report")
