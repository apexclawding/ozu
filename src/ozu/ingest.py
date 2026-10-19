"""Watcher + ingest.

1. `pull()`   — rclone copy of the Drive ScribeInbox into ~/ozu-data/raw (idempotent).
2. `ingest()` — every PDF under raw/ is rasterized page-by-page (PyMuPDF);
                each page image is content-hashed; unseen pages are enqueued.

If the Scribe's "Convert to text" was used, the exported PDF carries a text
layer. We capture it and hand it to the vision pass as a transcription prior.
It is only ever a prior: Amazon's OCR has no notion of ink colour, sketches,
or handwritten @tags, which is where this pipeline's meaning actually lives.

Cumulative notebook re-exports are the norm on the Scribe, so the page hash is
the dedup unit — re-exporting a 40-page notebook with one new page costs one
vision call, not forty.

A returning "<brand> " document gets a second filter before the queue (D-09):
`ink.py` compares it with the copy we sent and files the pages that carry no
handwriting as `skipped` rather than queueing them. Measured over every
document that has ever come back, that is 70 % of them — the covers, and
every page of a document read but not written on.
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import subprocess
from pathlib import Path

import fitz  # PyMuPDF

from .config import get_config
from . import db, ink, textlayer

log = logging.getLogger("ozu.ingest")

DPI = 200

# A Scribe export with no Convert-to-text still carries the "1 of 1" page
# footer as a text layer. Handing that to the vision pass as a transcription
# prior is pure noise, so require something with actual substance.
_FOOTER_RE = re.compile(r"^\d+\s+of\s+\d+$", re.I)
MIN_OCR_CHARS = 20


def _useful_ocr(text: str) -> str:
    text = (text or "").strip()
    if len(text) < MIN_OCR_CHARS or _FOOTER_RE.match(text):
        return ""
    return text


def pull() -> None:
    """Sync the Drive inbox down. Requires an rclone remote (OZU_RCLONE_REMOTE)."""
    cfg = get_config()
    cfg.ensure_dirs()
    cmd = ["rclone", "copy", cfg.rclone_remote, str(cfg.raw_dir),
           "--include", "*.pdf", "--max-depth", "3"]
    log.info("rclone: %s", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        raise RuntimeError(f"rclone failed: {res.stderr.strip()[:500]}")


# Amazon exports a notebook as "<Name>  - Kindle.pdf" (note the double space),
# and a RE-export does not overwrite: Drive gets "<Name>  - Kindle (1).pdf".
# Left alone those suffixes end up in every note's `source:`, in asset
# filenames, and in the string the work prefix rule matches against.
_KINDLE_SUFFIX_RE = re.compile(r"\s*-\s*kindle(\s*\(\d+\))?\s*$", re.I)


def _notebook_name(pdf: Path, raw_dir: Path) -> str:
    """Notebook = first path component under raw/, else the PDF stem."""
    rel = pdf.relative_to(raw_dir)
    raw = rel.parts[0] if len(rel.parts) > 1 else pdf.stem
    name = _KINDLE_SUFFIX_RE.sub("", raw)
    return " ".join(name.split()) or raw.strip()


def _page_hash(pix: "fitz.Pixmap") -> str:
    # Amazon stamps a page-position footer into the bottom of every export
    # ("1", "2", "1 of 4"...) that changes with export mode and notebook
    # length. Hash only the content area, or the same ink re-processes on
    # every cumulative export. Found live 2026-08-23: page 1 was read three
    # times under three different footers.
    keep = int(pix.height * 0.95)
    return hashlib.sha256(pix.samples[: keep * pix.stride]).hexdigest()


HASH_SCHEME = "2"  # bump when _page_hash changes; triggers the rehash below


def _rehash_ledger(con) -> None:
    """One-time, in-place: recompute ledger hashes under the current scheme
    so a hash change never makes already-seen pages look new. Rows whose
    source PDF is gone keep their old hash (unique, never matched again);
    rows that collide (same ink, different old footers) also keep theirs —
    the earliest row now owns the content hash and dedup goes through it."""
    if db.get_meta(con, "hash_scheme") == HASH_SCHEME:
        return
    for row in con.execute("SELECT id, pdf_path, page_no FROM pages").fetchall():
        try:
            doc = fitz.open(row["pdf_path"])
            pix = doc[row["page_no"] - 1].get_pixmap(dpi=DPI)
            doc.close()
        except Exception:
            continue
        try:
            con.execute("UPDATE pages SET sha256=? WHERE id=?",
                        (_page_hash(pix), row["id"]))
        except sqlite3.IntegrityError:
            pass
    db.set_meta(con, "hash_scheme", HASH_SCHEME)
    con.commit()
    log.info("page ledger rehashed under scheme %s", HASH_SCHEME)


def _clean_pages(cfg, pdf: Path, notebook: str) -> set[int]:
    """Page numbers (1-based) of a returning document that carry no ink, by
    comparing it with the copy we sent (D-09, `ink.py`). Empty when the
    comparison cannot be made — every page is then read, as before."""
    if not cfg.ink_detect:
        return set()
    sent = ink.sent_copy(notebook, cfg.sent_dirs)
    if sent is None:
        log.info("ink: no sent copy of %r on disk; reading every page", notebook)
        return set()
    scores = ink.score_pages(sent, pdf)
    if scores is None:
        return set()
    clean = {n + 1 for n, s in enumerate(scores)
             if not ink.has_ink(s, cfg.ink_threshold)}
    # A lone tick drawn over its box scores under the page threshold — the
    # printed outline's HALO eats the overlap (Site Audit 2026-09-26 p3:
    # one approval, 72 px, filed as blank). So a page that looks clean is
    # still read if ink sits beside any printed marker, or if the check
    # cannot be made.
    for page_no in sorted(clean):
        ticks = ink.locate(sent, pdf, page_no)
        if ticks is None or any(t.ticked for t in ticks):
            log.info("ink: %s p%d scores clean but %s; reading it", notebook,
                     page_no, "the tick check could not run" if ticks is None
                     else "a printed box carries a tick")
            clean.discard(page_no)
    log.info("ink: %s — %d of %d page(s) carry no handwriting (scores %s)",
             notebook, len(clean), len(scores),
             ",".join(str(s) for s in scores))
    return clean


def ingest() -> int:
    """Rasterize all PDFs in raw/, enqueue unseen pages. Returns count enqueued
    for reading — pages filed as clean by the ink detector are ledgered as
    `skipped` and are not counted, because nothing will read them."""
    cfg = get_config()
    cfg.ensure_dirs()
    con = db.connect(cfg.db_path)
    _rehash_ledger(con)
    new_pages = skipped = 0
    for pdf in sorted(cfg.raw_dir.rglob("*.pdf")):
        notebook = _notebook_name(pdf, cfg.raw_dir)
        # A "<brand> " document coming back from the Scribe is our own output,
        # annotated. Its pages queue as kind='annotation' and are read by the
        # annotation prompt, which extracts only the handwriting — the
        # printed text never becomes notes. Same bitmap dedup as notebooks:
        # a page annotated further re-processes; an untouched one doesn't.
        kind = ("annotation" if notebook.lower().startswith(cfg.digest_prefixes)
                else "ink")
        try:
            doc = fitz.open(pdf)
        except Exception as e:  # corrupt/partial download — skip, retry next run
            log.warning("cannot open %s: %s", pdf, e)
            continue
        # Only a returning document can be compared with something: a notebook
        # page is ink by definition and has no sent copy. Computed once per
        # PDF, before the page loop, and only if some page is actually new.
        clean: set[int] | None = None
        for page_no in range(doc.page_count):
            page = doc[page_no]
            pix = page.get_pixmap(dpi=DPI)
            sha = _page_hash(pix)
            if db.page_seen(con, sha):
                continue
            img_dir = cfg.pages_dir / notebook
            img_dir.mkdir(parents=True, exist_ok=True)
            # Name the image from the normalised notebook, not the PDF stem —
            # the stem still carries Amazon's "  - Kindle" suffix.
            image_path = img_dir / f"{notebook}-p{page_no + 1:03d}-{sha[:8]}.png"
            pix.save(image_path)
            try:
                # textlayer.extract, never page.get_text: this is the printed
                # reference the annotation prompt matches ticks against, and a
                # shaper ligature here reads as a mismatch against the very
                # action line it is quoting (D-05).
                ocr_text = _useful_ocr(textlayer.extract(page))
            except Exception:  # image-only page, or an exporter we don't know
                ocr_text = ""
            if kind == "annotation" and clean is None:
                clean = _clean_pages(cfg, pdf, notebook)
            if clean and page_no + 1 in clean:
                # A tombstone, not a queue entry: the hash is remembered, so a
                # re-export never asks again, and `ozu requeue` can still turn
                # it back into work if the detector is ever wrong.
                db.enqueue_page(con, sha, str(pdf), notebook, page_no + 1,
                                str(image_path), ocr_text, kind=kind,
                                status="skipped", error="no handwriting found")
                skipped += 1
                continue
            db.enqueue_page(con, sha, str(pdf), notebook, page_no + 1,
                            str(image_path), ocr_text, kind=kind)
            new_pages += 1
        doc.close()
    log.info("enqueued %d new page(s); %d returning page(s) carried no ink "
             "and were not read", new_pages, skipped)
    return new_pages
