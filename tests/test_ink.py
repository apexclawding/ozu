"""Ink detection (D-09): a returning page with no handwriting is never read.

The money test is `test_a_clean_return_costs_no_vision_call`; the safety test
is everything else. A false positive costs $0.065 and a false negative loses
the owner's ink, so every uncertainty here must resolve to "read it".

The numbers this file asserts against are measurements, not preferences —
`src/ozu/ink.py` records where each came from and
`test_threshold_sits_between_the_measured_artefact_and_the_faintest_real_ink`
is what stops them drifting apart.
"""
import sys
from pathlib import Path

import fitz
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from test_pipeline import env, make_pdf, MOCK_RESULT  # noqa: F401 — shared fixture

from ozu import ink


# --- building a document and its annotated return ---------------------------

def sent_document(path: Path, pages: int = 3) -> Path:
    """Stand-in for a Ozu document: printed text, no ink."""
    doc = fitz.open()
    for n in range(pages):
        page = doc.new_page(width=420, height=595)
        for line in range(20):
            page.insert_text((42, 60 + line * 14),
                             f"printed line {line} of page {n + 1} — official",
                             fontsize=9.5)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


def annotate(sent: Path, back: Path, marks: dict[int, str]) -> Path:
    """The same document with ink drawn on some of its pages. `marks` maps a
    1-based page number to 'tick' (the faintest real mark there is) or
    'scrawl' (a margin note)."""
    doc = fitz.open(sent)
    for page_no, kind in marks.items():
        page = doc[page_no - 1]
        shape = page.new_shape()
        if kind == "tick":
            # a ✓ about the size of a real one: two strokes, ~20pt tall
            shape.draw_line(fitz.Point(300, 300), fitz.Point(308, 312))
            shape.draw_line(fitz.Point(308, 312), fitz.Point(324, 288))
            shape.finish(color=(0, 0, 0), width=2.2, closePath=False)
        else:
            for i in range(6):
                shape.draw_line(fitz.Point(300, 260 + i * 12),
                                fitz.Point(390, 268 + i * 12))
            shape.finish(color=(0, 0, 0), width=2.0, closePath=False)
        shape.commit()
    back.parent.mkdir(parents=True, exist_ok=True)
    doc.save(back)
    doc.close()
    return back


def returning(env, name: str, marks: dict[int, str], pages: int = 3) -> Path:
    """A document this pipeline sent, come back from the Scribe with `marks`
    on it — written to the two places the real ones live."""
    sent = sent_document(env.editions_dir / f"{name}.pdf", pages)
    return annotate(sent, env.raw_dir / f"{name}  - Kindle.pdf", marks)


# --- the detector itself ----------------------------------------------------

def test_an_untouched_return_scores_zero_and_ink_scores_far_above_it(env, tmp_path):
    sent = sent_document(tmp_path / "sent.pdf")
    same = annotate(sent, tmp_path / "same.pdf", {})
    inked = annotate(sent, tmp_path / "inked.pdf", {2: "tick", 3: "scrawl"})

    assert ink.score_pages(sent, same) == [0, 0, 0]
    scores = ink.score_pages(sent, inked)
    assert scores[0] == 0
    assert all(s > ink.THRESHOLD for s in scores[1:]), scores
    # and the faintest mark of all — one tick — clears the bar with room
    assert scores[1] > 2 * ink.THRESHOLD, f"a single tick scored only {scores[1]}"


def test_threshold_sits_between_the_measured_artefact_and_the_faintest_real_ink():
    """The two numbers in the module docstring are measurements from the live
    corpus: 9 px on a page of pure print (re-rasterisation), 235 px for two
    handwritten ✓ on a Ozu Mail cover. The threshold has to separate them, and
    a change that moves it outside that gap is a change that either starts
    burning money or starts dropping ink."""
    assert ink.LARGEST_ARTEFACT < ink.THRESHOLD < ink.FAINTEST_REAL_INK
    assert ink.THRESHOLD >= 5 * ink.LARGEST_ARTEFACT
    assert ink.THRESHOLD <= ink.FAINTEST_REAL_INK / 2


def test_every_uncertainty_resolves_to_reading_the_page(env, tmp_path):
    sent = sent_document(tmp_path / "sent.pdf", pages=3)
    # not a PDF at all → unknown, and no exception escapes
    (tmp_path / "junk.pdf").write_bytes(b"not a pdf")
    assert ink.score_pages(sent, tmp_path / "junk.pdf") is None
    # a wholly different document → too much differs to be handwriting
    other = fitz.open()
    for _ in range(3):
        p = other.new_page(width=420, height=595)
        p.draw_rect(fitz.Rect(10, 10, 410, 585), color=None, fill=(0, 0, 0))
    other.save(tmp_path / "other.pdf")
    other.close()
    assert ink.score_pages(sent, tmp_path / "other.pdf") is None


