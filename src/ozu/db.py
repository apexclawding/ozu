"""SQLite persistence: page dedup queue, note index (FTS5), and the edge table.

The Markdown vault is the source of truth; everything here is derived and
rebuildable via `ozu index`.
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    sha256 TEXT UNIQUE NOT NULL,
    pdf_path TEXT NOT NULL,
    notebook TEXT NOT NULL,
    page_no INTEGER NOT NULL,
    image_path TEXT NOT NULL,
    ocr_text TEXT,                            -- Scribe's own text layer, if any
    kind TEXT NOT NULL DEFAULT 'ink',        -- ink | annotation (a "<brand> " doc back)
    status TEXT NOT NULL DEFAULT 'new',      -- new | processed | error
    error TEXT,
    created_at REAL NOT NULL,
    processed_at REAL
);

CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,                      -- YYYYMMDD-slug
    vault TEXT NOT NULL,                      -- personal | work
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    topic TEXT NOT NULL,
    summary TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',            -- comma-separated
    created TEXT NOT NULL,
    needs_review INTEGER NOT NULL DEFAULT 0,
    claude TEXT NOT NULL DEFAULT '',          -- '' | requested | done | error
    research TEXT NOT NULL DEFAULT '',        -- '' | requested | done | error
    learn TEXT NOT NULL DEFAULT '',           -- '' | requested | done | error | skipped
    source_page INTEGER REFERENCES pages(id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    id UNINDEXED, title, body, tags, tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS edges (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'related',     -- related|supports|contradicts|prerequisite|evolved-into
    reason TEXT,
    PRIMARY KEY (src, dst, type)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY,
    note_id TEXT NOT NULL REFERENCES notes(id),
    text TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);

-- ✓ receipts: every checkmark's outcome, carried to the owner in the next
-- return mail. Like `mailed`, this is a ledger `ozu index` cannot rebuild.
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY,
    line TEXT NOT NULL,                       -- the handwritten ✓ text
    closed TEXT,                              -- action it closed; NULL = miss
    candidates TEXT NOT NULL DEFAULT '',      -- nearest open actions at ✓ time
    created REAL NOT NULL,
    reported INTEGER NOT NULL DEFAULT 0,      -- flipped when a mail carries it
    kind TEXT NOT NULL DEFAULT ''             -- '' | unseen: read as ticked, no ink beside it
                                              --   | unread: ink beside it, not read (ink.reconcile)
);

-- Builds: DERIVED index of the Build notes' frontmatter (topics/<project>/
-- B-*.md). Rebuilt by `ozu index` and re-derived by builds.py after every
-- write; never the source of truth — the Markdown is (invariant 1/7).
CREATE TABLE IF NOT EXISTS builds (
    id TEXT PRIMARY KEY,                      -- B-yymmdd-4hex
    path TEXT NOT NULL,
    topic TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,                     -- proposed|revised|approved|executing|done|error|dropped
    rev INTEGER NOT NULL DEFAULT 1,
    model TEXT,                               -- opus|sonnet|fable, recorded at approval
    branch TEXT,
    pr TEXT,
    pending TEXT,                             -- ''|revise: work queued for the builds phase
    source_note TEXT,
    updated TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,      -- lifetime executions launched (frontmatter build_attempts)
    deferrals INTEGER NOT NULL DEFAULT 0      -- lifetime launches a spent usage window turned away
);

-- Build events: one-line outcomes for the next Ozu Mail (a drop confirmed, a
-- second @approved refused, an unmatchable page, a failed run). A LEDGER like
-- `receipts` — `ozu index` cannot rebuild it and must not drop it.
CREATE TABLE IF NOT EXISTS build_events (
    id INTEGER PRIMARY KEY,
    build_id TEXT,                            -- NULL when no build could be named
    line TEXT NOT NULL,
    created REAL NOT NULL,
    reported INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'mail'         -- mail: the owner reads it. trace: the run ledger, never printed
);

-- The "<brand> Learn" ledger: which learning note went out on which evening's
-- sheet, and at which spacing stage — 0 the first time, then 1/2/3 for the
-- +2/+7/+30-day returns. Stage -1 records that a skipped transcript (too
-- long, no captions) was reported once. Like `mailed`, a row is written only
-- after the send succeeded, and `ozu index` cannot rebuild it.
CREATE TABLE IF NOT EXISTS learn_sheet (
    note_id TEXT NOT NULL,
    stage INTEGER NOT NULL,
    sent_on TEXT NOT NULL,                    -- ISO date of the sheet
    PRIMARY KEY (note_id, stage)
);

-- Every document that reached the device, by the name the library shows:
-- the ledger the "Safe to delete" list is computed from (edition.py). A
-- row is written only after Send-to-Kindle accepted the file. `dead_on` is
-- the day the list first named it. A LEDGER: `ozu index` must not drop it.
CREATE TABLE IF NOT EXISTS delivered (
    name TEXT PRIMARY KEY,                    -- file stem, e.g. "<brand> Morning 2026-09-11"
    kind TEXT NOT NULL,                       -- morning|evening|extra|build|research|simulate|mail|learn|digest|result
    sent_on TEXT NOT NULL,                    -- ISO date
    build_id TEXT,                            -- build/result/simulate rows
    rev INTEGER,                              -- build rows
    dead_on TEXT,
    reason TEXT
);

-- The wiki ledger (wiki.py): every note that has been offered to a wiki run,
-- and what became of it — the concept pages that now cite it, `ignored: …`
-- with the run's reason, `unplaced` when the run said nothing about it, or
-- `error` when the run failed (`ozu wiki requeue` offers those again). The
-- pages themselves are Markdown; this only remembers what was already read.
-- A LEDGER: `ozu index` must not drop it.
CREATE TABLE IF NOT EXISTS wiki_fed (
    note_id TEXT PRIMARY KEY,
    fed_on TEXT NOT NULL,                     -- ISO date of the run
    outcome TEXT NOT NULL
);

"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """Additive column migrations. `pages` is the dedup ledger and is the one
    table `ozu index` cannot rebuild from the vault, so it is migrated in
    place rather than dropped."""
    have = {r["name"] for r in con.execute("PRAGMA table_info(pages)")}
    if "ocr_text" not in have:
        con.execute("ALTER TABLE pages ADD COLUMN ocr_text TEXT")
        con.commit()
    if "kind" not in have:
        con.execute("ALTER TABLE pages ADD COLUMN kind TEXT NOT NULL DEFAULT 'ink'")
        con.commit()
    # notes is rebuildable, but migrating in place spares a forced
    # `ozu index` on deploy.
    have_notes = {r["name"] for r in con.execute("PRAGMA table_info(notes)")}
    if "claude" not in have_notes:
        con.execute("ALTER TABLE notes ADD COLUMN claude TEXT NOT NULL DEFAULT ''")
        con.commit()
    if "research" not in have_notes:
        con.execute("ALTER TABLE notes ADD COLUMN research TEXT NOT NULL DEFAULT ''")
        con.commit()
    if "learn" not in have_notes:
        con.execute("ALTER TABLE notes ADD COLUMN learn TEXT NOT NULL DEFAULT ''")
        con.commit()
    # builds derives from frontmatter, but the attempt budget has to be
    # readable before `ozu index` next runs — a live box upgrading mid-loop
    # must not lose the counter that stops a relaunch.
    have_builds = {r["name"] for r in con.execute("PRAGMA table_info(builds)")}
    for col, decl in (("attempts", "INTEGER NOT NULL DEFAULT 0"),
                      ("deferrals", "INTEGER NOT NULL DEFAULT 0")):
        if col not in have_builds:
            con.execute(f"ALTER TABLE builds ADD COLUMN {col} {decl}")
            con.commit()
    # build_events is a LEDGER (invariant 1): migrated in place, never
    # dropped. Rows written before this column existed were all mail.
    have_events = {r["name"] for r in con.execute("PRAGMA table_info(build_events)")}
    if "kind" not in have_events:
        con.execute("ALTER TABLE build_events ADD COLUMN kind TEXT NOT NULL "
                    "DEFAULT 'mail'")
        con.commit()
    # receipts is a LEDGER too. Every row before this column was a plain
    # close-or-miss, which is what '' means.
    have_receipts = {r["name"] for r in con.execute("PRAGMA table_info(receipts)")}
    if "kind" not in have_receipts:
        con.execute("ALTER TABLE receipts ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
        con.commit()


def get_meta(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(con, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))
    con.commit()


# ---------- pages / dedup ----------

def page_seen(con: sqlite3.Connection, sha256: str) -> bool:
    return con.execute("SELECT 1 FROM pages WHERE sha256=?", (sha256,)).fetchone() is not None


def enqueue_page(con, sha256: str, pdf_path: str, notebook: str, page_no: int,
                 image_path: str, ocr_text: str = "", kind: str = "ink",
                 status: str = "new", error: str | None = None) -> int:
    """One page in the ledger. `status='skipped'` files it as a tombstone that
    is never read and never re-ingested — the ink detector (D-09) uses it for
    a returning page that carries no handwriting, and a purge uses it for a
    page that must never come back."""
    cur = con.execute(
        "INSERT INTO pages (sha256, pdf_path, notebook, page_no, image_path, ocr_text, kind, status, error, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sha256, pdf_path, notebook, page_no, image_path, ocr_text, kind,
         status, error, time.time()),
    )
    con.commit()
    return cur.lastrowid


def pending_pages(con, limit: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM pages WHERE status='new' ORDER BY id LIMIT ?", (limit,)
    ).fetchall()


def mark_page(con, page_id: int, status: str, error: str | None = None) -> None:
    con.execute(
        "UPDATE pages SET status=?, error=?, processed_at=? WHERE id=?",
        (status, error, time.time(), page_id),
    )
    con.commit()


# ---------- notes index ----------

def upsert_note(con, *, note_id: str, vault: str, path: str, title: str, topic: str,
                summary: str, tags: list[str], created: str, body: str,
                needs_review: bool, source_page: int | None,
                claude: str = "", research: str = "", learn: str = "",
                commit: bool = True) -> None:
    con.execute(
        "INSERT OR REPLACE INTO notes (id, vault, path, title, topic, summary, tags, created,"
        " needs_review, claude, research, learn, source_page)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (note_id, vault, path, title, topic, summary, ",".join(tags), created,
         int(needs_review), claude, research, learn, source_page),
    )
    con.execute("DELETE FROM notes_fts WHERE id=?", (note_id,))
    con.execute(
        "INSERT INTO notes_fts (id, title, body, tags) VALUES (?,?,?,?)",
        (note_id, title, body, " ".join(tags)),
    )
    if commit:
        con.commit()


def upsert_build(con, *, build_id: str, path: str, topic: str, title: str,
                 status: str, rev: int, model: str | None, branch: str | None,
                 pr: str | None, pending: str, source_note: str | None,
                 updated: str, attempts: int = 0, deferrals: int = 0,
                 commit: bool = True) -> None:
    """One derived row per Build note, re-derived from its frontmatter after
    every write and by `ozu index` — the file is the truth."""
    con.execute(
        "INSERT OR REPLACE INTO builds (id, path, topic, title, status, rev,"
        " model, branch, pr, pending, source_note, updated, attempts,"
        " deferrals)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (build_id, path, topic, title, status, rev, model, branch, pr,
         pending, source_note, updated, attempts, deferrals))
    if commit:
        con.commit()


def add_build_event(con, build_id: str | None, line: str,
                    kind: str = "mail") -> None:
    """One line about a Build. `kind='mail'` is read by a human in the next
    edition; `kind='trace'` is the run ledger — every launch and every
    outcome, recorded so a died run leaves something behind, and never
    printed (116 relaunches would have been 116 lines of paper)."""
    con.execute(
        "INSERT INTO build_events (build_id, line, created, kind)"
        " VALUES (?,?,?,?)",
        (build_id, line, time.time(), kind))
    con.commit()


def unreported_build_events(con) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM build_events WHERE reported=0 AND kind='mail'"
        " ORDER BY id").fetchall()


def build_trace(con, build_id: str) -> list[sqlite3.Row]:
    """Everything recorded about one Build's runs, oldest first."""
    return con.execute(
        "SELECT * FROM build_events WHERE build_id=? ORDER BY id",
        (build_id,)).fetchall()


