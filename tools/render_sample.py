"""Build every document type from the fixture corpus into local PDFs.

This is the preview harness the plan called Stage 0. Without it every design
iteration costs a deploy plus a two-hour delivery cycle; with it, looking at
the actual page is a five-second loop. Replaces `render_baseline.py`, which
measured the pre-Story renderer — that renderer no longer exists, so the
script could no longer be run at all.

    PYTHONPATH=src .venv/bin/python tools/render_sample.py [outdir]

Page 1 of each PDF is also saved to `thumbs/` at the size the Scribe's library
actually shows it (30%, ~126px wide). That is the whole point of the cover
(D-06) and the one thing a full-page render cannot tell you: open the folder
and the question "can I tell these apart at a glance?" answers itself.

It also re-checks the two D-01 gates on every case it renders, so the numbers
in `docs/glyph-loss.md` can be reproduced rather than believed:

  * **chars**  — every non-ASCII character in the source reached the text
    layer, and no `·` substitution marker is left in the body.
  * **intact** — long tokens are reconstructible: a break may fall at a hyphen
    or a slash, never between two alphanumerics. See the docstring on
    `tests/test_textlayer.py::test_roundtrip_intact` for why "contiguous" was
    the wrong bar.

No config and no secrets: `OZU_HOME` is pointed at a scratch directory before
`ozu.config` is imported, so `~/ozu-data/.env` is never read. That is what lets the
digest and mail cases render here — they call `get_config()` for the vault
path, which the old baseline script could not satisfy without loading real
config.
"""
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "sample"
OUT.mkdir(parents=True, exist_ok=True)

# before any ozu import: a throwaway home, so no real config or secret is read
_SCRATCH = tempfile.mkdtemp(prefix="ozu-sample-")
os.environ["OZU_HOME"] = _SCRATCH
os.environ["OZU_VAULT_DIR"] = str(Path(_SCRATCH) / "vault")
os.environ.setdefault("ANTHROPIC_API_KEY", "not-used-here")

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import fitz  # noqa: E402
import corpus  # noqa: E402
from ozu import textlayer  # noqa: E402
from ozu.digest_pdf import (build_research_pdf, build_mail_pdf, build_pdf,  # noqa: E402
                            build_learn_pdf, build_propose_pdf, build_result_pdf,
                            build_edition_pdf, NEW_NOTE_LIMIT)

BUILDERS = {
    "research": lambda payload, out: build_research_pdf(out_path=out, **payload),
    "mail": lambda payload, out: build_mail_pdf(payload, out),
    "digest": lambda payload, out: build_pdf(payload, out),
    "learn": lambda payload, out: build_learn_pdf(payload, out),
    "build-propose": lambda payload, out: build_propose_pdf(payload, out),
    "build-result": lambda payload, out: build_result_pdf(payload, out),
    "edition": lambda payload, out: build_edition_pdf(payload, out),
}


def source_text(case) -> str:
    """Everything the case put in, as one string, for the gates to look for."""
    p = case["payload"]
    if case["kind"] == "research":
        return p["report"]
    if case["kind"] == "build-propose":
        return "\n".join([p["proposal"], p.get("history", "")])
    if case["kind"] == "build-result":
        return "\n".join([p["report"], p.get("history", ""),
                          *(p.get("actions") or [])])
    bits = []
    if case["kind"] == "edition":
        # the sections' own strings, gathered the way their own cases do
        parts = []
        for r in p.get("results") or []:
            parts.append(source_text({"kind": "build-result", "payload": r}))
        if p.get("mail"):
            parts.append(source_text({"kind": "mail", "payload": p["mail"]}))
        for d in (p.get("dead") or {}).get("recent") or []:
            parts += [d["name"], d.get("reason", "")]
        return "\n".join(b for b in parts if b)
    if case["kind"] == "learn":
        for item in p.get("new") or []:
            bits += [item["title"], item["brief"]]
        for d in p.get("due") or []:
            bits += [d["title"], d.get("cue", ""), d.get("question", "")]
        for s_ in p.get("skipped") or []:
            bits += [s_["title"], s_.get("reason", "")]
        return "\n".join(b for b in bits if b)
    for key in ("new", "review", "built", "r_built", "queued", "failed",
                "r_queued", "r_failed", "plain", "work", "research"):
        rows = p.get(key) or []
        # `build_pdf` prints at most NEW_NOTE_LIMIT new notes and says so on
        # the page. Comparing against all 40 would report the cap as a
        # character loss — the 40-note fixture's 24th note carries an `é`,
        # which is how this was found. Import the limit rather than repeat it,
        # so the harness cannot drift from the renderer.
        if key == "new":
            rows = rows[:NEW_NOTE_LIMIT]
        for row in rows:
            bits += [str(row.get(k, "")) for k in ("title", "summary", "text")]
    for a in (p.get("actions") or []):
        bits.append(str(a.get("text", "")))
    for r in (p.get("receipts") or []):
        bits += [str(r.get("closed") or ""), str(r.get("line") or "")]
        bits += [str(c) for c in (r.get("candidates") or [])]
    for item in (p.get("plain") or []):
        bits += [str(a) for a in (item.get("actions") or [])]
    return "\n".join(b for b in bits if b)


