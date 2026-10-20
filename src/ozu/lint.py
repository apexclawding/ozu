"""A lint pass over the learning folder: what the transcript → brief loop
left inconsistent.

`okf check` answers "is this a conformant bundle"; this answers "does the
learning loop hold together" — the question the wiki pattern's *lint* step asks
(orphans, missing links, pages that no longer agree with each other). Only
the mechanical half is here. Every finding is decided from frontmatter and headings, with no
model call, so it is free to run, cannot be steered by a transcript's
captions, and says the same thing twice in a row.

Read-only by construction: it takes a vault path and returns strings. It
writes nothing, so it needs no lock and cannot race the tick's vault push.

What it checks, per note in the folder:

- a transcript whose brief run failed (`learn: error`) — nothing retries it;
- a transcript marked `learn: done` with no Learning note drawn from it;
- a Learning note whose transcript is no longer in the vault;
- two transcripts of the same video (pinned twice, briefed twice), or two
  notes of the same link or file (`articles.capture` refuses that itself,
  so one means something else wrote it);
- a brief missing a section of `prompts/learn_prompt.md`'s shape — the
  learning sheet's due-again cue is the first Takeaway and its prompt the
  Question, so a brief without them comes back as a blank line;
- a brief with no section of that shape at all — the run's reply was
  something other than a brief (a summary of a file it tried to write);
- a brief whose "Where it touches your work" cites no project note —
  only the learning folder or wiki concept pages, which the prompt asks
  for *as well as* a project, never instead (the prompt allows "nothing
  relates", so this is a pointer to read the brief again, not a defect);
- a note in the folder the loop did not write: anything whose `source` is
  not a transcript (`youtube:`), an article or a sent file (`web:`, `file:`,
  written by `articles.py`) or a brief (`learn:`). The learning run
  reads the vault and writes nothing, so one of these means
  something wrote into the vault that should not have;
- a wikilink to a note that does not exist (`[[…]]` inside code is syntax
  being shown, not a link — `okf.wikilinks`).
"""
from __future__ import annotations

from pathlib import Path

from . import okf

FOLDER = "topics/learning"
WIKI = Path("topics/wiki")
LOOP_SOURCES = ("youtube", "web", "file", "learn")   # transcripts, articles, the briefs drawn from them
SOURCE_KINDS = ("youtube", "web", "file")

# The brief's shape, as learn_prompt.md asks for it — matched on the start
# of the lower-cased heading, the way learn._sections/_first_line read it.
SECTIONS = ("takeaways", "techniques", "where it touches", "try this week", "question")
EMPTY_BRIEF = "(no brief returned)"     # what vault.write_learning_note writes for nothing


def _kind(fm: dict) -> str:
    return okf.source_kind(str(fm.get("source") or ""))


def _headings(body: str) -> list[str]:
    return [line.strip().lstrip("#").strip().lower()
            for line in body.splitlines() if line.strip().startswith("#")]


def _brief_findings(rel: Path, body: str) -> list[str]:
    if EMPTY_BRIEF in body:
        return [f"{rel}: the run returned no brief"]
    heads = _headings(body)
    have = [s for s in SECTIONS if any(h.startswith(s) for h in heads)]
    if not have:
        return [f"{rel}: no brief sections at all — the run's reply was not a brief"]
    # "entertainment with nothing to learn": Takeaways alone is the whole brief
    if have == ["takeaways"]:
        return []
    missing = [s for s in SECTIONS if s not in have]
    if missing:
        return [f"{rel}: brief is missing {', '.join(repr(m) for m in missing)}"]
    return []


def lint(vault_dir: Path, folder: str = FOLDER) -> list[str]:
    """Findings for `folder` (vault-relative), one line each, sorted by
    path. Links resolve against the whole vault: a brief citing a project
    note elsewhere is the point, not an error."""
    concepts = okf.load_concepts(vault_dir)
    ids = {str(fm.get("id")): fm for fm in concepts if fm.get("id")}
    root = Path(folder)
    mine = [fm for fm in concepts if root in fm["_rel"].parents]
    in_folder = {str(fm.get("id")) for fm in mine if fm.get("id")}
    not_projects = in_folder | {i for i, fm in ids.items() if WIKI in fm["_rel"].parents}

    briefed = {str(fm.get("source"))[len("learn:"):]
               for fm in concepts if _kind(fm) == "learn"}
    by_video: dict[str, Path] = {}
    out: list[str] = []
    for fm in mine:
        rel, body, kind = fm["_rel"], fm["_body"], _kind(fm)
        nid = str(fm.get("id") or "")
        if kind not in LOOP_SOURCES:
            out.append(f"{rel}: not written by the learning loop "
                       f"(source: {fm.get('source') or 'none'})")
        if kind in SOURCE_KINDS:
            state = str(fm.get("learn") or "")
            if state == "error":
                out.append(f"{rel}: brief run failed (learn: error) — nothing retries it")
            elif state == "done" and nid not in briefed:
                out.append(f"{rel}: learn: done, but no Learning note is drawn from it")
            vid = str(fm.get("video_id") or "") or (str(fm.get("source")) if kind != "youtube" else "")
            if vid and vid in by_video:
                what = "video" if kind == "youtube" else "link or file"
                out.append(f"{rel}: same {what} ({vid}) as {by_video[vid]}")
            elif vid:
                by_video[vid] = rel
        elif kind == "learn":
            src = str(fm.get("source"))[len("learn:"):]
            if src not in ids:
                out.append(f"{rel}: its source {src} is not in the vault")
            out += _brief_findings(rel, body)
            touches = any(h.startswith("where it touches") for h in _headings(body))
            cited = okf.wikilinks(body) - {nid, src}
            if touches and not cited - not_projects:
                out.append(f"{rel}: cites no project note, only {folder} or wiki "
                           f"pages — touches no project")
        for target in sorted(okf.wikilinks(body)):
            if target not in ids:
                out.append(f"{rel}: link to unknown note {target}")
    return sorted(out)