def mark_build_events_reported(con, ids: list[int]) -> None:
    con.executemany("UPDATE build_events SET reported=1 WHERE id=?",
                    [(i,) for i in ids])
    con.commit()


def add_edge(con, src: str, dst: str, type_: str = "related", reason: str | None = None,
             commit: bool = True) -> None:
    con.execute(
        "INSERT OR IGNORE INTO edges (src, dst, type, reason) VALUES (?,?,?,?)",
        (src, dst, type_, reason),
    )
    if commit:
        con.commit()


def add_action(con, note_id: str, text: str) -> None:
    con.execute(
        "INSERT INTO actions (note_id, text, created) VALUES (?,?,?)",
        (note_id, text, time.time()),
    )
    con.commit()


_STOPWORDS = {"the", "and", "for", "with", "that", "this"}

# Irregular pasts the prefix rule can't see: "bought" must close "Buy …".
_IRREGULAR = {
    "bought": "buy", "brought": "bring", "built": "build", "caught": "catch",
    "chose": "choose", "found": "find", "gave": "give", "got": "get",
    "held": "hold", "kept": "keep", "left": "leave", "lost": "lose",
    "made": "make", "met": "meet", "paid": "pay", "ran": "run",
    "said": "say", "sat": "sit", "saw": "see", "sent": "send",
    "sold": "sell", "spent": "spend", "spoke": "speak", "taught": "teach",
    "thought": "think", "told": "tell", "took": "take", "won": "win",
    "wrote": "write",
}

