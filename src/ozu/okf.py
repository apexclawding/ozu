"""The vault's file contract: Open Knowledge Format v0.2.

vault is an OKF bundle (https://github.com/GoogleCloudPlatform/open-knowledge-format,
SPEC.md; a copy lives at ~/okf on the VPS). OKF standardises exactly two
things, and this module is the only place in the pipeline that knows either:

1. **Layout.** Concepts are Markdown files with YAML frontmatter. `index.md`
   in every directory is the progressive-disclosure listing; `log.md` at the
   root is the update history. Both are reserved names and both are
   *derived* here from the notes' frontmatter — never appended to by a
   writer. That is what makes the vault safe for several writers (the VPS,
   a capture bridge, a script on another machine) at once: they write notes, and the VPS
   indexer regenerates the views. Adopted 2026-09-02, when the vault had 31
   notes; see docs/okf.md for the decisions.

2. **Frontmatter.** `type` is the only required key. `title`, `description`
   and `tags` are recommended; `generated` (who wrote it, when), `sources`
   (what it was made from), `status` and `verified` carry provenance and
   trust. Every other key the pipeline writes (`id`, `created`, `source`,
   `image`, `needs_review`, the directive ledgers …) is an OKF *extension*
   and stays exactly as it was.

Two house conventions sit on top of the spec:

- A note's filename is its `id` (`topics/ideas/20260818-check-cars-maintenance.md`),
  so `[[id]]` wikilinks resolve in Obsidian and on GitHub, the OKF concept
  id (path minus `.md`) and our id agree, and nothing needs an alias table.
- Bodies link with `[[id]]`, the form the prompts produce and Obsidian
  draws a graph from. `export()` rewrites them to relative Markdown links
  for consumers that only speak OKF (the bundled visualizer, other people's
  agents).
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

import yaml

log = logging.getLogger("ozu.okf")

OKF_VERSION = "0.2"
RESERVED = ("index.md", "log.md")
LEGACY_REGISTRIES = ("INDEX.md", "_topic.md")   # pre-OKF; their presence is an error

# `type` by the prefix of our `source:` key — the one field every writer
# already sets, so a note's kind never has to be guessed from its folder.
TYPE_BY_SOURCE = {
    "scribe": "Note",            # a handwritten page, read by the vision pass
    "voice": "Voice Memo",       # transcribed on-device by a capture bridge
    "youtube": "Transcript",     # captions of a pinned video
    "learn": "Learning",         # brief drawn from a transcript
    "research": "Research",      # overnight brief from a handwritten question
    "claude": "Build Report",    # companion of a @claude work run
    "build": "Build",            # a paper-approved Build (proposal → PR)
    "wiki": "Concept",           # a compiled concept page (wiki.py)
}
DEFAULT_TYPE = "Note"
# Root-level documents the pipeline knows by name.
GUIDE_TYPE = "Guide"

# Exactly two id shapes exist: lowercase date-slug, and the Build id
# (`[[B-260906-7f3a]]`). The pattern admits only those — a looser
# any-case match would start minting edges from stray [[CamelCase]] text.
WIKILINK = re.compile(r"\[\[(B-\d{6}-[0-9a-f]{4}|[a-z0-9-]+)\]\]")
# `[[id]]` inside a code span or fence is syntax being shown, not a link:
# a note about wikilinks writes `[[note-id]]` and links nothing.
_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def wikilinks(body: str) -> set[str]:
    """The note ids a body links to, outside code spans and fences."""
    return set(WIKILINK.findall(_CODE.sub("", body)))


def source_kind(source: str) -> str:
    """The producer named by a `source:` value: `scribe:Inbox/p6` → scribe,
    `research:20260823-x` → research."""
    m = re.match(r"\s*([a-z]+)", (source or "").lower())
    return m.group(1) if m else ""


def type_for(source: str) -> str:
    """`scribe:Inbox/p6` → Note, `research:2026…` → Research, …"""
    return TYPE_BY_SOURCE.get(source_kind(source), DEFAULT_TYPE)


def now_iso() -> str:
    """OKF timestamps are ISO 8601 with an explicit offset; we write UTC."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def actor(producer: str, version: str = "") -> str:
    """The §7 actor convention: `<producer>/<version>` for tools."""
    return f"{producer}/{version}" if version else producer