def test_a_document_that_came_back_a_different_length_still_compares(env, tmp_path):
    """Two of the nineteen documents in the corpus came back one page shorter
    than the copy on disk. Demanding equal page counts threw away a tenth of
    the saving; the shared pages still compare, a page with no counterpart is
    read, and a comparison that has slipped out of step is caught by the size
    of the difference rather than by the page count."""
    sent = sent_document(tmp_path / "sent.pdf", pages=4)
    shorter = fitz.open(sent); shorter.delete_page(3)
    shorter.save(tmp_path / "shorter.pdf"); shorter.close()
    assert ink.score_pages(sent, tmp_path / "shorter.pdf") == [0, 0, 0]

    longer = fitz.open(sent); longer.new_page(width=420, height=595)
    longer.save(tmp_path / "longer.pdf"); longer.close()
    scores = ink.score_pages(sent, tmp_path / "longer.pdf")
    assert scores[:4] == [0, 0, 0, 0]
    assert scores[4] == ink.UNKNOWN and ink.has_ink(scores[4]), \
        "a page we have nothing to compare against must be read"


def test_the_sent_copy_is_found_by_the_notebook_name(env):
    sent = sent_document(env.editions_dir / "Ozu Morning 2026-09-20.pdf")
    assert ink.sent_copy("Ozu Morning 2026-09-20", env.sent_dirs) == sent
    # the Kindle suffix is already stripped by ingest, but survive it anyway
    assert ink.sent_copy("Ozu Morning 2026-09-20  - Kindle", env.sent_dirs) == sent
    assert ink.sent_copy("Inbox", env.sent_dirs) is None


# --- what ingest does with it -----------------------------------------------

def queued(env):
    from ozu import db
    con = db.connect(env.db_path)
    return {(r["page_no"], r["status"]) for r in
            con.execute("SELECT page_no, status FROM pages ORDER BY page_no")}


def test_a_clean_return_costs_no_vision_call(env, monkeypatch):
    """The whole point: 67 % of returning pages carry nothing, and those pages
    must never reach the API. They are still ledgered, so the bitmap is
    remembered and a re-export does not ask again."""
    from ozu import ingest, understand
    returning(env, "Ozu Evening 2026-09-20", {2: "scrawl"})

    assert ingest.ingest() == 1, "only the page with ink is queued to be read"
    assert queued(env) == {(1, "skipped"), (2, "new"), (3, "skipped")}

    calls = []
    monkeypatch.setattr(understand, "analyze_page",
                        lambda *a, **k: calls.append(k.get("kind")) or dict(MOCK_RESULT))
    understand.process_pending()
    assert len(calls) == 1, "a skipped page must never reach the vision call"

    # re-exported unchanged: nothing new, nothing re-read
    assert ingest.ingest() == 0


def test_a_notebook_page_is_never_skipped(env):
    """Ink pages have no sent copy by definition and must always be read —
    the detector only ever looks at documents we generated."""
    from ozu import ingest
    make_pdf(env.raw_dir / "Inbox" / "a.pdf", ["handwriting"])
    assert ingest.ingest() == 1
    assert queued(env) == {(1, "new")}


def test_a_return_we_cannot_find_the_original_of_is_read_in_full(env):
    from ozu import ingest
    sent_document(env.editions_dir / "Ozu Evening 2026-09-20.pdf")
    # comes back under a name we never sent
    annotate(env.editions_dir / "Ozu Evening 2026-09-20.pdf",
             env.raw_dir / "Ozu Evening 2026-09-99  - Kindle.pdf", {})
    assert ingest.ingest() == 3


def test_detection_can_be_turned_off(env, monkeypatch):
    from ozu import ingest
    monkeypatch.setattr(env, "ink_detect", False)
    returning(env, "Ozu Evening 2026-09-20", {2: "tick"})
    assert ingest.ingest() == 3


def test_a_skipped_page_can_be_requeued_by_hand(env):
    """The escape hatch. If the detector is ever wrong about a page, the ink
    is not lost — the row is still there and `ozu requeue` turns it back into
    work, without needing the document to be exported again."""
    from ozu import db, ingest
    returning(env, "Ozu Evening 2026-09-20", {2: "scrawl"})
    ingest.ingest()
    con = db.connect(env.db_path)
    n = con.execute("UPDATE pages SET status='new' WHERE status='skipped'"
                    " AND notebook LIKE ?", ("%Evening%",)).rowcount
    con.commit()
    assert n == 2
    assert queued(env) == {(1, "new"), (2, "new"), (3, "new")}
