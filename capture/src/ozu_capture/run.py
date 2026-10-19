"""One tick of the bridge: resume what is owed, then sweep and process.

Every stage is re-entrant against the ledger; "run it again" is the
universal recovery action (NFR-02). The launchd job runs exactly this.

Two ticks live here. `run_once` is the voice source (the Mac LaunchAgent):
inbox sweep → WHISPER → note → publish. `youtube_tick` is the youtube
source (the NAS container, Amendment 1 §6.1): playlist listing →
captions → transcript note → publish, with the audio hand-off to the
voice inbox when a video has no captions — which the voice tick then
recognises by its sidecar and files as a learning transcript (FR-05).
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import logging
import shutil
import time
from pathlib import Path

from . import ledger, note, scan, transcribe, vaultops, youtube
from .config import Config

log = logging.getLogger("ozu_capture.run")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _walled_out_path(cfg: Config, audio: Path) -> Path:
    out, n = cfg.walled_dir / f"{audio.stem}.md", 2
    while out.exists():
        out = cfg.walled_dir / f"{audio.stem}-{n}.md"
        n += 1
    return out


REMOUNT_BACKOFF_SECONDS = 900


def _mount_flag(cfg: Config) -> Path:
    return cfg.data_dir / ".mount_down"


def _remount_allowed(cfg: Config) -> bool:
    """While the share is known-down, retry the (dialog-capable) remount at
    most every 15 min rather than every tick."""
    flag = _mount_flag(cfg)
    if not flag.exists():
        return True
    return (time.time() - flag.stat().st_mtime) >= REMOUNT_BACKOFF_SECONDS


def _log_mount_state(cfg: Config, mounted: bool) -> None:
    """FR-02: log once per state change, not per tick."""
    flag = _mount_flag(cfg)
    if not mounted:
        if not flag.exists():
            log.warning("share not mounted and remount failed; going quiet "
                        "until it returns")
        flag.touch()                 # also restamps the remount backoff clock
    elif flag.exists():
        log.info("share mounted again; resuming")
        flag.unlink()


# -- the youtube hand-off sidecar (FR-05) ------------------------------------

SIDECAR_PREFIX = "yt-"


def sidecar_path(audio: Path) -> Path:
    return audio.with_name(f"{audio.stem}.json")


def read_sidecar(audio: Path) -> dict | None:
    """The metadata the NAS container dropped beside a caption-less
    video's audio; None for an ordinary voice memo."""
    if not audio.name.startswith(SIDECAR_PREFIX):
        return None
    p = sidecar_path(audio)
    if not p.exists():
        return None
    try:
        meta = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("sidecar %s unreadable (%s); treating %s as a voice memo",
                    p.name, e, audio.name)
        return None
    return meta if isinstance(meta, dict) and meta.get("video_id") else None


def _tidy(cfg: Config, con, sha: str, audio: Path, detail: str = "") -> None:
    """pushed → done: move audio to processed/ (FR-34); a hand-off sidecar
    has done its job by now and is removed rather than archived."""
    if audio.exists():
        dest = scan.move_to_processed(audio, audio.parent, cfg)
        log.info("tidied %s -> %s", audio.name, dest.parent.name)
    sc = sidecar_path(audio)
    if audio.name.startswith(SIDECAR_PREFIX) and sc.exists():
        sc.unlink()
    ledger.set_state(con, sha, "done", detail)


def _push_and_tidy(cfg: Config, con, sha: str, audio: Path) -> None:
    clone = cfg.vault_clone
    try:
        vaultops.push(cfg, clone)
    except vaultops.PushDeferred as e:
        log.warning("push deferred (commit is safe locally): %s", e)
        return
    ledger.set_state(con, sha, "pushed")
    _tidy(cfg, con, sha, audio)