# ---------------------------------------------------------------------------
# frontmatter: dump and parse

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(T[\d:]+(\.\d+)?(Z|[+-]\d{2}:\d{2})?)?$")
_NUMBER = re.compile(r"^[-+]?(\d[\d_]*(\.\d*)?|\.\d+)([eE][-+]?\d+)?$")
_SEXAGESIMAL = re.compile(r"^[-+]?\d+(:\d+)+(\.\d*)?$")    # 00:19:37 — YAML 1.1 reads it as an int
_RESERVED_WORDS = {"true", "false", "null", "yes", "no", "on", "off", "~", ""}


def _scalar(v) -> str:
    """One YAML scalar, quoted only when a plain scalar would be misread.

    Dates stay bare (`created: 2026-08-25` — Obsidian shows a date property,
    and the OKF loader keeps it a string); anything that would parse as a
    number, boolean, sexagesimal, or contain a mapping/comment indicator is
    double-quoted. Titles like `Learning: Finally, an Open Standard…` are the
    common case — a bare `: ` inside a plain scalar is a YAML error, which
    is why the vault's hand-rolled frontmatter was never strictly parseable.
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("\n", " ").strip()
    if _DATE.match(s):
        return s
    needs_quote = (
        s.lower() in _RESERVED_WORDS
        or _NUMBER.match(s) or _SEXAGESIMAL.match(s)
        or ": " in s or " #" in s
        or s.endswith(":")
        or s[0] in "-?:,[]{}#&*!|>'\"%@`"
        or s != str(v).strip()
    )
    if not needs_quote:
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dump_frontmatter(fields: dict) -> str:
    """Serialize a frontmatter block in the vault's house style.

    Scalars and flat lists keep the one-line forms every reader here greps
    for (`tags: [a, b]`, `related: [...]` — pmaster's `check` insists the
    related array opens and closes on one line). Mappings and lists of
    mappings (`generated`, `sources`, `verified`) use block style, except
    `generated` which follows the spec's own inline `{ by, at }` idiom.
    `None` values are skipped, so callers can pass optional keys freely.
    """
    lines = ["---"]
    for k, v in fields.items():
        if v is None:
            continue
        if isinstance(v, dict):
            if k == "generated" or k != "related_why" and all(
                    not isinstance(x, (dict, list)) for x in v.values()) and len(v) <= 3:
                inner = ", ".join(f"{ik}: {_scalar(iv)}" for ik, iv in v.items() if iv is not None)
                lines.append(f"{k}: {{ {inner} }}")
            else:
                lines.append(f"{k}:")
                for ik, iv in v.items():
                    if iv is not None:
                        lines.append(f"  {_scalar(ik)}: {_scalar(iv)}")
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            lines.append(f"{k}:")
            for item in v:
                first = True
                for ik, iv in item.items():
                    if iv is None:
                        continue
                    lines.append(f"  {'- ' if first else '  '}{ik}: {_scalar(iv)}")
                    first = False
        elif isinstance(v, list):
            lines.append(f"{k}: [{', '.join(_scalar(x) for x in v)}]")
        else:
            lines.append(f"{k}: {_scalar(v)}")
    lines.append("---\n")
    return "\n".join(lines)


class _Loader(yaml.SafeLoader):
    """SafeLoader that keeps dates and timestamps as the text written.

    Same trick as OKF's reference parser: PyYAML speaks YAML 1.1, whose
    implicit resolver turns `2026-08-25` into a `date` object, which then
    fails every `str` comparison downstream and rewrites itself on a
    round-trip. Booleans are still resolved (`needs_review: false`).
    """


_Loader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _split(text: str) -> tuple[str | None, str]:
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end == -1:
        return None, text
    return text[3:end], text[end + 4:]


def _parse_legacy(head: str) -> dict:
    """The pre-OKF line parser: `k: v`, and `k: [a, b]` as a list. Kept as
    the fallback for hand-edited notes whose YAML does not parse strictly —
    `check()` reports those, but no reader should choke on them."""
    fm: dict = {}
    for line in head.strip().splitlines():
        if ":" not in line or line.startswith((" ", "\t", "-")):
            continue
        k, _, v = line.partition(":")
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            fm[k.strip()] = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        elif len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            fm[k.strip()] = v[1:-1]        # a quoted scalar, as YAML would read it
        else:
            fm[k.strip()] = v
    return fm


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Frontmatter dict + body. Strict YAML first, legacy line parser second.
    Values are strings, lists, dicts or bools — never dates."""
    head, body = _split(text)
    if head is None:
        return {}, text
    try:
        fm = yaml.load(head, Loader=_Loader)
        if isinstance(fm, dict):
            return {str(k): v for k, v in fm.items()}, body
    except yaml.YAMLError:
        pass
    return _parse_legacy(head), body