def reconstruct(token, text):
    """See tests/test_textlayer.py::_reconstruct — kept in step with it."""
    for start in range(len(text)):
        i, j, mid = start, 0, []
        while j < len(token) and i < len(text):
            if text[i] == "\n":
                if 0 < j and token[j - 1].isalnum() and token[j].isalnum():
                    mid.append(f"{token[max(0, j - 8):j]}|{token[j:j + 8]}")
                i += 1
                continue
            if text[i] != token[j]:
                break
            i += 1
            j += 1
        if j == len(token):
            return True, mid
    return False, None


print(f"{'case':13} {'kind':9} {'pages':>5} {'KB':>5} {'·':>3} {'lig':>4}  verdict")
print("-" * 88)
fails = 0
THUMBS = OUT / "thumbs"
THUMBS.mkdir(parents=True, exist_ok=True)
# the library grid on the device: an A5 page at ~126px wide
THUMB = fitz.Matrix(0.3, 0.3)

for name, case in corpus.sample().items():
    out = OUT / f"{name}.pdf"
    BUILDERS[case["kind"]](case["payload"], out)
    with fitz.open(out) as doc:
        pages = doc.page_count
        got = "\n".join(textlayer.extract(p) for p in doc)
        raw = "".join(p.get_text() for p in doc)
        doc[0].get_pixmap(matrix=THUMB).save(THUMBS / f"{name}.png")

    src = source_text(case)
    lost = sorted({c for c in src if ord(c) > 127} - set(got))
    # the header/footer meta separator is legitimate furniture; a · anywhere
    # else is the substitution signature of a font missing the glyph. Match it
    # across whitespace, not on a literal " · ": a separator that falls at a
    # line break arrives as " ·\n", and counting that as a loss reported three
    # phantom failures on the first document long enough to wrap on one.
    dots = len(re.sub(r"\s·\s", "", got, flags=re.S).split("·")) - 1
    ligs = sorted({c for c in raw if 0xFB00 <= ord(c) <= 0xFB06})
    broken = []
    for tok in {textlayer.fold(w) for w in src.split() if len(w) > 60}:
        found, mid = reconstruct(tok, got)
        if not found:
            broken.append("unreconstructible")
        elif mid:
            broken += mid

    ok = not lost and not dots and not broken
    fails += 0 if ok else 1
    print(f"{name:13} {case['kind']:9} {pages:5} {out.stat().st_size // 1024:5} "
          f"{dots:3} {len(ligs):4}  {'OK' if ok else 'FAIL'}"
          + (f"  lost {' '.join(lost)}" if lost else "")
          + (f"  mid-word {broken}" if broken else ""))

    # a cap is fine; a cap the reader cannot see is not
    n_new = len(case["payload"].get("new") or [])
    if n_new > NEW_NOTE_LIMIT:
        said = f"+ {n_new - NEW_NOTE_LIMIT} more" in got
        print(f"{'':13} {'':9} {'':5} {'':5} {'':3} {'':4}  "
              f"capped at {NEW_NOTE_LIMIT} of {n_new} new notes; "
              f"page says so: {said}")
        if not said:
            fails += 1

print("-" * 88)
print(f"{len(corpus.sample()) - fails}/{len(corpus.sample())} cases clean.")
print(f"\nPDFs in {OUT}; cover thumbnails at library scale in {THUMBS}")
print("The `lig` column counts U+FB00–FB06 in the RAW text layer. A non-zero\n"
      "count there is expected and fine — Charis shapes 'fi' — because every\n"
      "reader goes through textlayer.extract(), which folds them. It is the\n"
      "number that would have silently broken tick-matching without that fold.")