def resume_pending(cfg: Config, con) -> None:
    """Pushes owed from earlier ticks (FR-32). Works without the mount;
    tidying just waits until the audio's path is reachable again."""
    for row in ledger.rows_in_state(con, "committed"):
        clone = cfg.vault_clone
        try:
            vaultops.ensure_clone(cfg)
            vaultops.sync(cfg, clone)
        except (vaultops.SyncDeferred, vaultops.VaultError) as e:
            log.warning("resume: sync failed, will retry: %s", e)
            return
        _push_and_tidy(cfg, con, row["sha"], Path(row["path"]))
    for row in ledger.rows_in_state(con, "pushed"):
        _tidy(cfg, con, row["sha"], Path(row["path"]))


def process_file(cfg: Config, con, inbox: Path, audio: Path) -> None:
    sha = sha256_file(audio)
    row = ledger.get(con, sha)
    state = row["state"] if row else None

    if state in ledger.TERMINAL:
        dest = scan.move_to_processed(audio, inbox, cfg)
        sc = sidecar_path(audio)
        if audio.name.startswith(SIDECAR_PREFIX) and sc.exists():
            sc.unlink()
        log.info("duplicate audio %s (sha already %s) tidied to %s",
                 audio.name, state, dest.parent.name)
        return
    if state == "committed":        # crash between commit and push
        ledger.upsert(con, sha, path=str(audio), size=audio.stat().st_size,
                      state="committed")
        _push_and_tidy(cfg, con, sha, audio)
        return
    if state == "pushed":           # crash between push and tidy
        _tidy(cfg, con, sha, audio)
        return
    # state None, "new", or "failed" with its marker deleted: (re)process
    st = audio.stat()
    ledger.upsert(con, sha, path=str(audio), size=st.st_size, state="new")

    # A hand-off from the youtube source is public video audio, never a
    # work memo: the wall's filename rule does not apply (FR-05).
    meta = read_sidecar(audio)

    if meta is None and scan.is_walled(audio, cfg.walled_prefixes):
        out = _walled_out_path(cfg, audio)
        try:
            transcribe.run_whisper(cfg, audio, out)
        except transcribe.TranscriptionError as e:
            scan.write_marker(audio, str(e))
            ledger.set_state(con, sha, "failed", str(e)[:300])
            return
        scan.move_to_processed(audio, inbox, cfg)
        ledger.set_state(con, sha, "walled", detail=str(out))
        log.info("walled: %s transcribed local-only -> %s (never pushed)",
                 audio.name, out.name)
        return

    # A transcript from an attempt that failed later (vault down, push infra
    # broken) is kept and reused — re-transcribing an hour of audio because
    # git was unreachable would waste real GPU minutes. Reuse only what is
    # provably a finished transcript, never a crash artifact.
    tmp = cfg.tmp_dir / f"{sha}.md"
    tr = None
    if tmp.exists():
        cand = transcribe.parse(tmp.read_text())
        if cand.duration_seconds > 0 and (cand.no_speech or "**[" in cand.body):
            tr = cand
            log.info("reusing transcript from an earlier attempt for %s",
                     audio.name)
    if tr is None:
        try:
            transcribe.run_whisper(cfg, audio, tmp)
        except transcribe.TranscriptionError as e:
            scan.write_marker(audio, str(e))
            ledger.set_state(con, sha, "failed", str(e)[:300])
            return
        tr = transcribe.parse(tmp.read_text())

    if tr.no_speech:
        scan.move_to_processed(audio, inbox, cfg)
        if meta is not None and sidecar_path(audio).exists():
            sidecar_path(audio).unlink()
        ledger.set_state(con, sha, "no_speech")
        tmp.unlink(missing_ok=True)
        log.info("no speech in %s; no note (FR-12)", audio.name)
        return

    try:
        clone = vaultops.ensure_clone(cfg)
        vaultops.reset_if_dirty(clone)
        vaultops.sync(cfg, clone)
    except vaultops.SyncDeferred as e:
        log.warning("vault sync deferred; %s stays queued: %s", audio.name, e)
        return
    except vaultops.VaultError as e:
        log.error("vault error; %s stays queued: %s", audio.name, e)
        return

    today = dt.date.today().isoformat()
    if meta is not None:
        topic = str(meta.get("topic") or cfg.youtube_topic)
        existing = note.vault_has_video(clone, topic, str(meta["video_id"]))
        if existing and existing[1] != "skipped":
            scan.move_to_processed(audio, inbox, cfg)
            sidecar_path(audio).unlink(missing_ok=True)
            ledger.set_state(con, sha, "done",
                             f"already in vault as {existing[0]}")
            log.info("%s already in vault as %s; nothing to do", audio.name,
                     existing[0])
            return
        if not meta.get("language") and tr.language:
            meta = {**meta, "language": tr.language}
        note_id, rel = note.write_youtube_note(
            clone, topic, meta=meta, transcript=tr.body, today=today,
            captions="whisper", replace=existing[2] if existing else None)
        title = note.sanitize_title(str(meta.get("title", "")))
        msg = f"youtube: {title} ({meta['video_id']})"
    else:
        existing_id = note.vault_has_sha(clone, cfg.topic, sha)
        if existing_id:                 # FR-22: survives a lost ledger
            scan.move_to_processed(audio, inbox, cfg)
            ledger.set_state(con, sha, "done",
                             f"already in vault as {existing_id}")
            log.info("%s already in vault as %s; nothing to do", audio.name,
                     existing_id)
            return
        recorded = dt.date.fromtimestamp(st.st_mtime).isoformat()
        note_id, rel = note.write_note(
            clone, cfg.topic, audio_name=audio.name, sha=sha,
            recorded=recorded, today=today, tr=tr)
        title = note.title_from_filename(audio.name)
        msg = f"voice: {title} ({audio.name})"

    # only the note: index.md and log.md are derived by the VPS (OKF, 2026-09-02)
    vaultops.commit(cfg, clone, [rel], msg)
    ledger.upsert(con, sha, path=str(audio), size=st.st_size,
                  state="committed", note_id=note_id)
    tmp.unlink(missing_ok=True)
    _push_and_tidy(cfg, con, sha, audio)