def strict_error(text: str) -> str | None:
    """Why the frontmatter is not conformant YAML, or None when it is."""
    head, _ = _split(text)
    if head is None:
        return "no frontmatter block"
    try:
        fm = yaml.load(head, Loader=_Loader)
    except yaml.YAMLError as e:
        return f"frontmatter is not valid YAML: {str(e).splitlines()[0]}"
    if not isinstance(fm, dict):
        return "frontmatter is not a mapping"
    return None


def related_why(fm: dict) -> dict[str, str]:
    """Why each link exists, as the note itself records it: `related_why`
    is either one sentence for every link (a wiki page) or a mapping
    id → reason. The edges' `reason` derives from this, so a rebuild keeps
    what the writer knew when it made the link."""
    why = fm.get("related_why")
    if isinstance(why, str) and why.strip():
        return {"*": why.strip()}          # one reason for every link
    if isinstance(why, dict):
        return {str(k): str(v).strip() for k, v in why.items()
                if v is not None and str(v).strip()}
    return {}


def description_of(fm: dict) -> str:
    """OKF `description`, with the pre-OKF `summary` as the fallback."""
    return str(fm.get("description") or fm.get("summary") or "").strip()


# ---------------------------------------------------------------------------
# walking a bundle

def _is_hidden(rel: Path) -> bool:
    return any(p.startswith(".") for p in rel.parts)


def concept_files(vault_dir: Path) -> list[Path]:
    """Every concept document: all `.md` files minus the reserved names,
    the legacy registries, and anything under a dot-directory (`.git`,
    `.obsidian`, `.handover` — the repo's own housekeeping, not knowledge)."""
    out = []
    for path in sorted(vault_dir.rglob("*.md")):
        rel = path.relative_to(vault_dir)
        if _is_hidden(rel) or path.name in RESERVED or path.name in LEGACY_REGISTRIES:
            continue
        out.append(path)
    return out


def load_concepts(vault_dir: Path) -> list[dict]:
    """Frontmatter of every concept, plus `_path` (absolute) and `_rel`."""
    out = []
    for path in concept_files(vault_dir):
        fm, body = parse_frontmatter(path.read_text())
        fm["_path"], fm["_rel"], fm["_body"] = path, path.relative_to(vault_dir), body
        out.append(fm)
    return out


# ---------------------------------------------------------------------------
# derived views: index.md and log.md

def _entry(title: str, link: str, desc: str) -> str:
    return f"* [{title}]({link})" + (f" - {desc}" if desc else "")


def _sort_key(fm: dict) -> str:
    # newest first: ids are date-prefixed; fall back to `created`, then path
    return str(fm.get("id") or fm.get("created") or "") + "|" + str(fm["_rel"])


def _dir_index(vault_dir: Path, directory: Path, concepts: list[dict], subdirs: dict[Path, str]) -> str:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for fm in concepts:
        grouped[str(fm.get("type") or "Other")].append(fm)
    sections: list[str] = []
    if directory == vault_dir:
        sections.append(f'---\nokf_version: "{OKF_VERSION}"\n---')
    if subdirs:
        lines = ["# Folders", ""]
        for sub, desc in sorted(subdirs.items(), key=lambda kv: str(kv[0])):
            rel = sub.relative_to(directory).as_posix()
            lines.append(_entry(rel, f"{rel}/index.md", desc))
        sections.append("\n".join(lines))
    for typ in sorted(grouped):
        lines = [f"# {typ}", ""]
        for fm in sorted(grouped[typ], key=_sort_key, reverse=True):
            title = str(fm.get("title") or fm["_path"].stem)
            lines.append(_entry(title, fm["_path"].name, description_of(fm)))
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"


