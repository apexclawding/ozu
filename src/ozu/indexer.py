"""Rebuild the SQLite index from the Markdown vault, and the vault's own
derived views with it.

Run `ozu index` any time — after hand-editing notes, restoring from git, or
if ozu.db is lost. The vault is the source of truth; this derives it.
Wikilinks [[note-id]] in bodies become edges alongside frontmatter `related`,
each carrying the reason the note's `related_why` gives for it.

`index_file` is the one door into the derived tables: every writer calls it
after writing its note, and `rebuild` is that same call over every file. So
a note indexed the moment it was written and the same note after a rebuild
are the same rows — nothing a writer knows can live only in the db.

The same pass regenerates `index.md` (every folder) and `log.md` from the
notes' frontmatter. That makes the VPS the single writer of both views:
other producers (a capture bridge, a script on another machine) only ever add or edit
notes, and the next `ozu index` — every run, every sync — lists them.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .config import canonical_topic, get_config
from . import db, okf

log = logging.getLogger("ozu.indexer")

INDEXED_HEAD = "indexed_head"   # meta: the vault commit the db was last rebuilt from
WIKILINK = okf.WIKILINK
_parse_frontmatter = okf.parse_frontmatter     # readers across the pipeline import it here


def index_file(con, path: Path, *, vault: str = "personal",
               source_page: int | None = None) -> str | None:
    """Derive one note's rows — note, search text, outgoing edges, Build row —
    from its file. Does not commit: writers commit after, `rebuild` once at
    the end. Returns the note id, or None for a file without one."""
    fm, body = _parse_frontmatter(path.read_text())
    note_id = fm.get("id")
    if not note_id:
        return None
    note_id = str(note_id)
    topic = canonical_topic(path.parent.name)
    tags = fm.get("tags", []) if isinstance(fm.get("tags"), list) else []
    db.upsert_note(
        con, note_id=note_id, vault=vault, path=str(path),
        title=str(fm.get("title", note_id)), topic=topic,
        summary=okf.description_of(fm), tags=[str(t) for t in tags],
        created=str(fm.get("created", "")), body=body,
        needs_review=str(fm.get("needs_review", "false")).lower() == "true",
        source_page=source_page,
        claude=str(fm.get("claude", "")),
        research=str(fm.get("research", "")),
        learn=str(fm.get("learn", "")),
        commit=False,
    )
    con.execute("DELETE FROM edges WHERE src=?", (note_id,))
    if vault == "personal":   # governance: walled notes never enter the graph
        related = fm.get("related", []) if isinstance(fm.get("related"), list) else []
        why = okf.related_why(fm)
        for rid in sorted(set(str(r) for r in related) | set(WIKILINK.findall(body))):
            if rid != note_id:
                db.add_edge(con, note_id, rid, reason=why.get(rid) or why.get("*"),
                            commit=False)
        if fm.get("build"):
            # a Build note: its frontmatter is the ledger, this row
            # the derived index (builds.py re-derives it on writes)
            from . import builds
            builds.derive_row(con, path, commit=False)
    return note_id


def rebuild() -> int:
    """Every derived table, from the files, in one transaction: a reader
    (the MCP server) sees the old graph or the new one, never an empty one
    half-way through."""
    cfg = get_config()
    con = db.connect(cfg.db_path)
    # the page a note was read from is not in the file; carry it across
    pages = {r["id"]: r["source_page"] for r in
             con.execute("SELECT id, source_page FROM notes WHERE source_page IS NOT NULL")}
    con.execute("DELETE FROM notes")
    con.execute("DELETE FROM notes_fts")
    con.execute("DELETE FROM edges")
    con.execute("DELETE FROM builds")   # derived from Build-note frontmatter

    count = 0
    try:
        for vault_name, vault_dir in (("personal", cfg.vault_dir), ("work", cfg.work_vault_dir)):
            if not vault_dir.exists():
                continue
            for path in okf.concept_files(vault_dir):
                note_id = index_file(con, path, vault=vault_name)
                if note_id:
                    if note_id in pages:
                        con.execute("UPDATE notes SET source_page=? WHERE id=?",
                                    (pages[note_id], note_id))
                    count += 1
        con.commit()
    except BaseException:
        con.rollback()      # the old graph stays served
        raise

    # the walled vault is not an OKF bundle and gets no generated views;
    # it is a backstop, not a knowledge base anyone browses
    if cfg.vault_dir.exists():
        changed = okf.regenerate(cfg.vault_dir)
        if changed:
            log.info("regenerated %d view(s): %s", len(changed),
                     ", ".join(p.relative_to(cfg.vault_dir).as_posix() for p in changed))
            # Commit what this pass changed. A rebuild runs right after
            # `sync_from_origin` folds offsite notes in; if the fresh
            # index.md sat uncommitted, the *next* cycle's
            # `git pull --rebase` would refuse ("unstaged changes") and
            # the vault would stop syncing until something else wrote.
            from .vault import _git_commit
            _git_commit(cfg.vault_dir, f"index: regenerate {len(changed)} derived view(s)")
        head = _head(cfg.vault_dir)
        if head:
            db.set_meta(con, INDEXED_HEAD, head)
    log.info("indexed %d note(s)", count)
    return count


def _git(vault_dir: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=vault_dir, capture_output=True,
                          text=True, timeout=60)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _head(vault_dir: Path) -> str:
    if not (vault_dir / ".git").exists():
        return ""
    return _git(vault_dir, "rev-parse", "HEAD")


def catch_up() -> bool:
    """Bring the db up to whatever the vault holds now, whoever changed it.

    Called at the end of every tick and run, under the pipeline lock. The
    pipeline commits everything it writes, so anything still uncommitted
    here is someone else's (a hand edit, another program caught mid-write)
    and is committed first; a dirty tree would also make the next
    `git pull --rebase` refuse. Then, if HEAD is not the commit the db was
    last rebuilt from, rebuild. An idle tick costs one `git status` and one
    `rev-parse`. A vault that is not a git repository is left alone.
    Returns True when it rebuilt."""
    cfg = get_config()
    vault_dir = cfg.vault_dir
    if not (vault_dir / ".git").exists():
        return False
    dirty = _git(vault_dir, "status", "--porcelain")
    if dirty:
        from .vault import _git_commit
        n = len(dirty.splitlines())
        log.info("vault: %d path(s) changed outside the pipeline; committing", n)
        _git_commit(vault_dir, f"vault: hand edits ({n} path(s))")
    head = _head(vault_dir)
    con = db.connect(cfg.db_path)
    if not head or head == db.get_meta(con, INDEXED_HEAD):
        return False
    rebuild()
    return True
