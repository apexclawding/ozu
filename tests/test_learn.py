"""Learning-brief tests. The headless call is mocked; the ledger, the
companion note, the edges, the sheet, the spacing schedule, the delivery
gating, the mail exclusion and the allowlist are real.

Transcript notes arrive in the vault from a capture bridge (a YouTube
playlist watcher), not from a Scribe page, so the fixture writes one straight into
`topics/learning/` and reindexes — exactly what `ozu run` does after
`sync_from_origin`."""
import datetime as dt
import os.path
import sys
from pathlib import Path

import fitz

from ozu import textlayer

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from test_pipeline import env, make_pdf, MOCK_RESULT  # noqa: F401 — shared fixture

BRIEF = """# Takeaways
- A lab notebook compounds: write the question before the result.
- Retrieval beats memory when the corpus is yours.

# Techniques, tools, tips
- Date every page and never tear one out [06:45].

# Where it touches your work
- **kettle** — [[20250101-old-note]]: a brew log is a lab notebook with a timer.

# Try this week
Write one brew-day page in the notebook format.

# Question for the page
Which of your logs would survive a stranger reading them?"""

TRANSCRIPT_LINE = "What you're looking at right over here is a page from my notebook."


def seed_transcript(env, *, title="How to Keep a Lab Notebook", video_id="Xk3vQ9pLm2A",
                    learn="requested", created="2026-09-01", extra="", vault_dir=None):
    """A transcript note as a capture bridge writes it, then `ozu index`."""
    from ozu import indexer, vault
    vault_dir = vault_dir or env.vault_dir
    vault.scaffold(vault_dir)
    d = vault_dir / "topics" / "learning"
    d.mkdir(parents=True, exist_ok=True)
    slug = vault._slugify(title)
    note_id = f"{created.replace('-', '')}-{slug}"
    (d / f"{note_id}.md").write_text(f"""---
type: Transcript
id: {note_id}
title: "{title}"
description: YouTube — Example Workshop, 14m 35s, English auto captions.
tags: [learning, youtube, transcript]
created: {created}
updated: {created}
source: youtube:{video_id}
video_id: {video_id}
url: https://www.youtube.com/watch?v={video_id}
channel: Example Workshop
duration: "00:14:35"
published: 2026-07-03
language: en-US
captions: auto
learn: {learn}
{extra}related: []
needs_review: false
generated: {{ by: capture/youtube, at: {created}T05:00:00Z }}
sources:
  - id: video
    resource: https://www.youtube.com/watch?v={video_id}
    title: "{title}"
---

# {title}

> Summary: YouTube — Example Workshop, 14m 35s, English auto captions.

https://www.youtube.com/watch?v={video_id}

**[00:00:00]** {TRANSCRIPT_LINE}
""")
    indexer.rebuild()
    return note_id


def run_briefs(env, monkeypatch, report=BRIEF):
    from ozu import agent, learn
    monkeypatch.setattr(agent, "_run_claude", lambda *a, **k: report)
    return learn.work_pending()


def pdf_text(path: Path) -> str:
    doc = fitz.open(path)
    text = "\n".join(textlayer.extract(p) for p in doc)
    doc.close()
    return text


def deliver(monkeypatch):
    from ozu import send_kindle
    monkeypatch.setattr(send_kindle, "send_pdf", lambda p: True)


def evening(day):
    """The sheet reaches the device as the Learn section of that day's
    evening edition — one per day, sent only when it has something."""
    from ozu import edition
    return edition.send("evening", day)


D = dt.date(2026, 9, 2)


def test_learn_request_indexes_from_frontmatter(env):
    from ozu import db
    seed_transcript(env)
    con = db.connect(env.db_path)
    row = con.execute("SELECT learn, vault, topic FROM notes").fetchone()
    assert (row["learn"], row["vault"], row["topic"]) == ("requested", "personal", "learning")