def _describe_dir(concepts: list[dict], nested: int) -> str:
    """A folder's one-line description for the parent index: count, and the
    newest note so an agent knows what it will find without opening it."""
    n = len(concepts) + nested
    if not concepts:
        return f"{n} note{'s' if n != 1 else ''}"
    newest = max(concepts, key=_sort_key)
    date = str(newest.get("created") or "")
    return (f"{n} note{'s' if n != 1 else ''}; newest {date}: "
            f"{str(newest.get('title') or newest['_path'].stem)}").strip()


def render_indexes(vault_dir: Path) -> dict[Path, str]:
    """index.md text for every directory that holds concepts (or holds one
    that does), keyed by path. Pure — nothing is written."""
    concepts = load_concepts(vault_dir)
    by_dir: dict[Path, list[dict]] = defaultdict(list)
    for fm in concepts:
        by_dir[fm["_path"].parent].append(fm)
    # every ancestor of a concept directory gets an index, up to the root
    dirs: set[Path] = {vault_dir}
    for d in list(by_dir):
        cur = d
        while cur != vault_dir:
            dirs.add(cur)
            cur = cur.parent
    counts: dict[Path, int] = defaultdict(int)     # concepts under a dir, recursively
    for fm in concepts:
        cur = fm["_path"].parent
        while True:
            counts[cur] += 1
            if cur == vault_dir:
                break
            cur = cur.parent

    out: dict[Path, str] = {}
    for d in sorted(dirs):
        subdirs = {s: _describe_dir(by_dir.get(s, []), counts[s] - len(by_dir.get(s, [])))
                   for s in dirs if s.parent == d and s != d}
        # the root lists topic folders directly (two hops to a note, not three)
        if d == vault_dir:
            for s in list(subdirs):
                if not by_dir.get(s):      # a pure container like topics/
                    for ss in dirs:
                        if ss.parent == s:
                            subdirs[ss] = _describe_dir(by_dir.get(ss, []), 0)
                    del subdirs[s]
        out[d / "index.md"] = _dir_index(vault_dir, d, by_dir.get(d, []), subdirs)
    return out


def regenerate_indexes(vault_dir: Path) -> list[Path]:
    """Write every index.md; returns the paths that changed."""
    changed = []
    for path, text in render_indexes(vault_dir).items():
        if not path.exists() or path.read_text() != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            changed.append(path)
    return changed


def render_log(vault_dir: Path) -> str:
    """log.md per OKF §9, derived from `created`/`updated`/`status` so it
    needs no writer of its own. Newest day first."""
    events: dict[str, list[str]] = defaultdict(list)
    for fm in load_concepts(vault_dir):
        title = str(fm.get("title") or fm["_path"].stem)
        rel = fm["_rel"].as_posix()
        gen = fm.get("generated") if isinstance(fm.get("generated"), dict) else {}
        who = f" — {gen['by']}" if gen.get("by") else ""
        created, updated = str(fm.get("created") or ""), str(fm.get("updated") or "")
        if created:
            events[created].append(f"* **Creation**: [{title}]({rel}){who}")
        if updated and updated != created:
            kind = "Deprecation" if str(fm.get("status") or "") == "deprecated" else "Update"
            events[updated].append(f"* **{kind}**: [{title}]({rel})")
    parts = ["# Vault Update Log", ""]
    for day in sorted(events, reverse=True):
        parts.append(f"## {day}")
        parts.extend(sorted(events[day]))
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def regenerate_log(vault_dir: Path) -> bool:
    path = vault_dir / "log.md"
    text = render_log(vault_dir)
    if path.exists() and path.read_text() == text:
        return False
    path.write_text(text)
    return True


def remove_legacy_registries(vault_dir: Path) -> list[Path]:
    """Delete INDEX.md / _topic.md wherever a pre-OKF writer left them.

    A producer still on the old code (a capture bridge until it is
    rebuilt) re-creates them beside the generated index.md. Left alone they
    are worse than clutter: on a case-insensitive disk INDEX.md and index.md
    are one file, and a clone that tracks both can never be checked out
    clean — which is exactly what stopped the Mac from pulling on
    2026-09-03. The registries are derived views, so deleting them loses
    nothing; the note they pointed at is still there and gets indexed."""
    removed = []
    for name in LEGACY_REGISTRIES:
        for p in sorted(vault_dir.rglob(name)):
            if _is_hidden(p.relative_to(vault_dir)):
                continue
            p.unlink()
            removed.append(p)
            log.warning("removed legacy registry %s (a pre-OKF writer is still active)",
                        p.relative_to(vault_dir))
    return removed


