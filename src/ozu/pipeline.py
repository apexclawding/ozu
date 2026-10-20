"""CLI: python -m ozu.pipeline <command>

Commands:
  init     scaffold ~/ozu-data dirs + vault
  pull     rclone sync of the Drive ScribeInbox
  ingest   rasterize + dedup + enqueue new pages
  process  run the Claude vision pass on the queue
  claude   run queued @claude work runs now (the legacy one-shot queue)
  research run queued @research briefs now
  learn    run queued learning briefs (pinned videos, articles) now;
           `learn add <url|file>` reads an article, a post or a PDF into the
           queue; `learn probe <urls.txt>` reads each URL and writes nothing
  wiki     status | run | requeue [--all] — the concept pages compiled from
           the vault (`requeue` offers failed notes again; --all recompiles)
  edition  morning | evening | extra [YYYY-MM-DD] [--force] — build and send
           one edition now (the timers run morning 07:00, evening 20:00;
           one of each per day unless --force). `mail` = `edition extra`.
  tick     pull → ingest → process → builds (the 5-minute paper loop)
  builds   list Builds; `builds run` the builds phase now;
           `builds simulate --note <id>` a proposal PDF without a vision call;
           `builds reset <id>` hand an exhausted Build a fresh attempt budget;
           `builds split <id> --part f.md ...` carve one Build into several
  run      pull → ingest → process → claude/research/learn/wiki runs (the 2-hourly job)
  requeue  re-read pages the pipeline gave up on: `requeue error` (a failed
           vision call), `requeue skipped [name]` (pages the ink detector
           filed as blank), `requeue <page-id>...`
  index    rebuild SQLite index/edges from the vault, regenerate index.md + log.md
  okf      check | export [dir] | viz [html]  — the vault as an OKF v0.2 bundle
  lint     [folder] — read-only lint of the learning loop in `folder`
           (default topics/learning): failed or lost briefs, orphaned
           briefs, missing sections, dead links; exits 1 on any finding
  digest   write the weekly digest Markdown into ~/ozu-data/digests/ (the
           typeset copy rides Sunday's evening edition)
  mcp      start the MCP server (add --http for remote)
  status   queue and vault counts
"""
from __future__ import annotations

import contextlib
import fcntl
import logging
import sys
from pathlib import Path

from .config import get_config
from . import db


