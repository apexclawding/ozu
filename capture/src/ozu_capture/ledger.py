"""The ledger: a sha-keyed state machine in stdlib sqlite3.

States and their meaning (REQUIREMENTS FR-32):
    new        sha computed, file stable, nothing produced yet
    committed  note committed locally, push still owed
    pushed     pushed to origin, audio not yet tidied to processed/
    done       terminal: note in the vault, audio in processed/
    walled     terminal: transcript kept local-only (FR-25)
    no_speech  terminal: silent memo, no note (FR-12)
    failed     retryable: .failed.txt marker beside the audio is the gate;
               deleting the marker is the retry gesture (FR-11)

Losing this database must never duplicate a note — the vault-side
source_sha256 scan (FR-22) is the backstop, so the ledger is allowed to be
an optimization plus the resume pointer, not the only defence.

The observations table backs the quiesce gate (FR-04): the bridge is a
short-lived process per tick, so "unchanged since last look" has to be
remembered on disk.

The youtube source keeps its own table, keyed by video id (Amendment 1
§6.1 FR-02), with the same discipline: the vault-side `video_id:` scan is the
backstop, the ledger is the resume pointer. Its states:
    failed      transient: a fetch failed; `attempts` counts them
    committed   note committed locally, push still owed
    done        terminal: the note is in the vault
    handed_off  terminal here: audio + sidecar dropped for the Mac lane
`outcome` says what the note was: `transcript`, or `skipped` (too long, no
captions with the fallback off, three failed fetches) — a skipped stub is
still a committed-and-pushed note, so it walks the same states.
The `meta` table is a small key/value store for the poll clock and the
yt-dlp upgrade clock.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

TERMINAL = frozenset({"done", "walled", "no_speech"})
VIDEO_TERMINAL = frozenset({"done", "handed_off"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    sha        TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    state      TEXT NOT NULL,
    note_id    TEXT DEFAULT '',
    detail     TEXT DEFAULT '',
    first_seen TEXT NOT NULL,
    updated    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations(
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    observed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS videos(
    video_id   TEXT PRIMARY KEY,
    title      TEXT DEFAULT '',
    state      TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT DEFAULT '',
    note_id    TEXT DEFAULT '',
    outcome    TEXT DEFAULT '',
    first_seen TEXT NOT NULL,
    updated    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    _migrate(con)
    con.commit()
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS never adds columns to a table that already
    exists, so a db created before a schema gained a column must be altered
    in place (bit the Mac's ledger when videos grew the outcome column)."""
    have = {r["name"] for r in con.execute("PRAGMA table_info(videos)")}
    if "outcome" not in have:
        con.execute("ALTER TABLE videos ADD COLUMN outcome TEXT DEFAULT ''")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def get(con: sqlite3.Connection, sha: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM files WHERE sha=?", (sha,)).fetchone()


def upsert(con: sqlite3.Connection, sha: str, *, path: str, size: int,
           state: str, note_id: str = "", detail: str = "") -> None:
    now = _now()
    con.execute(
        """INSERT INTO files(sha, path, size, state, note_id, detail,
                             first_seen, updated)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(sha) DO UPDATE SET
             path=excluded.path, size=excluded.size, state=excluded.state,
             note_id=CASE WHEN excluded.note_id != '' THEN excluded.note_id
                          ELSE files.note_id END,
             detail=excluded.detail, updated=excluded.updated""",
        (sha, path, size, state, note_id, detail, now, now))
    con.commit()


def set_state(con: sqlite3.Connection, sha: str, state: str,
              detail: str = "") -> None:
    con.execute("UPDATE files SET state=?, detail=?, updated=? WHERE sha=?",
                (state, detail, _now(), sha))
    con.commit()


def rows_in_state(con: sqlite3.Connection, state: str) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM files WHERE state=? ORDER BY updated",
                       (state,)).fetchall()