# A ✓ line must cover this share of the action's own words to close it.
_CLOSE_THRESHOLD = 0.6


def _action_tokens(s: str) -> set[str]:
    return {_IRREGULAR.get(w, w) for w in re.findall(r"[a-z0-9]+", s.lower())
            if len(w) > 2 and w not in _STOPWORDS}


def _tokens_match(a: str, b: str) -> bool:
    # inflection-tolerant: "checked" closes "check", "cars" closes "car"
    return a == b or a.startswith(b) or b.startswith(a)


def _rank_actions(con, text: str, done: int = 0) -> list[tuple[float, int, str]]:
    """Actions scored by how much of their own wording the ✓ line covers,
    best first. Zero-overlap actions are not candidates at all."""
    want = _action_tokens(text)
    ranked = []
    if not want:
        return ranked
    for r in con.execute("SELECT id, text FROM actions WHERE done=?", (done,)):
        have = _action_tokens(r["text"])
        if not have:
            continue
        hit = sum(1 for h in have if any(_tokens_match(h, w) for w in want))
        if hit:
            ranked.append((hit / len(have), r["id"], r["text"]))
    ranked.sort(key=lambda t: -t[0])
    return ranked


def matches_closed_action(con, text: str) -> bool:
    """True when a ✓ line points at an action that is already done — the
    normal case when an annotated document is re-exported and its old ticks
    are read a second time. Such a line is a re-read, not a miss."""
    ranked = _rank_actions(con, text, done=1)
    return bool(ranked) and ranked[0][0] >= _CLOSE_THRESHOLD