@contextlib.contextmanager
def hold_lock(name: str = "pipeline.lock", *, wait: bool = False):
    """One flock for every command that walks the pipeline, so the 5-minute
    tick and the 2-hourly nightly can never run it concurrently. Yields True
    with the lock held. The tick uses wait=False and simply skips — another
    tick lands in five minutes; the nightly uses wait=True and blocks until
    the tick in flight finishes (a oneshot systemd unit can afford to)."""
    cfg = get_config()
    cfg.home.mkdir(parents=True, exist_ok=True)
    lock_path = cfg.home / name
    with open(lock_path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _tick() -> None:
    """The paper loop: everything a returning page needs, nothing that
    belongs to the night. Each stage is guarded — Drive being down must not
    cost the stages after it. No mail here since 2026-09-11: what the ink
    became waits for the next edition (edition.py), only Build proposals
    still leave within the tick."""
    from . import builds, ingest, understand
    logger = logging.getLogger("ozu")
    try:
        ingest.pull()
    except Exception as e:
        logger.warning("pull failed, tick continues: %s", e)
    ingest.ingest()
    understand.process_pending()
    try:
        builds.run_pending()
    except Exception:
        logger.warning("builds phase failed, tick continues", exc_info=True)
    _catch_up(logger)


def _catch_up(logger) -> None:
    """Last step of every tick and run: the db follows the vault, whoever
    moved it (this run's writers, a hand edit, another program)."""
    from . import indexer
    try:
        indexer.catch_up()
    except Exception:
        logger.warning("index catch-up failed; the next tick retries", exc_info=True)


def _nightly(cfg, agent, builds, indexer, ingest, understand, vault) -> None:
    """The 2-hourly cycle: everything the tick does NOT own — the offsite
    vault sync, the legacy @claude one-shot queue, research and learning
    runs. It still walks pull → ingest → process → builds so the pipeline
    survives the tick timer dying, at the cost of some redundant (deduped,
    and therefore free) work when both are healthy."""
    logger = logging.getLogger("ozu")
    if vault.sync_from_origin(cfg.vault_dir):
        indexer.rebuild()  # files changed offsite; the db derives from them
    try:
        ingest.pull()
    except Exception as e:  # Drive down ≠ lose the run; process what we have
        logger.warning("pull failed, continuing: %s", e)
    ingest.ingest()
    understand.process_pending()
    try:  # a broken work run must not fail the nightly job
        agent.work_pending()
    except Exception:
        logger.warning("claude work runs failed, run continues", exc_info=True)
    try:  # the paper loop's phase also runs here, in case the tick timer is off
        builds.run_pending()
    except Exception:
        logger.warning("builds phase failed, run continues", exc_info=True)
    try:
        from . import research
        research.work_pending()
    except Exception:
        logger.warning("research runs failed, run continues", exc_info=True)
    try:  # transcripts a capture bridge pushed since the last cycle
        from . import learn
        learn.work_pending()
    except Exception:
        logger.warning("learn runs failed, run continues", exc_info=True)
    try:  # after the briefs, so tonight's brief reaches the wiki tonight
        from . import wiki
        wiki.work_pending()
    except Exception:
        logger.warning("wiki run failed, run continues", exc_info=True)
    _catch_up(logger)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    cfg = get_config()

    if cmd == "init":
        from . import vault
        cfg.ensure_dirs()
        vault.scaffold(cfg.vault_dir)
        # work-vault is deliberately NOT created: employer content stays
        # on-device and is never exported (plan §9). The governance wall in
        # vault.write_notes() remains active as a backstop and will scaffold
        # work-vault on demand if a work-flagged page ever reaches the pipeline.
        db.connect(cfg.db_path)
        print(f"Initialized {cfg.home}, {cfg.vault_dir}")
    elif cmd == "pull":
        from . import ingest
        ingest.pull()
    elif cmd == "ingest":
        from . import ingest
        ingest.ingest()
    elif cmd == "process":
        from . import understand
        understand.process_pending()
    elif cmd == "claude":
        from . import agent
        agent.work_pending()
    elif cmd == "research":
        from . import research
        research.work_pending()
    elif cmd == "learn":
        sub = sys.argv[2] if len(sys.argv) > 2 else "run"
        if sub == "add" and len(sys.argv) > 3:
            import json
            from . import articles
            with hold_lock(wait=True) as held:
                assert held
                print(json.dumps(articles.capture(sys.argv[3]), indent=2, ensure_ascii=False))
        elif sub == "probe" and len(sys.argv) > 3:
            from . import articles
            urls = [u.strip() for u in Path(sys.argv[3]).read_text().splitlines()
                    if u.strip() and not u.startswith("#")]
            rows = articles.probe(urls)
            for r in rows:
                what = (f"{r['kind']}, {r['words']} words, {r['figures']} figures"
                        + (f", {r['pages']} pages" if r.get("pages") else "")
                        + (f" — partial: {r['partial']}" if r.get("partial") else "")
                        if r["ok"] else r.get("reason", "nothing readable"))
                print(f"{'ok  ' if r['ok'] else 'FAIL'}  {r['url']}\n      {what}")
            print(f"{sum(r['ok'] for r in rows)}/{len(rows)} readable")
        elif sub == "run":
            from . import learn
            learn.work_pending()
        else:
            print("usage: ozu learn [run | add <url|file> | probe <urls.txt>]")
            sys.exit(1)
    elif cmd == "wiki":
        from . import wiki
        sub = sys.argv[2] if len(sys.argv) > 2 else "status"
        if sub == "run":
            with hold_lock(wait=True) as held:
                assert held
                print(f"{wiki.work_pending()} page(s) written")
        elif sub == "requeue":
            everything = "--all" in sys.argv[3:]
            print(f"{wiki.requeue(errors_only=not everything)} note(s) offered "
                  f"to the wiki again")
        elif sub == "status":
            print(wiki.status(db.connect(cfg.db_path)))
        else:
            print("usage: ozu wiki [status | run | requeue [--all]]")
            sys.exit(1)
    elif cmd in ("edition", "mail"):
        import datetime as _dt
        from . import edition
        args = [a for a in sys.argv[2:] if a != "--force"]
        if cmd == "mail":
            kind, rest = "extra", args
        else:
            kind, rest = (args[0] if args else ""), args[1:]
        if kind not in edition.KINDS:
            print("usage: ozu edition morning|evening|extra [YYYY-MM-DD] [--force]")
            sys.exit(1)
        try:
            day = _dt.date.fromisoformat(rest[0]) if rest else None
        except ValueError:
            print("usage: ozu edition morning|evening|extra [YYYY-MM-DD] [--force]")
            sys.exit(1)
        with hold_lock(wait=True) as held:
            assert held
            print("sent" if edition.send(kind, day, force="--force" in sys.argv)
                  else "nothing sent")
    elif cmd == "tick":
        with hold_lock() as held:
            if not held:
                logging.getLogger("ozu").info(
                    "another pipeline run holds the lock; this tick skips")
                sys.exit(0)
            _tick()
    elif cmd == "builds":
        from . import builds
        sub = sys.argv[2] if len(sys.argv) > 2 else "list"
        if sub == "run":
            with hold_lock(wait=True) as held:
                assert held
                builds.run_pending()
        elif sub == "simulate":
            if len(sys.argv) < 5 or sys.argv[3] != "--note":
                print("usage: ozu builds simulate --note <note-id>")
                sys.exit(1)
            print(builds.simulate(sys.argv[4]))
        elif sub == "reset":
            if len(sys.argv) < 4:
                print("usage: ozu builds reset <build-id> [--no-approve]")
                sys.exit(1)
            bid = sys.argv[3]
            approve = "--no-approve" not in sys.argv[4:]
            status = builds.reset_budget(bid, approve=approve)
            print(f"{bid}: budget cleared, status {status}"
                  + (" — the next tick will execute it once more"
                     if status == "approved" else ""))
        elif sub == "split":
            args = sys.argv[3:]
            parts = [Path(a) for a, flag in zip(args[1:], args)
                     if flag == "--part"]
            if len(args) < 3 or not parts:
                print("usage: ozu builds split <build-id> --part <file.md> "
                      "[--part <file.md> ...]")
                sys.exit(1)
            with hold_lock(wait=True) as held:
                assert held
                made = builds.split(args[0], parts)
            print(f"{args[0]} split into {len(made)}: {', '.join(made)}")
            print("each is `proposed` and on its way to the Scribe — "
                  "nothing runs until you approve it in ink")
        elif sub == "list":
            con = db.connect(cfg.db_path)
            rows = con.execute(
                "SELECT * FROM builds ORDER BY updated DESC, id DESC").fetchall()
            for r in rows:
                pend = f" (pending {r['pending']})" if r["pending"] else ""
                spent = (f" [{r['attempts']}a {r['deferrals']}d]"
                         if r["attempts"] or r["deferrals"] else "")
                print(f"{r['id']}  {r['status']:<9} r{r['rev']}  {r['topic']:<14}"
                      f" {r['title']}{pend}{spent}")
            if not rows:
                print("no builds yet — write @claude @<project> on a page")
        else:
            print("usage: ozu builds "
                  "[list | run | reset <id> | simulate --note <id>]")
            sys.exit(1)
    elif cmd == "run":
        from . import agent, builds, indexer, ingest, understand, vault
        with hold_lock(wait=True) as held:
            assert held  # wait=True always acquires
            _nightly(cfg, agent, builds, indexer, ingest, understand, vault)
    elif cmd == "requeue":
        args = sys.argv[2:]
        con = db.connect(cfg.db_path)
        if args and args[0] == "error":
            n = con.execute("UPDATE pages SET status='new', error=NULL"
                            " WHERE status='error'").rowcount
        elif args and args[0] == "skipped":
            like = f"%{args[1]}%" if len(args) > 1 else "%"
            n = con.execute("UPDATE pages SET status='new', error=NULL"
                            " WHERE status='skipped' AND notebook LIKE ?",
                            (like,)).rowcount
        elif args and all(a.isdigit() for a in args):
            n = con.executemany("UPDATE pages SET status='new', error=NULL"
                                " WHERE id=?", [(int(a),) for a in args]).rowcount
        else:
            print("usage: ozu requeue error | skipped [name] | <page-id>...")
            sys.exit(1)
        con.commit()
        print(f"{n} page(s) queued to be read again")
    elif cmd == "index":
        from . import indexer
        indexer.rebuild()
    elif cmd == "okf":
        from . import okf
        sub = sys.argv[2] if len(sys.argv) > 2 else "check"
        if sub == "check":
            errors, warnings = okf.check(cfg.vault_dir)
            for w in warnings:
                print(f"warning: {w}")
            for e in errors:
                print(f"error: {e}")
            n = len(okf.concept_files(cfg.vault_dir))
            print(f"{n} concept(s): {len(errors)} error(s), {len(warnings)} warning(s)")
            sys.exit(1 if errors else 0)
        elif sub == "export":
            out = Path(sys.argv[3]).expanduser() if len(sys.argv) > 3 else cfg.home / "okf-export"
            stats = okf.export(cfg.vault_dir, out)
            print(f"exported {stats['concepts']} concept(s), "
                  f"{stats['links_rewritten']} wikilink(s) rewritten → {out}")
        elif sub == "viz":
            out = Path(sys.argv[3]).expanduser() if len(sys.argv) > 3 else cfg.home / "okf-export" / "viz.html"
            bundle = cfg.home / "okf-export"
            okf.export(cfg.vault_dir, bundle)
            okf.visualize(bundle, out)
            print(f"wrote {out}")
        else:
            print("usage: ozu okf check | export [dir] | viz [html]")
            sys.exit(1)
    elif cmd == "lint":
        from . import lint
        folder = sys.argv[2] if len(sys.argv) > 2 else lint.FOLDER
        findings = lint.lint(cfg.vault_dir, folder)
        for f in findings:
            print(f)
        print(f"{folder}: {len(findings)} finding(s)")
        sys.exit(1 if findings else 0)
    elif cmd == "digest":
        from . import digest
        digest.run()
    elif cmd == "mcp":
        from . import mcp_server
        mcp_server.main()
    elif cmd == "status":
        con = db.connect(cfg.db_path)
        q = {r["status"]: r["c"] for r in
             con.execute("SELECT status, COUNT(*) c FROM pages GROUP BY status")}
        n = con.execute("SELECT COUNT(*) c FROM notes WHERE vault='personal'").fetchone()["c"]
        # Walled notes are invisible to every other view (no edges, no digest,
        # no MCP), so a walled page otherwise reads as "the run did nothing".
        walled_n = con.execute("SELECT COUNT(*) c FROM notes WHERE vault='work'").fetchone()["c"]
        e = con.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
        a = con.execute("SELECT COUNT(*) c FROM actions WHERE done=0").fetchone()["c"]
        cq = con.execute("SELECT COUNT(*) c FROM notes WHERE claude='requested'").fetchone()["c"]
        rq = con.execute("SELECT COUNT(*) c FROM notes WHERE research='requested'").fetchone()["c"]
        lq = con.execute("SELECT COUNT(*) c FROM notes WHERE learn='requested'").fetchone()["c"]
        b = {r["status"]: r["c"] for r in con.execute(
            "SELECT status, COUNT(*) c FROM builds GROUP BY status")}
        builds_s = f" | builds: {b}" if b else ""
        walled = f" | walled: {walled_n}" if walled_n else ""
        queued = f" | @claude queued: {cq}" if cq else ""
        rqueued = f" | @research queued: {rq}" if rq else ""
        lqueued = f" | @learn queued: {lq}" if lq else ""
        print(f"pages: {q or '{}'} | notes: {n} | edges: {e} | open actions: {a}{builds_s}{walled}{queued}{rqueued}{lqueued}")
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
