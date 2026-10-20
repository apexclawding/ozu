"""Vault writer.

Writes atomic Markdown notes with OKF v0.2 frontmatter (see okf.py), keeps the
derived views (`index.md` per folder, `log.md`) regenerated, mirrors
everything into the SQLite index, and enforces the governance split:
work-notebook or work_flagged content goes to the work vault and never enters the
personal index or edges.

A note's filename is its id. Every writer here funnels through `_emit`, so
the four note kinds (page, @claude companion, @research brief, learning
brief) carry the same contract: `type`, `description`, `generated` and
`sources` alongside the pipeline's own keys.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import shutil
import subprocess
from pathlib import Path

from .config import canonical_topic, get_config
from . import db, indexer, okf

log = logging.getLogger("ozu.vault")


def scaffold(vault_dir: Path) -> None:
    vault_dir.mkdir(parents=True, exist_ok=True)
    (vault_dir / "topics").mkdir(exist_ok=True)
    (vault_dir / "people").mkdir(exist_ok=True)
    (vault_dir / "assets").mkdir(exist_ok=True)
    okf.write_guide(vault_dir)
    if not (vault_dir / "index.md").exists():
        okf.regenerate(vault_dir)
    if not (vault_dir / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=vault_dir, check=False)


def _slugify(s: str, *, suffix: str = "") -> str:
    """Slug capped at 60 chars. A `suffix` is reserved *before* truncating,
    never appended after: a marker that only survives on short titles is
    worse than none, because the id it collides with is the note's own
    source (see `write_learning_note`)."""
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return (s[:60 - len(suffix)].rstrip("-") or "note") + suffix


def _unique_id(vault_dir: Path, topic_dir: Path, slug: str, date: str) -> tuple[str, Path]:
    """Id and path together: the file is named after the id, so a collision
    on either is a collision on both."""
    base = f"{date.replace('-', '')}-{slug}"
    note_id, n = base, 2
    while (topic_dir / f"{note_id}.md").exists():
        note_id = f"{base}-{n}"
        n += 1
    return note_id, topic_dir / f"{note_id}.md"


def _topic_dir(vault_dir: Path, topic: str) -> Path:
    topic = canonical_topic(topic)
    d = vault_dir / ("people" if topic == "people" else f"topics/{topic}")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _emit(vault_dir: Path, path: Path, *, fields: dict, body: str,
          generated_by: str, sources: list[dict] | None = None,
          needs_review: bool = False, regenerate: bool = True) -> str:
    """Write one concept file: the caller's ordered fields, then OKF's
    provenance tail. `type` is derived from `source`, `description` is the
    index line. Regenerates index.md/log.md unless the caller batches."""
    fm = {"type": okf.type_for(str(fields.get("source", "")))}
    fm.update(fields)
    fm["needs_review"] = bool(needs_review)
    if needs_review:
        fm["status"] = "draft"        # OKF lifecycle: not yet reviewed
    fm["generated"] = {"by": generated_by, "at": okf.now_iso()}
    if sources:
        fm["sources"] = sources
    title, desc = fm["title"], fm["description"]
    path.write_text(f"{okf.dump_frontmatter(fm)}\n# {title}\n\n> Summary: {desc}\n\n{body}\n")
    if regenerate:
        okf.regenerate(vault_dir)
    return str(path.relative_to(vault_dir))


def _note_source(source: dict, sid: str = "source") -> list[dict]:
    """OKF `sources` entry pointing at the vault note a run started from —
    a bundle-relative path, so lineage is a link a consumer can follow."""
    cfg = get_config()
    try:
        rel = "/" + Path(source["path"]).resolve().relative_to(cfg.vault_dir.resolve()).as_posix()
    except (KeyError, ValueError):
        rel = f"/topics/{canonical_topic(source.get('topic', 'ideas'))}/{source['id']}.md"
    return [{"id": sid, "resource": rel, "title": source.get("title") or source["id"]}]


def write_notes(result: dict, *, notebook: str, page_row_id: int,
                image_path: Path, needs_review: bool,
                related_build: str | None = None) -> list[str]:
    """Write every atomic note from one analyzed page. Returns note ids.

    `related_build`: the Build this page was matched to, when it is an
    annotated "<brand> Build" document — margin notes then link to the Build note
    so the conversation's marginalia stay attached to it."""
    cfg = get_config()
    is_work = bool(result.get("work_flag")) or notebook.lower().startswith(cfg.work_notebook_prefixes)
    vault_dir = cfg.work_vault_dir if is_work else cfg.vault_dir
    if is_work and not vault_dir.exists():
        # work-vault is not created by `init` (see pipeline.py). If a work page
        # ever arrives anyway, scaffold the walled vault on the spot rather
        # than letting the content leak into vault.
        log.warning("work-flagged content reached the pipeline; scaffolding %s", vault_dir)
        scaffold(vault_dir)
    vault_name = "work" if is_work else "personal"
    scaffold(vault_dir)

    today = dt.date.today().isoformat()
    con = db.connect(cfg.db_path)

    # keep the ink: copy the page image into the owning vault's assets
    asset_dir = vault_dir / "assets" / today[:7]
    asset_dir.mkdir(parents=True, exist_ok=True)
    asset_path = asset_dir / image_path.name
    if not asset_path.exists():
        shutil.copy2(image_path, asset_path)

    # a page read by the vision pass; `capture` (the MCP tool) is typed text
    # from a Claude conversation and says so
    captured = notebook == "claude-capture"
    by = "ozu/capture" if captured else okf.actor("ozu", cfg.model)
    page_no = result.get("page_no", "?")
    page_source = None if captured else [{
        "id": "page",
        "resource": "/" + asset_path.relative_to(vault_dir).as_posix(),
        "title": f"{notebook} p{page_no}",
    }]

    ids: list[str] = []
    for note in result.get("notes", []):
        topic = canonical_topic(_slugify(note.get("topic") or "ideas"))
        topic_dir = _topic_dir(vault_dir, topic)
        slug = _slugify(note.get("slug") or note.get("title") or "note")
        note_id, path = _unique_id(vault_dir, topic_dir, slug, today)
        title = note.get("title") or slug
        summary = (note.get("summary") or "").strip() or title
        tags = [_slugify(t) for t in note.get("tags", [])][:8]
        related = [r for r in note.get("related", []) if isinstance(r, str)]
        body = (note.get("body") or "").strip()
        if result.get("drawing_description") and len(result.get("notes", [])) == 1:
            body += f"\n\n## Drawing\n{result['drawing_description']}"
        # directives are inert behind the wall, like edges and actions below
        claude_req = bool(note.get("claude_request")) and vault_name == "personal"
        research_req = bool(note.get("research_request")) and vault_name == "personal"
        # A page-level propose directive turns @claude from the old one-shot
        # run into a Build proposal: the note queues as `build-requested`,
        # which the builds phase picks up and agent.work_pending (which
        # selects only 'requested') can never fire — one grammar, one path.
        proposing = (result.get("build_directive") or {}).get("kind") == "propose"
        claude_status = ("build-requested" if claude_req and proposing
                         else "requested" if claude_req else "")
        reasons = [r.strip() for r in (note.get("related_reason") or "").split(";")]
        why = {rid: reasons[i] for i, rid in enumerate(related)
               if i < len(reasons) and reasons[i]}
        if related_build and related_build not in related:
            related = related + [related_build]
            why[related_build] = "margin note on this Build"

        fields = dict(
            id=note_id, title=title, description=summary, tags=tags,
            created=today, updated=today,
            source=f"scribe:{notebook}/p{page_no}",
            image=asset_path.relative_to(vault_dir).as_posix(),
            related=related,
            related_why=why or None,
            **({"claude": claude_status} if claude_status else {}),
            **({"research": "requested"} if research_req else {}),
        )
        rel_path = _emit(vault_dir, path, fields=fields, body=body, generated_by=by,
                         sources=page_source, needs_review=needs_review,
                         regenerate=False)   # once per page, below

        # the note's rows, edges included, derive from the file just written
        indexer.index_file(con, path, vault=vault_name, source_page=page_row_id)
        con.commit()
        if vault_name == "personal":  # governance: work notes never enter the graph
            for action in note.get("actions", []):
                db.add_action(con, note_id, action)
        ids.append(note_id)
        log.info("wrote %s → %s", note_id, rel_path)

    okf.regenerate(vault_dir)
    _git_commit(vault_dir, f"pipeline: {len(ids)} note(s) from {notebook}")
    return ids