def counts(con: sqlite3.Connection) -> dict[str, int]:
    return {r["state"]: r["c"] for r in con.execute(
        "SELECT state, COUNT(*) c FROM files GROUP BY state")}


def recent(con: sqlite3.Connection, n: int = 5) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM files ORDER BY updated DESC LIMIT ?", (n,)).fetchall()


def observe(con: sqlite3.Connection, path: str, size: int, mtime: float,
            now: float, quiesce_seconds: int) -> bool:
    """Record one look at a file. True only when (size, mtime) matches the
    previous look AND that look is at least quiesce_seconds old (FR-04)."""
    row = con.execute("SELECT * FROM observations WHERE path=?",
                      (path,)).fetchone()
    if row and row["size"] == size and row["mtime"] == mtime:
        return (now - row["observed_at"]) >= quiesce_seconds
    con.execute(
        """INSERT INTO observations(path, size, mtime, observed_at)
           VALUES(?,?,?,?)
           ON CONFLICT(path) DO UPDATE SET size=excluded.size,
             mtime=excluded.mtime, observed_at=excluded.observed_at""",
        (path, size, mtime, now))
    con.commit()
    return False


def prune_observations(con: sqlite3.Connection, present: set[str]) -> None:
    rows = con.execute("SELECT path FROM observations").fetchall()
    gone = [r["path"] for r in rows if r["path"] not in present]
    if gone:
        con.executemany("DELETE FROM observations WHERE path=?",
                        [(p,) for p in gone])
        con.commit()


# -- youtube source ----------------------------------------------------------

def video_get(con: sqlite3.Connection, video_id: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM videos WHERE video_id=?",
                       (video_id,)).fetchone()


def video_upsert(con: sqlite3.Connection, video_id: str, *, state: str,
                 title: str = "", note_id: str = "", last_error: str = "",
                 attempts: int | None = None, outcome: str = "") -> None:
    """Insert or update one video. `attempts` is only written when given;
    `title`, `note_id` and `outcome` keep their old value when passed
    empty."""
    now = _now()
    con.execute(
        """INSERT INTO videos(video_id, title, state, attempts, last_error,
                              note_id, outcome, first_seen, updated)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(video_id) DO UPDATE SET
             state=excluded.state,
             title=CASE WHEN excluded.title != '' THEN excluded.title
                        ELSE videos.title END,
             note_id=CASE WHEN excluded.note_id != '' THEN excluded.note_id
                          ELSE videos.note_id END,
             outcome=CASE WHEN excluded.outcome != '' THEN excluded.outcome
                          ELSE videos.outcome END,
             attempts=CASE WHEN ? THEN excluded.attempts ELSE videos.attempts END,
             last_error=excluded.last_error, updated=excluded.updated""",
        (video_id, title, state, attempts or 0, last_error, note_id, outcome,
         now, now, attempts is not None))
    con.commit()


def video_rows_in_state(con: sqlite3.Connection, state: str
                        ) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM videos WHERE state=? ORDER BY updated",
                       (state,)).fetchall()


def video_counts(con: sqlite3.Connection) -> dict[str, int]:
    """Counts by state, with done rows split by outcome (`done/skipped`)."""
    out: dict[str, int] = {}
    for r in con.execute("SELECT state, outcome, COUNT(*) c FROM videos "
                         "GROUP BY state, outcome"):
        key = (f"{r['state']}/{r['outcome']}"
               if r["state"] == "done" and r["outcome"] == "skipped"
               else r["state"])
        out[key] = out.get(key, 0) + r["c"]
    return out


def video_recent(con: sqlite3.Connection, n: int = 5) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM videos ORDER BY updated DESC LIMIT ?", (n,)).fetchall()


def video_stuck(con: sqlite3.Connection, min_attempts: int = 3
                ) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM videos WHERE state='failed' AND attempts>=? "
        "ORDER BY updated", (min_attempts,)).fetchall()


def meta_get(con: sqlite3.Connection, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO meta(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
    con.commit()