def _locked(cfg: Config):
    lock_file = cfg.lock_path.open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:         # FR-06: another instance is live
        lock_file.close()
        return None
    return lock_file


def run_once(cfg: Config) -> int:
    cfg.ensure_dirs()
    lock_file = _locked(cfg)
    if lock_file is None:
        return 0
    try:
        con = ledger.connect(cfg.ledger_path)
        resume_pending(cfg, con)
        if not cfg.voice_enabled:
            return 0
        inbox = scan.ensure_mounted(cfg, allow_remount=_remount_allowed(cfg))
        _log_mount_state(cfg, inbox is not None)
        if inbox is None:
            return 0
        swp = scan.sweep_inbox(inbox, con, cfg)
        for audio in swp.eligible:
            process_file(cfg, con, inbox, audio)
        return 0
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


# -- the youtube source ------------------------------------------------------

MAX_FETCH_ATTEMPTS = 3
LISTING_FAILURES_KEY = "listing_failures"
LAST_POLLED_KEY = "last_polled"


def _video_push(cfg: Config, con, video_id: str) -> None:
    try:
        vaultops.push(cfg, cfg.vault_clone)
    except vaultops.PushDeferred as e:
        log.warning("push deferred for %s (commit is safe locally): %s",
                    video_id, e)
        return
    ledger.video_upsert(con, video_id, state="done")


def _video_resume(cfg: Config, con) -> None:
    """Pushes owed from earlier ticks (FR-09, retried every tick)."""
    for row in ledger.video_rows_in_state(con, "committed"):
        try:
            vaultops.ensure_clone(cfg)
            vaultops.sync(cfg, cfg.vault_clone)
        except (vaultops.SyncDeferred, vaultops.VaultError) as e:
            log.warning("resume: sync failed, will retry: %s", e)
            return
        _video_push(cfg, con, row["video_id"])