def test_learn_run_is_read_only_and_writes_the_companion(env, monkeypatch):
    from ozu import agent, db, learn
    src_id = seed_transcript(env)
    seen = {}

    def fake_run(prompt, workdir, allowed_tools=None, model=None, timeout=None, writes=False):
        seen.update(prompt=prompt, cwd=workdir, tools=allowed_tools, model=model)
        return BRIEF

    monkeypatch.setattr(agent, "_run_claude", fake_run)
    assert learn.work_pending() == 1

    # FR-22: Read pinned to the vault, and that is the whole allowlist. A
    # transcript is third-party text, so no web tool may ever join this.
    scope = "//" + os.path.abspath(env.vault_dir).lstrip("/") + "/**"
    assert seen["tools"] == f"Read({scope})"
    # D-10: rightsized. Summarising a transcript it was handed, with no web
    # and a vault-scoped Read, is the one run kind that asks least — and it
    # bills the subscription, so this buys usage headroom, not money.
    assert seen["model"] == env.run_models["learn"] == "claude-sonnet-5"
    assert seen["cwd"] == env.vault_dir
    # the run reads the transcript itself; the prompt names the file and
    # the video, and never inlines the captions
    assert "topics/learning/" in seen["prompt"] and "Example Workshop" in seen["prompt"]
    assert TRANSCRIPT_LINE not in seen["prompt"]

    con = db.connect(env.db_path)
    src = con.execute("SELECT * FROM notes WHERE id=?", (src_id,)).fetchone()
    assert src["learn"] == "done" and "learn: done" in Path(src["path"]).read_text()

    companion = next((env.vault_dir / "topics" / "learning").glob("*-learning.md"))
    text = companion.read_text()
    assert "title: \"Learning: How to Keep" in text       # a colon in a title is quoted — strict YAML
    assert "tags: [learning, claude-learn]" in text
    assert f"source: learn:{src_id}" in text and "video_id: Xk3vQ9pLm2A" in text
    assert "pinned: 2026-09-01" in text and 'duration: "00:14:35"' in text
    assert f"From [[{src_id}]]" in text and "## Takeaways" in text
    from ozu import okf
    assert okf.parse_frontmatter(text)[0]["description"].startswith("A lab notebook compounds")  # first takeaway = index line

    comp = con.execute("SELECT * FROM notes WHERE (','||tags||',') LIKE '%,claude-learn,%'").fetchone()
    edges = {(e["dst"], e["reason"]) for e in
             con.execute("SELECT dst, reason FROM edges WHERE src=?", (comp["id"],))}
    assert (src_id, "learning brief") in edges
    assert ("20250101-old-note", "cited in learning brief") in edges

    assert learn.work_pending() == 0   # one run per note, ever


def test_learn_failure_marked_error_and_terminal(env, monkeypatch):
    from ozu import agent, db, learn
    seed_transcript(env)

    def boom(*a, **k):
        raise RuntimeError("claude exited 1")

    monkeypatch.setattr(agent, "_run_claude", boom)
    assert learn.work_pending() == 1
    con = db.connect(env.db_path)
    assert con.execute("SELECT learn FROM notes").fetchone()["learn"] == "error"
    assert learn.work_pending() == 0


def test_auth_failure_leaves_transcript_queued(env, monkeypatch):
    from ozu import agent, db, learn
    seed_transcript(env)

    def expired(*a, **k):
        raise agent.AuthError("Failed to authenticate: OAuth session expired")

    monkeypatch.setattr(agent, "_run_claude", expired)
    assert learn.work_pending() == 1
    con = db.connect(env.db_path)
    assert con.execute("SELECT learn FROM notes").fetchone()["learn"] == "requested"


def test_walled_transcript_never_runs(env, monkeypatch):
    from ozu import learn
    seed_transcript(env, vault_dir=env.work_vault_dir)
    assert run_briefs(env, monkeypatch) == 0
    assert not list((env.vault_dir / "topics").rglob("*-learning.md")) if (env.vault_dir / "topics").exists() else True
    assert learn.work_pending() == 0


def test_runs_are_capped_per_cycle(env, monkeypatch):
    for i in range(4):
        seed_transcript(env, title=f"Video {i}", video_id=f"vid{i}")
    assert run_briefs(env, monkeypatch) == 3    # OZU_MAX_LEARN_RUNS default
    assert run_briefs(env, monkeypatch) == 1


def test_slugify_reserves_room_for_the_suffix():
    """The cap is 60; a suffix has to be subtracted from it, not appended
    past it, or the marker silently disappears on exactly the long titles
    where it is doing the distinguishing work."""
    from ozu import vault
    long_title = "Seven Rules for a Lab Notebook That Still Makes Sense Ten Years Later"
    slug = vault._slugify(long_title, suffix="-learning")
    assert slug.endswith("-learning") and len(slug) <= 60
    assert slug != vault._slugify(long_title)          # the whole point: distinct ids
    assert vault._slugify("x" * 59 + " y") == "x" * 59  # the cut never leaves a trailing dash


