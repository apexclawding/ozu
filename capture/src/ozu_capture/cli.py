"""ozu-capture: run-once | status | install-agent | uninstall-agent
             | youtube-once | youtube-loop

The first four are the voice source (the Mac LaunchAgent). The last two are
the youtube source: `youtube-once` is one tick in the foreground — the
debugging and test entry — and `youtube-loop` is what the NAS container
runs: a tick whenever the poll clock allows, and a daily yt-dlp upgrade
(Amendment 1 FR-11), forever.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import subprocess
import sys
import time
from pathlib import Path

from . import config, ledger, logsetup

log = logging.getLogger("ozu_capture.cli")

UPGRADE_EVERY_SECONDS = 24 * 3600
UPGRADED_KEY = "ytdlp_upgraded"
VERSION_KEY = "ytdlp_version"


def _fmt_epoch(value: str) -> str:
    try:
        t = float(value)
    except ValueError:
        return "never"
    return "never" if t <= 0 else dt.datetime.fromtimestamp(t).strftime(
        "%Y-%m-%d %H:%M")


def _cmd_status(cfg: config.Config) -> int:
    con = ledger.connect(cfg.ledger_path)
    print(f"data:   {cfg.data_dir}")
    if cfg.voice_enabled:
        from . import scan
        if cfg.inbox_override:
            inbox = Path(cfg.inbox_override)
            print(f"inbox:  {inbox} (override)")
        else:
            inbox = scan.find_mount(cfg.share_url)
            print(f"share:  {cfg.share_url}")
            print(f"mount:  {inbox or 'NOT MOUNTED'}")
        print(f"ledger: {ledger.counts(con) or '{} (empty)'}")
        if inbox and inbox.is_dir():
            swp = scan.sweep_inbox(inbox, con, cfg)
            if swp.waiting:
                print(f"waiting (not yet stable): "
                      f"{', '.join(p.name for p in swp.waiting)}")
            if swp.marked:
                print(f"failed (delete .failed.txt to retry): "
                      f"{', '.join(p.name for p in swp.marked)}")
            if swp.eligible:
                print(f"queued: {', '.join(p.name for p in swp.eligible)}")
        for r in ledger.recent(con):
            print(f"  {r['updated']}  {r['state']:<10} {Path(r['path']).name}"
                  + (f"  -> {r['note_id']}" if r["note_id"] else ""))
    if cfg.youtube_enabled or ledger.video_counts(con):
        from . import youtube
        print("youtube:")
        print(f"  playlist:    {cfg.playlist_url or '(none)'}"
              + ("" if cfg.youtube_enabled else "  (disabled)"))
        print(f"  last polled: "
              f"{_fmt_epoch(ledger.meta_get(con, 'last_polled', '0'))}"
              f"  (every {cfg.poll_minutes} min)")
        fails = ledger.meta_get(con, "listing_failures", "0")
        if fails not in ("", "0"):
            print(f"  LISTING FAILING: {fails} poll(s) in a row")
        ver = ledger.meta_get(con, VERSION_KEY, "") or youtube.ytdlp_version(cfg)
        print(f"  yt-dlp:      {ver}; last upgrade "
              f"{_fmt_epoch(ledger.meta_get(con, UPGRADED_KEY, '0'))}")
        print(f"  videos:      {ledger.video_counts(con) or '{} (none yet)'}")
        stuck = ledger.video_stuck(con)
        if stuck:
            print("  STUCK (3+ failed fetches; delete the row to retry): "
                  + ", ".join(f"{r['video_id']} ({r['last_error'][:60]})"
                              for r in stuck))
        for r in ledger.video_recent(con):
            print(f"  {r['updated']}  {r['state']:<10} {r['video_id']}"
                  f"  {r['title'][:50]}"
                  + (f"  -> {r['note_id']}" if r["note_id"] else "")
                  + (f"  [{r['last_error'][:60]}]"
                     if r["last_error"] and r["state"] != "done" else ""))
    return 0


def _maybe_upgrade(cfg: config.Config, con, now: float | None = None) -> None:
    """FR-11: `pip install --user -U yt-dlp` once a day. A pip-installed
    yt-dlp cannot self-update, and YouTube changes faster than anyone
    rebuilds an image. The user site shadows the image's copy (PATH puts
    ~/.local/bin first), so a failed upgrade just keeps the old one."""
    from . import youtube
    if not cfg.ytdlp_upgrade:
        return
    now = time.time() if now is None else now
    last = float(ledger.meta_get(con, UPGRADED_KEY, "0") or 0)
    if now - last < UPGRADE_EVERY_SECONDS:
        return
    cmd = [sys.executable, "-m", "pip", "install", "--user", "--quiet",
           "--upgrade", "yt-dlp"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("yt-dlp upgrade could not run (%s); keeping current", e)
        p = None
    if p is not None and p.returncode != 0:
        log.warning("yt-dlp upgrade failed (exit %d): %s", p.returncode,
                    (p.stderr or "").strip()[-300:])
    ledger.meta_set(con, UPGRADED_KEY, str(now))
    ver = youtube.ytdlp_version(cfg)
    ledger.meta_set(con, VERSION_KEY, ver)
    log.info("yt-dlp %s", ver)


def _cmd_youtube_loop(cfg: config.Config) -> int:
    from . import run, youtube
    con = ledger.connect(cfg.ledger_path)
    ledger.meta_set(con, VERSION_KEY, youtube.ytdlp_version(cfg))
    log.info("youtube loop: %s every %d min; yt-dlp %s", cfg.playlist_url,
             cfg.poll_minutes, ledger.meta_get(con, VERSION_KEY))
    _maybe_upgrade(cfg, con)
    while True:
        try:
            run.youtube_tick(cfg, con)
        except Exception:               # a tick must never take the loop down
            log.exception("youtube tick crashed; next tick in a minute")
        try:
            _maybe_upgrade(cfg, con)
        except Exception:
            log.exception("yt-dlp upgrade check crashed")
        time.sleep(60)


def main() -> None:
    ap = argparse.ArgumentParser(prog="ozu-capture")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run-once", "status", "install-agent", "uninstall-agent",
                 "youtube-once", "youtube-loop"):
        sub.add_parser(name)
    args = ap.parse_args()

    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)
    cfg.ensure_dirs()

    if args.cmd == "status":
        sys.exit(_cmd_status(cfg))
    if args.cmd == "run-once":
        logsetup.setup(cfg.log_path)
        from . import run
        sys.exit(run.run_once(cfg))
    if args.cmd in ("youtube-once", "youtube-loop"):
        if not cfg.youtube_enabled:
            print("config error: [youtube] enabled = false", file=sys.stderr)
            sys.exit(2)
        logsetup.setup(cfg.log_path)
        if args.cmd == "youtube-once":
            from . import run
            sys.exit(run.youtube_once(cfg))
        sys.exit(_cmd_youtube_loop(cfg))
    if args.cmd in ("install-agent", "uninstall-agent"):
        from . import agentctl
        sys.exit(agentctl.install(cfg) if args.cmd == "install-agent"
                 else agentctl.uninstall(cfg))


if __name__ == "__main__":
    main()
