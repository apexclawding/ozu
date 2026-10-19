"""Draw `tests/fixtures/ink-2000px.png`: a synthetic "handwritten" page.

The render tests need a full-resolution page image to place on a mail page
(the "needs review" path). A real page would carry someone's real notes, so
this one is drawn: ruled paper at the Scribe's export size, an italic face
with a wobbling baseline in blue ink, one red action line, an @tag and a
boxes-and-arrows sketch — the same ingredients a real page has.

    .venv/bin/python tools/fixture_page.py
"""
from __future__ import annotations

import random
from pathlib import Path

import fitz

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "ink-2000px.png"
W, H = 1654, 2339
BLUE = (0.18, 0.2, 0.45)
RED = (0.75, 0.12, 0.12)
RULE = (0.62, 0.62, 0.62)

LINES = [
    "All of these happen at once on brew day and",
    "for every batch the frustration is the same:",
    "three timers on three devices and a notebook",
    "covered in wort. Which is the opportunity",
    "to build one calm screen, like having a",
    "brewing partner that tells me the next step",
    "and keeps the log, and even suggests the",
    "next recipe from the last one's notes.",
]


def main() -> None:
    random.seed(7)                     # the same page every time
    doc = fitz.open()
    page = doc.new_page(width=W, height=H)
    page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
    for y in range(240, 2200, 98):
        page.draw_line((0, y), (W, y), color=RULE, width=2)

    def write(x, y, text, size=58, color=BLUE):
        for ch in text:                # a wobbling baseline reads as ink, not type
            page.insert_text((x, y + random.uniform(-3, 3)), ch, fontsize=size,
                             fontname="TiIt", color=color)
            x += fitz.get_text_length(ch, fontname="TiIt",
                                      fontsize=size) * random.uniform(0.98, 1.06)

    write(130, 215, 'Vision for "Kettle"', 80)
    write(200, 320, "@kettle", 64)
    page.draw_oval(fitz.Rect(180, 265, 420, 345), color=BLUE, width=4)
    page.draw_rect(fitz.Rect(90, 400, 420, 700), color=BLUE, width=5)
    for i, t in enumerate(["Brew day", "starts a", "timer"]):
        write(130, 480 + i * 90, t, 60)
    for y, t in [(470, "mash steps"), (570, "boil + hop alarms"), (690, "cooling + pitch")]:
        page.draw_line((420, y - 20), (610, y - 20), color=BLUE, width=4)
        page.draw_polyline([(590, y - 35), (612, y - 20), (590, y - 5)], color=BLUE, width=4)
        write(630, y, t, 58)
    page.draw_line((150, 700), (150, 820), color=BLUE, width=4)
    page.draw_line((150, 820), (240, 820), color=BLUE, width=4)
    write(260, 840, "one batch log per brew", 58)
    for i, t in enumerate(LINES):
        write(110, 1030 + i * 98, t, 60)
    write(110, 1030 + (len(LINES) + 1) * 98, "Ask the shop about a second fermenter", 60, RED)

    page.get_pixmap(dpi=72).save(OUT)
    print(f"wrote {OUT} ({W}x{H})")


if __name__ == "__main__":
    main()