def test_long_title_brief_does_not_take_the_transcript_id(env, monkeypatch):
    """Regression, live vault 2026-09-03: `_slugify(f"{title}-learning")`
    truncated to 60 *after* appending, so this title's brief lost its marker
    and landed on the transcript's own id — a duplicate `okf check` caught
    only later, because the pre-OKF transcript was title-named and the
    `_unique_id` filename probe therefore missed the clash."""
    from ozu import db
    src_id = seed_transcript(
        env, title="Seven Rules for a Lab Notebook That Still Makes Sense Ten Years Later")
    assert run_briefs(env, monkeypatch) == 1

    con = db.connect(env.db_path)
    comp = con.execute(
        "SELECT id, path FROM notes WHERE (','||tags||',') LIKE '%,claude-learn,%'").fetchone()
    assert comp["id"] != src_id
    assert comp["id"].endswith("-learning")
    assert Path(comp["path"]).stem == comp["id"]       # OKF: the filename is the id
    assert (env.vault_dir / "topics" / "learning" / f"{src_id}.md").exists()


def test_sheet_sends_new_once_and_only_when_delivered(env, monkeypatch):
    from ozu import db, layout, learn
    seed_transcript(env)
    run_briefs(env, monkeypatch)

    # not configured → built, not sent, nothing ledgered
    assert evening(D) is False
    con = db.connect(env.db_path)
    assert con.execute("SELECT COUNT(*) c FROM learn_sheet").fetchone()["c"] == 0

    deliver(monkeypatch)
    assert evening(D) is True
    pdf = env.editions_dir / "Ozu Evening 2026-09-02.pdf"
    text = pdf_text(pdf)
    assert "Ozu Learn" in text and "Wednesday 2 September 2026" in text
    assert "Ozu Evening" in text and "In this edition" in text
    assert "How to Keep a Lab Notebook" in text
    assert "Example Workshop" in text and "pinned 2026-09-01" in text
    assert "A lab notebook compounds" in text and "In your own words" in text
    assert "note: 2026" in text                      # the id line (FR-41)
    assert layout.BOX not in text                    # nothing a tick could close
    rows = con.execute("SELECT stage, sent_on FROM learn_sheet").fetchall()
    assert [(r["stage"], r["sent_on"]) for r in rows] == [(0, "2026-09-02")]

    # same evening again: one sheet per day
    assert evening(D) is False
    assert evening(D + dt.timedelta(days=1)) is False   # nothing due yet


def test_sheet_resurfaces_on_the_spacing_schedule(env, monkeypatch):
    from ozu import db, learn
    seed_transcript(env)
    run_briefs(env, monkeypatch)
    deliver(monkeypatch)
    assert evening(D) is True

    def sheet_text(day):
        assert evening(day) is True
        return pdf_text(env.editions_dir / f"Ozu Evening {day.isoformat()}.pdf")

    # +2 days: second meeting, as a cue and the question, not the full brief
    assert evening(D + dt.timedelta(days=1)) is False
    text = sheet_text(D + dt.timedelta(days=2))
    assert "Due again" in text and "2nd time" in text and "learned 2026-09-02" in text
    assert "Cue: A lab notebook compounds" in text
    assert "Question: Which of your logs would survive" in text
    assert "Keep raw/" not in text                   # the brief itself is not reprinted

    # +7 after that, +30 after that, then never
    assert evening(D + dt.timedelta(days=8)) is False
    assert "3rd time" in sheet_text(D + dt.timedelta(days=9))
    assert evening(D + dt.timedelta(days=38)) is False
    assert "4th time" in sheet_text(D + dt.timedelta(days=39))
    for later in (40, 69, 100, 400):
        assert evening(D + dt.timedelta(days=later)) is False

    con = db.connect(env.db_path)
    stages = [r["stage"] for r in con.execute("SELECT stage FROM learn_sheet ORDER BY stage")]
    assert stages == [0, 1, 2, 3]