def regenerate(vault_dir: Path) -> list[Path]:
    """Both derived views, and the removal of any legacy ones. Called after
    every write and by `ozu index`; returns every path that changed."""
    changed = remove_legacy_registries(vault_dir)
    changed += regenerate_indexes(vault_dir)
    if regenerate_log(vault_dir):
        changed.append(vault_dir / "log.md")
    return changed


# ---------------------------------------------------------------------------
# the vault's CLAUDE.md — a Guide concept, and the router for agent runs

GUIDE = """---
type: Guide
title: How to use this vault
description: Where things are, how to find a note in two reads, how to cite it, and what the frontmatter promises.
tags: [guide, okf]
created: {date}
updated: {date}
generated: {{ by: ozu/okf, at: {now} }}
---

# How to use this vault

This is a personal knowledge base: handwritten pages from an e-ink
tablet, voice memos, video transcripts, and the briefs written from them.
It is an **Open Knowledge Format (OKF v0.2) bundle** — plain Markdown with
YAML frontmatter, organised so an agent can find what it needs in two reads.

## Finding things

1. `index.md` (this folder) lists the topic folders with a count and the
   newest note in each. Pick the folder that fits the question.
2. That folder's `index.md` lists every note there, newest first, grouped by
   `type`, each with a one-line description. Open only the 1–3 that fit.
3. `log.md` is the chronological history — what was created or updated when.
4. `topics/wiki/` holds the concept pages (`type: Concept`): what is known
   about one subject across every note that touched it, each claim citing
   its sources. Start there for "what do I know about X"; open the cited
   notes for the detail.

Prefer opening a few specific notes over scanning everything. Full-text
search (`grep -ril`) is fine when the indexes do not point anywhere.

## Citing and linking

Every note's filename is its `id`. Cite a note as `[[<note-id>]]` — the id
exactly as it appears in the frontmatter, which is also the filename without
`.md`. `related:` in the frontmatter carries the same ids.

## What the frontmatter promises

- `type` — the kind of document: Note (a handwritten page), Voice Memo,
  Transcript, Learning, Research, Build, Build Report, Concept, Guide.
- `description` — one sentence; the index line.
- `generated` — who produced the content and when. Almost everything here
  is machine-written from the owner's own ink or recordings; `sources` says
  from what (the page image, the video, the note that asked).
- `status: draft` — the transcription was low-confidence (`needs_review`);
  check the linked page image before relying on the words.
- No `verified` key means nobody has confirmed the content against its
  source yet. Treat it as a faithful read, not a checked fact.

## Writing

Agents do not edit `index.md` or `log.md` — both are regenerated from the
notes by the indexer. New knowledge is a new note with `type`, `id`,
`title`, `description`, `created`, `updated`, `source`, `related` and
`generated` — plus `related_why`, why each link exists, when you know it;
the same idea changed is an edit in place with `updated` bumped. Never
merge in content from the walled work vault.
"""


def write_guide(vault_dir: Path, *, force: bool = False) -> bool:
    path = vault_dir / "CLAUDE.md"
    if path.exists() and not force:
        return False
    today = dt.date.today().isoformat()
    path.write_text(GUIDE.format(date=today, now=now_iso()))
    return True


# ---------------------------------------------------------------------------
# conformance