def _write_and_publish(cfg: Config, con, meta: dict, *, transcript: str,
                       captions: str, learn: str, skip_reason: str = "",
                       replace: Path | None = None, attempts: int | None = None
                       ) -> None:
    clone = cfg.vault_clone
    today = dt.date.today().isoformat()
    note_id, rel = note.write_youtube_note(
        clone, cfg.youtube_topic, meta=meta, transcript=transcript,
        today=today, captions=captions, learn=learn, skip_reason=skip_reason,
        replace=replace)
    title = note.sanitize_title(str(meta.get("title", "")))
    vaultops.commit(cfg, clone, [rel], f"youtube: {title} ({meta['video_id']})")
    ledger.video_upsert(con, str(meta["video_id"]), state="committed",
                        title=title, note_id=note_id,
                        last_error=skip_reason, attempts=attempts,
                        outcome="skipped" if learn == "skipped" else "transcript")
    _video_push(cfg, con, str(meta["video_id"]))


def _fail(con, video_id: str, title: str, err: str, attempts: int) -> None:
    ledger.video_upsert(con, video_id, state="failed", title=title,
                        last_error=err[:300], attempts=attempts)
    log.warning("video %s: attempt %d failed: %s", video_id, attempts, err)


def process_video(cfg: Config, con, entry: dict) -> None:
    """One playlist entry through the state machine (FR-02..FR-09)."""
    video_id = entry["id"]
    row = ledger.video_get(con, video_id)
    if row and row["state"] in ledger.VIDEO_TERMINAL:
        return
    if row and row["state"] == "committed":
        _video_push(cfg, con, video_id)
        return
    attempts = (row["attempts"] if row else 0) + 1
    title = entry.get("title", "")

    clone = cfg.vault_clone
    existing = note.vault_has_video(clone, cfg.youtube_topic, video_id)
    if existing and existing[1] != "skipped":     # FR-02 backstop
        ledger.video_upsert(con, video_id, state="done", title=title,
                            note_id=existing[0],
                            last_error=f"already in vault as {existing[0]}")
        log.info("%s already in vault as %s; nothing to do", video_id,
                 existing[0])
        return
    replace = existing[2] if existing else None

    # Three failed fetches → a `skipped` stub so the sheet reports it once
    # and the tick stops spending YouTube's patience on it (§10).
    if row and row["state"] == "failed" and row["attempts"] >= MAX_FETCH_ATTEMPTS:
        meta = {"video_id": video_id, "title": title or row["title"],
                "channel": "", "duration": entry.get("duration", 0),
                "upload_date": "", "language": "",
                "url": youtube.video_url(video_id)}
        _write_and_publish(cfg, con, meta, transcript="", captions="none",
                           learn="skipped",
                           skip_reason=f"fetch failed {row['attempts']} times "
                                       f"({row['last_error'][:80]})",
                           replace=replace, attempts=row["attempts"])
        return

    try:
        info = youtube.fetch_info(cfg, video_id)
    except youtube.FetchError as e:
        _fail(con, video_id, title, str(e), attempts)
        return
    meta = info.as_meta()
    title = note.sanitize_title(info.title or title)

    if cfg.max_minutes and info.duration > cfg.max_minutes * 60:
        _write_and_publish(cfg, con, meta, transcript="", captions="none",
                           learn="skipped",
                           skip_reason=f"too long ({note.human_duration(info.duration)}; "
                                       f"cap {cfg.max_minutes} min)",
                           replace=replace, attempts=attempts)
        return

    choice = youtube.pick_captions(info, cfg.languages)
    if choice is None:
        if cfg.whisper_fallback:
            try:
                audio = youtube.download_audio(cfg, video_id, Path(cfg.handoff_dir))
            except youtube.FetchError as e:
                _fail(con, video_id, title, str(e), attempts)
                return
            sidecar = audio.with_name(f"{audio.stem}.json")
            sidecar.write_text(json.dumps({**meta, "topic": cfg.youtube_topic},
                                          ensure_ascii=False, indent=1))
            ledger.video_upsert(con, video_id, state="handed_off", title=title,
                                last_error=f"no captions; audio -> {audio.name}",
                                attempts=attempts)
            log.info("%s has no captions; handed %s to the voice inbox",
                     video_id, audio.name)
            return
        _write_and_publish(cfg, con, meta, transcript="", captions="none",
                           learn="skipped", skip_reason="no captions",
                           replace=replace, attempts=attempts)
        return

    kind, track = choice
    work = cfg.tmp_dir / f"yt-{video_id}"
    shutil.rmtree(work, ignore_errors=True)
    try:
        vtt = youtube.fetch_captions(cfg, video_id, kind, track, work)
        transcript = youtube.vtt_to_transcript(vtt.read_text(errors="replace"))
    except youtube.FetchError as e:
        _fail(con, video_id, title, str(e), attempts)
        return
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not transcript.strip():
        _fail(con, video_id, title, f"{kind} track {track} was empty", attempts)
        return
    meta["language"] = info.language or track.split("-", 1)[0]
    _write_and_publish(cfg, con, meta, transcript=transcript, captions=kind,
                       learn="requested", replace=replace, attempts=attempts)
    log.info("%s -> transcript note (%s captions, %s)", video_id, kind, track)