def test_sheet_caps_three_new_and_two_due_and_carries_the_rest(env, monkeypatch):
    from ozu import learn
    env.max_learn_runs = 10
    titles = [f"Video {i}" for i in range(5)]
    for i, t in enumerate(titles):
        seed_transcript(env, title=t, video_id=f"vid{i}", created=f"2026-08-2{i + 1}")
    assert run_briefs(env, monkeypatch) == 5
    deliver(monkeypatch)

    assert evening(D) is True
    text = pdf_text(env.editions_dir / "Ozu Evening 2026-09-02.pdf")
    assert all(f"Video {i}" in text for i in range(3))     # oldest three
    assert not any(f"Video {i}" in text for i in (3, 4))
    assert evening(D) is False                     # one per evening

    day = D + dt.timedelta(days=1)
    assert evening(day) is True                    # the other two
    text = pdf_text(env.editions_dir / f"Ozu Evening {day.isoformat()}.pdf")
    assert "Video 3" in text and "Video 4" in text and "Video 0" not in text

    # D+2: the first three are due together; only two come back, one waits
    day = D + dt.timedelta(days=2)
    assert evening(day) is True
    text = pdf_text(env.editions_dir / f"Ozu Evening {day.isoformat()}.pdf")
    assert text.count("2nd time") == 2
    day = D + dt.timedelta(days=3)
    assert evening(day) is True
    text = pdf_text(env.editions_dir / f"Ozu Evening {day.isoformat()}.pdf")
    # the one that waited, plus the two from the second evening now due
    assert text.count("2nd time") == 2


def test_skipped_transcript_is_reported_once(env, monkeypatch):
    from ozu import db, learn
    seed_transcript(env, title="Let's reproduce GPT-2", video_id="l8pRSuU81PU",
                    learn="skipped", extra="skip_reason: too long (4h 01m)\n")
    assert run_briefs(env, monkeypatch) == 0             # skipped never runs
    deliver(monkeypatch)
    assert evening(D) is True
    text = pdf_text(env.editions_dir / "Ozu Evening 2026-09-02.pdf")
    assert "Couldn't read" in text and "too long (4h 01m)" in text
    con = db.connect(env.db_path)
    assert con.execute("SELECT stage FROM learn_sheet").fetchone()["stage"] == -1
    assert evening(D + dt.timedelta(days=1)) is False


def test_mail_never_carries_learning_notes(env, monkeypatch):
    from ozu import db, ingest, mail, understand
    con = db.connect(env.db_path)
    mail._ensure_ledger(con)                     # seeded while empty
    seed_transcript(env)
    run_briefs(env, monkeypatch)
    assert mail.gather(db.connect(env.db_path)) is None

    # a real ink note beside them still mails, alone
    make_pdf(env.raw_dir / "Inbox" / "m.pdf", ["x"])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: dict(MOCK_RESULT))
    understand.process_pending()
    data = mail.gather(db.connect(env.db_path))
    assert [i["title"] for i in data["plain"]] == ["Kettle freemium pricing"]
    assert not any("learning" in (i.get("id") or "") for i in data["plain"])
    assert len(data["ids"]) == 1


def test_ledger_and_status_survive_index_rebuild(env, monkeypatch):
    from ozu import db, indexer, learn
    seed_transcript(env)
    run_briefs(env, monkeypatch)
    deliver(monkeypatch)
    assert evening(D) is True

    env.db_path.unlink()
    con = db.connect(env.db_path)
    # learn_sheet is a ledger: the file is gone, so it is gone — and that is
    # the mail rule too. What `ozu index` must never do is *drop* it:
    con.execute("INSERT INTO learn_sheet VALUES ('x', 0, '2026-09-02')")
    con.commit()
    indexer.rebuild()
    assert con.execute("SELECT COUNT(*) c FROM learn_sheet").fetchone()["c"] == 1
    assert learn.work_pending() == 0             # `learn: done` came back from the file
    assert con.execute("SELECT COUNT(*) c FROM edges WHERE dst='20250101-old-note'").fetchone()["c"] == 1


def test_digest_counts_learning_and_lists_failures(env, monkeypatch):
    from ozu import agent, digest, learn
    seed_transcript(env)
    run_briefs(env, monkeypatch)
    deliver(monkeypatch)
    evening(dt.date.today())
    text = digest.build_digest()
    assert "Learned this week" in text and "1 video · 1 coming back next week" in text

    seed_transcript(env, title="Broken one", video_id="zzz")
    monkeypatch.setattr(agent, "_run_claude",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    learn.work_pending()
    assert "@learn failed:* Broken one" in digest.build_digest()