def close_matching_action(con, text: str) -> str | None:
    """Close the open action best matching a handwritten ✓ line. Fuzzy on
    purpose: '✓ serviced the bikes' must close
    "Service the bike". Returns the closed action's text, or None.

    The score is coverage of the action's own words, not of the ✓ line's:
    a completion naturally carries detail the action never had ("Bought the
    drill, it's the 18V one with two batteries" must close "Buy a drill" — found
    live 2026-08-24, when dividing by the longer side scored it 0.33)."""
    ranked = _rank_actions(con, text)
    if ranked and ranked[0][0] >= _CLOSE_THRESHOLD:
        _, action_id, action_text = ranked[0]
        con.execute("UPDATE actions SET done=1 WHERE id=?", (action_id,))
        con.commit()
        return action_text
    return None


def add_receipt(con, line: str, closed: str | None, kind: str = "") -> None:
    """Ledger a ✓ outcome for the next return mail. For a miss, the nearest
    open actions are captured now — the ledger the ✓ actually missed
    against, not whatever is open by mail time. A `kind` receipt (the ink
    check disagreed with the reader, `ink.reconcile`) names a printed line
    that is itself the open action, so it carries no candidates."""
    candidates = "" if closed or kind else "\n".join(
        t for _, _, t in _rank_actions(con, line)[:3])
    con.execute(
        "INSERT INTO receipts (line, closed, candidates, created, kind)"
        " VALUES (?,?,?,?,?)",
        (line, closed, candidates, time.time(), kind),
    )
    con.commit()