def sync_from_origin(vault_dir: Path) -> bool:
    """Fold offsite edits (phone Obsidian, GitHub web) into the working vault.
    Returns True when anything arrived — the caller must then reindex, since
    the db is derived from the files. On conflict the VPS copy stays truth:
    the rebase is aborted and the run continues on local state, so the
    offsite edit is never lost — it waits on GitHub, and the nightly push
    will refuse (leading '-' in the unit tolerates that) until a later sync
    or a human reconciles."""
    if not (vault_dir / ".git").exists():
        return False
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=vault_dir,
                          capture_output=True, text=True).stdout.strip()
    proc = subprocess.run(["git", "pull", "--rebase", "--quiet", "origin", "main"],
                          cwd=vault_dir, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        subprocess.run(["git", "rebase", "--abort"], cwd=vault_dir,
                       capture_output=True)
        log.warning("vault sync failed; continuing on local state: %s",
                    proc.stderr.strip()[:300])
        return False
    new_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=vault_dir,
                              capture_output=True, text=True).stdout.strip()
    if new_head != head:
        log.info("vault sync: offsite edits folded in (%s -> %s)",
                 head[:8], new_head[:8])
    return new_head != head


def set_directive_status(note_path: Path, status: str, key: str = "claude") -> None:
    """Update a directive's frontmatter line (`claude:` or `research:`) in
    place. The vault is the ledger for runs — the db column derives from
    this line, so a rebuilt db can never re-run a finished note."""
    text = note_path.read_text()
    end = text.find("\n---", 3)
    if not text.startswith("---") or end == -1:
        log.warning("no frontmatter in %s; %s status not recorded", note_path, key)
        return
    head, tail = text[:end], text[end:]
    today = dt.date.today().isoformat()
    head = re.sub(r"^updated: .*$", f"updated: {today}", head, flags=re.M)
    if re.search(rf"^{key}: ", head, flags=re.M):
        head = re.sub(rf"^{key}: .*$", f"{key}: {status}", head, flags=re.M)
    else:
        head += f"\n{key}: {status}"
    note_path.write_text(head + tail)