def youtube_tick(cfg: Config, con=None, *, force: bool = False,
                 now: float | None = None) -> int:
    """One tick: owed pushes, then (if the poll clock says so) one listing
    and up to max_per_tick new videos. Returns the number processed."""
    cfg.ensure_dirs()
    own_con = con is None
    con = con or ledger.connect(cfg.ledger_path)
    now = time.time() if now is None else now
    try:
        _video_resume(cfg, con)
        last = float(ledger.meta_get(con, LAST_POLLED_KEY, "0") or 0)
        if not force and now - last < cfg.poll_minutes * 60:
            return 0
        try:
            entries = youtube.list_playlist(cfg)
        except youtube.FetchError as e:
            n = int(ledger.meta_get(con, LISTING_FAILURES_KEY, "0") or 0) + 1
            ledger.meta_set(con, LISTING_FAILURES_KEY, str(n))
            if n == 1 or n == MAX_FETCH_ATTEMPTS:
                log.warning("playlist listing failed (%d in a row): %s", n, e)
            ledger.meta_set(con, LAST_POLLED_KEY, str(now))
            return 0
        if int(ledger.meta_get(con, LISTING_FAILURES_KEY, "0") or 0):
            log.info("playlist listing works again")
            ledger.meta_set(con, LISTING_FAILURES_KEY, "0")
        ledger.meta_set(con, LAST_POLLED_KEY, str(now))

        todo = [e for e in entries
                if not (r := ledger.video_get(con, e["id"]))
                or r["state"] not in ledger.VIDEO_TERMINAL]
        if not todo:
            return 0
        try:
            vaultops.ensure_clone(cfg)
            vaultops.reset_if_dirty(cfg.vault_clone)
            vaultops.sync(cfg, cfg.vault_clone)
        except vaultops.SyncDeferred as e:
            log.warning("vault sync deferred; %d video(s) wait: %s", len(todo), e)
            return 0
        except vaultops.VaultError as e:
            log.error("vault error; %d video(s) wait: %s", len(todo), e)
            return 0
        done = 0
        for entry in todo[:cfg.max_per_tick]:
            process_video(cfg, con, entry)
            done += 1
        if len(todo) > cfg.max_per_tick:
            log.info("%d more video(s) wait for the next tick",
                     len(todo) - cfg.max_per_tick)
        return done
    finally:
        if own_con:
            con.close()


def youtube_once(cfg: Config) -> int:
    lock_file = _locked(cfg)
    if lock_file is None:
        return 0
    try:
        youtube_tick(cfg, force=True)
        return 0
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