def unreported_receipts(con) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM receipts WHERE reported=0 ORDER BY id").fetchall()


def mark_receipts_reported(con, ids: list[int]) -> None:
    con.executemany("UPDATE receipts SET reported=1 WHERE id=?",
                    [(i,) for i in ids])
    con.commit()


def search_notes(con, query: str, vault: str = "personal", limit: int = 10) -> list[sqlite3.Row]:
    return con.execute(
        """SELECT n.id, n.title, n.path, n.topic, n.summary, n.tags,
                  snippet(notes_fts, 2, '»', '«', ' … ', 18) AS snippet
           FROM notes_fts f JOIN notes n ON n.id = f.id
           WHERE notes_fts MATCH ? AND n.vault = ?
           ORDER BY rank LIMIT ?""",
        (query, vault, limit),
    ).fetchall()


def neighbors(con, note_id: str, depth: int = 1, edge_type: str | None = None,
              vault: str | None = None) -> list[dict]:
    """Edges around a note, `depth` hops out. With `vault`, the walk is fenced
    allow-list style: it starts only from a note stored with exactly that
    label, and drops every edge whose other end is a note with any other
    label (so it never crosses into one). An id that names no note at all
    is a dangling link and is returned as before."""
    if vault is not None:
        row = con.execute("SELECT vault FROM notes WHERE id=?", (note_id,)).fetchone()
        if not row or row["vault"] != vault:
            return []
    seen: set[str] = {note_id}
    frontier = {note_id}
    out: list[dict] = []
    for _ in range(max(1, depth)):
        if not frontier:
            break
        qmarks = ",".join("?" * len(frontier))
        params: list = list(frontier) + list(frontier)
        type_clause = ""
        if edge_type:
            type_clause = " AND type=?"
            params.append(edge_type)
        rows = con.execute(
            f"SELECT src, dst, type, reason FROM edges"
            f" WHERE (src IN ({qmarks}) OR dst IN ({qmarks})){type_clause}",
            params,
        ).fetchall()
        if vault is not None:
            ends = {nid for r in rows for nid in (r["src"], r["dst"])}
            qm = ",".join("?" * len(ends))
            fenced = {r["id"] for r in con.execute(
                f"SELECT id FROM notes WHERE id IN ({qm}) AND vault != ?",
                [*ends, vault]).fetchall()} if ends else set()
            rows = [r for r in rows if r["src"] not in fenced and r["dst"] not in fenced]
        nxt: set[str] = set()
        for r in rows:
            out.append(dict(r))
            for nid in (r["src"], r["dst"]):
                if nid not in seen:
                    seen.add(nid)
                    nxt.add(nid)
        frontier = nxt
    return out