def write_agent_note(*, source: dict, report: str, branch: str | None,
                     compare_url: str | None) -> str:
    """Companion note for a @claude work run, beside the source note. Its
    existence — like the source's `claude:` status — survives `ozu index`,
    so it is the run ledger as much as it is content."""
    cfg = get_config()
    con = db.connect(cfg.db_path)
    today = dt.date.today().isoformat()
    topic = canonical_topic(source["topic"])
    topic_dir = _topic_dir(cfg.vault_dir, topic)
    slug = _slugify(source["title"], suffix="-claude")
    note_id, path = _unique_id(cfg.vault_dir, topic_dir, slug, today)
    title = f"Claude run: {source['title']}"
    summary = (f"Claude built this on branch {branch} — review before merging."
               if branch else
               "Claude worked this idea but produced no commits — see report.")

    lines = [f"Worked from [[{source['id']}]]."]
    if branch:
        lines.append(f"\n**Branch:** `{branch}`")
    if compare_url:
        lines.append(f"**Review:** {compare_url}")
    body = ("\n".join(lines)
            + f"\n\n## Report\n\n{report.strip() or '(the agent returned no report)'}")

    rel_path = _emit(cfg.vault_dir, path, body=body, generated_by="ozu/claude-code",
                     fields=dict(id=note_id, title=title, description=summary,
                                 tags=["claude-work", topic], created=today, updated=today,
                                 source=f"claude:{source['id']}", related=[source["id"]],
                                 related_why="claude work run"),
                     sources=_note_source(source))
    indexer.index_file(con, path)
    con.commit()
    log.info("wrote %s → %s", note_id, rel_path)
    return note_id


def write_research_note(*, source: dict, report: str,
                        pdf_name: str | None) -> str:
    """Companion note for an @research run: the brief itself, linked to the
    ink that asked for it. Same ledger role as the @claude companion."""
    cfg = get_config()
    con = db.connect(cfg.db_path)
    today = dt.date.today().isoformat()
    topic = canonical_topic(source["topic"])
    topic_dir = _topic_dir(cfg.vault_dir, topic)
    slug = _slugify(source["title"], suffix="-research")
    note_id, path = _unique_id(cfg.vault_dir, topic_dir, slug, today)
    title = f"Research: {source['title']}"
    summary = ("Overnight research brief — delivered to the Scribe." if pdf_name
               else "Overnight research brief — in the vault (Scribe delivery not configured).")

    lines = [f"Researched from [[{source['id']}]]."]
    if pdf_name:
        lines.append(f"\n**Delivered as:** `{pdf_name}`")
    body = "\n".join(lines) + f"\n\n## Brief\n\n{report.strip() or '(no report returned)'}"

    rel_path = _emit(cfg.vault_dir, path, body=body, generated_by="ozu/claude-code",
                     fields=dict(id=note_id, title=title, description=summary,
                                 tags=["claude-research", topic], created=today, updated=today,
                                 source=f"research:{source['id']}", related=[source["id"]],
                                 related_why="research brief"),
                     sources=_note_source(source))
    indexer.index_file(con, path)
    con.commit()
    log.info("wrote %s → %s", note_id, rel_path)
    return note_id