def check(vault_dir: Path) -> tuple[list[str], list[str]]:
    """(errors, warnings). Errors break OKF v0.2 conformance or the house
    conventions the pipeline relies on; warnings are things a consumer must
    tolerate but a human would want fixed."""
    errors, warnings = [], []
    for name in LEGACY_REGISTRIES:
        for p in vault_dir.rglob(name):
            if not _is_hidden(p.relative_to(vault_dir)):
                errors.append(f"{p.relative_to(vault_dir)}: legacy registry — delete it; index.md is generated")
    ids: dict[str, Path] = {}
    concepts = []
    for path in concept_files(vault_dir):
        rel = path.relative_to(vault_dir)
        text = path.read_text()
        err = strict_error(text)
        if err:
            errors.append(f"{rel}: {err}")
            continue
        fm, body = parse_frontmatter(text)
        if not str(fm.get("type") or "").strip():
            errors.append(f"{rel}: missing required `type`")
        nid = str(fm.get("id") or "")
        if nid:
            if nid in ids:
                errors.append(f"{rel}: duplicate id {nid} (also {ids[nid].relative_to(vault_dir)})")
            ids[nid] = path
            if path.stem != nid and path.parent != vault_dir:
                warnings.append(f"{rel}: filename is not the id ({nid})")
        elif path.parent != vault_dir:
            warnings.append(f"{rel}: no `id` — wikilinks cannot reach it")
        if not description_of(fm):
            warnings.append(f"{rel}: no `description`")
        concepts.append((rel, fm, body))
    for rel, fm, body in concepts:
        related = fm.get("related") if isinstance(fm.get("related"), list) else []
        for target in sorted(wikilinks(body) | {str(r) for r in related}):
            if target not in ids:
                warnings.append(f"{rel}: link to unknown note {target}")
    for path, text in render_indexes(vault_dir).items():
        if not path.exists():
            errors.append(f"{path.relative_to(vault_dir)}: missing — run `ozu index`")
        elif path.read_text() != text:
            errors.append(f"{path.relative_to(vault_dir)}: stale — run `ozu index`")
    logp = vault_dir / "log.md"
    if not logp.exists():
        errors.append("log.md: missing — run `ozu index`")
    elif logp.read_text() != render_log(vault_dir):
        errors.append("log.md: stale — run `ozu index`")
    return errors, warnings


# ---------------------------------------------------------------------------
# export: a pure-OKF copy for consumers that do not speak wikilinks

EXPORT_MARKER = ".okf-export"


def export(vault_dir: Path, out_dir: Path) -> dict:
    """Copy the bundle to `out_dir` with `[[id]]` rewritten as relative
    Markdown links (`[Title](../ideas/2026….md)`), assets included, indexes
    and log regenerated. Refuses to wipe a directory it did not create."""
    if out_dir.exists():
        if not (out_dir / EXPORT_MARKER).exists():
            raise SystemExit(f"{out_dir} exists and is not a previous export; refusing to overwrite")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    (out_dir / EXPORT_MARKER).write_text(f"exported {now_iso()} from {vault_dir}\n")

    concepts = load_concepts(vault_dir)
    by_id = {str(fm["id"]): fm for fm in concepts if fm.get("id")}
    rewritten = 0

    def link(src_rel: Path, target_id: str) -> str:
        target = by_id[target_id]
        rel = os.path.relpath(target["_rel"], start=src_rel.parent)
        return f"[{target.get('title') or target_id}]({Path(rel).as_posix()})"

    for fm in concepts:
        text = fm["_path"].read_text()
        head, body = _split(text)
        n = 0

        def repl(m):
            nonlocal n
            if m.group(1) in by_id:
                n += 1
                return link(fm["_rel"], m.group(1))
            return m.group(0)
        body = WIKILINK.sub(repl, body)
        rewritten += n
        dest = out_dir / fm["_rel"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f"---{head}\n---{body}" if head is not None else body)

    assets = vault_dir / "assets"
    if assets.is_dir():
        shutil.copytree(assets, out_dir / "assets")
    regenerate(out_dir)
    return {"concepts": len(concepts), "links_rewritten": rewritten, "out": out_dir}


def visualize(bundle_dir: Path, out: Path, name: str = "vault") -> Path:
    """Render OKF's bundled viewer (a self-contained HTML graph) with the
    reference implementation's own code, which lives in its own venv so
    google-adk never enters this one. OZU_OKF_HOME points at the checkout."""
    home = Path(os.environ.get("OZU_OKF_HOME", "~/okf")).expanduser()
    python = home / ".venv" / "bin" / "python"
    if not python.exists():
        raise SystemExit(
            f"OKF reference implementation not found at {home} — install with:\n"
            "  gh repo clone GoogleCloudPlatform/open-knowledge-format ~/okf\n"
            "  cd ~/okf && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'")
    subprocess.run([str(python), "-m", "reference_agent", "visualize",
                    "--bundle", str(bundle_dir), "--out", str(out), "--name", name],
                   check=True, cwd=home)
    return out