_H1 = re.compile(r"^# ", re.M)
_WIKILINK = okf.WIKILINK

def _first_takeaway(report: str) -> str:
    """The first bullet under the Takeaways heading — the one-line summary a
    registry entry wants. Falls back to the first non-heading line."""
    in_takeaways, fallback = False, ""
    for line in report.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            in_takeaways = "takeaway" in s.lower()
            continue
        text = s[2:].strip() if s.startswith(("- ", "* ")) else s
        if in_takeaways:
            return text
        fallback = fallback or text
    return fallback



def write_learning_note(*, source: dict, report: str, video: dict) -> str:
    """Companion note for a learning run: the brief drawn from a pinned
    video's transcript, linked to the transcript note it came from. The
    transcript's `learn:` line is the run ledger; this note is the content
    the evening sheet reads, so channel/duration/url ride in its frontmatter
    and the sheet never has to open the transcript."""
    cfg = get_config()
    con = db.connect(cfg.db_path)
    today = dt.date.today().isoformat()
    topic = canonical_topic(source["topic"])
    topic_dir = _topic_dir(cfg.vault_dir, topic)
    slug = _slugify(source["title"], suffix="-learning")
    note_id, path = _unique_id(cfg.vault_dir, topic_dir, slug, today)
    title = f"Learning: {source['title']}"
    summary = (_first_takeaway(report) or f"Learning brief from {source['title']}")[:200]

    # an article (articles.py) names its site and reading time where a video
    # names its channel and length, so the sheet's line reads the same for both
    channel = str(video.get("channel") or video.get("site") or video.get("author") or "")
    duration = str(video.get("duration") or video.get("reading_time")
                   or (f"{video['pages']} pages" if video.get("pages") else "")).strip('"')
    meta = " · ".join(x for x in (f"[[{source['id']}]]", channel, duration) if x)
    body_lines = [f"From {meta}"]
    if video.get("url"):
        body_lines.append(str(video["url"]))
    # the brief's `#` headings sit under this note's H1 title, so demote them
    brief = _H1.sub("## ", (report or "").strip()) or "(no brief returned)"
    body = "\n".join(body_lines) + f"\n\n{brief}"

    # provenance: the transcript note (lineage inside the bundle) and the
    # video itself (the external leaf source)
    is_video = okf.source_kind(str(video.get("source") or "")) == "youtube" or bool(video.get("video_id"))
    sources = _note_source(source, sid="transcript" if is_video else "article")
    if video.get("url"):
        sources.append({"id": "video" if is_video else "web", "resource": str(video["url"]),
                        "title": source["title"], "author": channel or None})
    rel_path = _emit(cfg.vault_dir, path, body=body, generated_by="ozu/claude-code",
                     fields=dict(id=note_id, title=title, description=summary,
                                 tags=["learning", "claude-learn"], created=today, updated=today,
                                 source=f"learn:{source['id']}",
                                 video_id=str(video.get("video_id") or ""),
                                 channel=channel, duration=duration,
                                 url=str(video.get("url") or ""),
                                 pinned=str(video.get("created") or today),   # the day the video was pinned
                                 related=[source["id"]],
                                 # every [[id]] the brief cites is an edge too;
                                 # the reason rides in the file so a rebuild keeps it
                                 related_why={source["id"]: "learning brief", **{
                                     rid: "cited in learning brief" for rid in sorted(
                                         set(_WIKILINK.findall(brief)) - {note_id, source["id"]})}}),
                     sources=sources)
    indexer.index_file(con, path)
    con.commit()
    log.info("wrote %s → %s", note_id, rel_path)
    return note_id



def _git_commit(vault_dir: Path, msg: str) -> None:
    # A silently failed commit is worse than none: the tree stays dirty and
    # the next sync's `git pull --rebase` refuses, wedging the loop. The one
    # silent failure we have actually hit is a box with no git identity
    # (a bare CI runner), so fall back to a pipeline identity rather than
    # override one that exists.
    try:
        subprocess.run(["git", "add", "-A"], cwd=vault_dir, check=False,
                       capture_output=True)
        cmd = ["git"]
        ident = subprocess.run(["git", "config", "user.email"], cwd=vault_dir,
                               capture_output=True, text=True)
        if not ident.stdout.strip():
            cmd += ["-c", "user.name=ozu",
                    "-c", "user.email=ozu@localhost"]
        proc = subprocess.run(cmd + ["commit", "-q", "-m", msg], cwd=vault_dir,
                              check=False, capture_output=True, text=True)
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0 and "nothing to commit" not in out:
            log.warning("vault commit failed: %s", out.strip())
    except Exception:
        log.warning("git commit skipped", exc_info=True)
