# Voice note → knowledge vault bridge — Requirements

**Project:** ozu-capture (developed under the codename VOICEBRIDGE)
**Status:** v1.0, approved 2026-08-29 and built; Amendment 1 (YouTube) at the end
**Author:** Sergio dos Santos
**Date:** 2026-08-29
**Target machine:** Apple M1, 8 GB (the only WHISPER-capable machine), macOS 26.5.0

---

## 1. Overview

A thin bridge that turns a voice memo dropped on the NAS into a note in the
Ozu knowledge vault. It watches a NAS folder, feeds new audio to the
already-built WHISPER CLI (on-device MLX transcription), transforms the §7
transcript into an Ozu-vault-conforming Markdown note, and pushes it to the
vault's git origin — where the VPS's 2-hourly `ozu run` folds it in and
indexes it, exactly as it already does for phone Obsidian edits.

```
phone → NAS VoiceInbox (SMB) → Mac bridge (launchd poll)
     → whisper-notes CLI (§7 .md) → Ozu-note transform
     → git commit + push to you/vault
     → VPS sync_from_origin + reindex (existing, every 2h)
     → graph / MCP / digest
```

### 1.1 Discovery findings this design rests on (verified 2026-08-29)

| Fact | Evidence |
|------|----------|
| `ozu run` calls `vault.sync_from_origin()` **first**, and reindexes when anything arrived | ozu `src/ozu/pipeline.py` |
| Offsite pushes to the vault origin are a designed, documented lane (phone Obsidian) | ozu `vault.sync_from_origin` docstring |
| Vault origin is the **private** repo `github.com/you/vault` (404 unauthenticated) | VPS `git remote -v`; curl check |
| The Mac can authenticate pushes to it (HTTPS + osxkeychain) | `git push --dry-run` reached ref negotiation |
| The "nightly" timer actually runs **every 2 hours** (`00/2:30 UTC`) | ozu `deploy/systemd/ozu-nightly.timer` |
| WHISPER v1 is complete; `uv run --project <dir> whisper-notes transcribe <file> --out <path>` works from any cwd, exits non-zero on failure | WHISPER `.handover/HANDOVER.md`; tested |
| WHISPER enforces its FR-01 extension allowlist (`.amr`/`.3gp` rejected) | `whisper_notes/core/audio.py::ALLOWED_EXTENSIONS` |
| Notes need frontmatter `id` or the indexer skips them; topic = parent dir name; single-line frontmatter values only | ozu `src/ozu/indexer.py` |
| The NAS (host `nas.local`) is mounted and sub-ms away; SMB writes work | live mount check |

### 1.2 The privacy boundary (approved 2026-08-29)

WHISPER promises zero egress and keeps it — the bridge, not WHISPER, is the
component that exports. **Transcript text leaves the Mac**: to the private
GitHub repo `you/vault`, then into the VPS index, then out over
the bearer-token MCP endpoint. This is the same boundary every existing vault
note already crosses. **Audio never leaves the Mac/NAS**; inference stays
on-device. The owner approved this boundary and this specific remote on
2026-08-29, before any push exists. The bridge SHALL refuse to push to any
other remote (FR-33).

---

## 2. Goals and non-goals

### 2.1 Goals

| # | Goal |
|---|------|
| G1 | A memo dropped in the NAS VoiceInbox becomes a queryable vault note with no manual step |
| G2 | End-to-end idempotence: no re-run, crash, rename, or re-drop can duplicate a note |
| G3 | Full Ozu vault conformance: frontmatter, registries, topic layout — indistinguishable from a `vault.py`-written note |
| G4 | Honest failure: a failed file stays in place with a visible marker and a log line, never a silent drop |
| G5 | Phone capture works from both iPhone and Android via configuration only |
| G6 | Employer-content governance at least as strong as Ozu's (here it can be *pre*-egress, since transcription is local) |

### 2.2 Non-goals (v1)

| # | Non-goal | Rationale |
|---|----------|-----------|
| N1 | Any change to the WHISPER repo | Its §2.2/§13.3 scope is binding; folder-watching was explicitly deferred *out* of it. The bridge is a separate thin client of the CLI. |
| N2 | Any change to Ozu | The direct-push lane needs none. If an Ozu change ever becomes necessary it is its own project with its own handover discipline. |
| N3 | Summarization, action extraction, spoken `@project` tags | Requires an LLM pass; deferred (§13.1). `topics/voice/` is the fixed v1 destination. |
| N4 | Phone-side code | Capture is configuration (§12 checklists). The bridge only ever watches the folder. |
| N5 | Transcribing work/employer memos into the vault | Governance rule (§6.4). Walled memos are transcribed locally and never pushed. |
| N6 | A UI | It is a daemon. `ozu-capture status` is the whole interface. |

---

## 3. Primary flow

1. The owner records a memo on either phone; the phone app uploads it to the
   NAS share `VoiceInbox` (§12).
2. Within one poll interval (default 120 s) the Mac bridge sees the file,
   waits until its size/mtime are stable across two polls, computes its
   sha256, and checks the ledger.
3. New file → `whisper-notes transcribe` runs (one at a time, on-device).
4. The §7 transcript is transformed into a the vault note under
   `topics/voice/`, named by its id, in OKF v0.2 form. No registry is
   written — the VPS derives `index.md`/`log.md` from the frontmatter.
5. The bridge pulls `--rebase`, commits, and pushes to the vault origin.
6. The audio file moves to `VoiceInbox/processed/` on the NAS.
7. Within ≤2 h the VPS folds the note in and indexes it; it is now visible to
   `search_notes` over MCP, the graph, and the weekly digest.

---

## 4. Constraints

| # | Constraint | Impact |
|---|-----------|--------|
| C1 | The M1/8 GB Mac is the only WHISPER-capable machine and must be awake | launchd LaunchAgent, runs while logged in; memos dropped while asleep are picked up on wake (accepted 2026-08-29). No pmset wakes. |
| C2 | One transcription at a time (WHISPER's 8 GB constraint is binding) | The bridge serializes its own queue; launchd runs one instance of the job at a time, plus a lockfile belt-and-braces. |
| C3 | FSEvents is unreliable on SMB mounts | Poll, don't watch. |
| C4 | SMB mounts vanish after sleep/reboot | Every run re-checks the mount and attempts a keychain-backed remount before scanning (FR-02). |
| C5 | Stdlib-first, no heavyweight deps | Zero third-party packages. `uv` (already installed, house toolchain) pins Python 3.12 for a stable absolute interpreter path under launchd. |
| C6 | WHISPER's FR-01 allowlist excludes Android-recorder defaults (`.amr`, `.3gp`) | Bridge accepts exactly WHISPER's list; anything else gets a `.failed` marker with remediation, and §12 documents setting recorders to m4a. |
| C7 | The vault working copy on the VPS wins conflicts; offsite pushes must be conflict-free to be timely | Dedicated `topics/voice/` dir + collision-suffixed filenames ⇒ no same-file conflicts by construction. |

---

## 5. Technology decisions

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Language / runtime | Python 3.12 via `uv`, **zero third-party dependencies** | stdlib has everything needed (`subprocess`, `hashlib`, `sqlite3`, `tomllib`, `logging`). uv gives launchd a stable absolute interpreter. |
| Scheduling | launchd LaunchAgent, `StartInterval` 120 s, short-lived process per tick | Crash-safe by construction (no long-lived state), single-instance for free, no root. |
| Transcription | `uv run --project ~/ozu/transcribe whisper-notes transcribe <file> --out <tmp.md>` via `subprocess` argument list | The sanctioned integration surface. WHISPER repo untouched (N1). |
| State ledger | stdlib `sqlite3` at `./data/ledger.db` | Survives crashes mid-state-machine; rebuildable — the vault-side sha check (FR-22) means a lost ledger cannot cause duplicates. |
| Vault access | Dedicated clone at `./data/vault/` (gitignored), HTTPS remote, osxkeychain auth | Never disturbs the owner's working clone at `~/vault`. |
| Config | `config.toml` via stdlib `tomllib` | Read-only config; no dependency. No secrets in it — auth lives in the keychain. |
| NAS folder | **New dedicated share `VoiceInbox`** on the NAS, mounted at `/Volumes/VoiceInbox` | Decided 2026-08-29 (over reusing an existing share). Requires the §12.0 NAS checklist before the E2E phase. |

---

## 6. Functional requirements

### 6.1 Watching and pickup

| ID | Requirement |
|----|-------------|
| **FR-01** | The bridge SHALL poll the configured inbox directory on a launchd `StartInterval` (default 120 s, configurable). No FSEvents/watchdog (C3). |
| **FR-02** | Each tick SHALL verify the SMB mount is present; if absent it SHALL attempt one keychain-backed remount (`osascript` `mount volume "smb://user@nas.local/VoiceInbox"`), and on failure log once per state change and exit 0 (quiet retry next tick). |
| **FR-03** | The bridge SHALL consider only files whose extension is in WHISPER's FR-01 allowlist (`.m4a .mp3 .wav .aac .flac .ogg .opus .wma .aiff .mp4 .mov .webm .mkv`). Hidden files (dot-prefixed), zero-byte files, and files carrying a `.failed.txt` marker SHALL be ignored. Files with any other extension SHALL receive a `.failed.txt` marker naming the remediation (record as m4a / convert) — once, not per tick. |
| **FR-04** | **Quiesce gate:** a file SHALL NOT be picked up until its (size, mtime) is unchanged between two observations at least 60 s apart. A half-uploaded file is therefore never processed. |
| **FR-05** | On pickup the bridge SHALL compute the file's SHA-256. The sha, not the filename, is the identity: a ledger entry in a terminal state (`done`, `walled`, `no_speech`) for that sha SHALL end processing (the duplicate audio file is moved to `processed/` with a log line, producing nothing). |
| **FR-06** | Exactly one transcription SHALL run at a time; eligible files are processed oldest-mtime-first within a tick. A lockfile SHALL prevent overlapping bridge instances regardless of how the process was started. |

### 6.2 Transcription

| ID | Requirement |
|----|-------------|
| **FR-10** | Transcription SHALL be a `subprocess` invocation (argument list, never a shell string) of the WHISPER CLI, writing to a temp path inside the bridge's own data dir — never into the WHISPER repo or the NAS. |
| **FR-11** | A non-zero CLI exit SHALL fail the file: audio left in place, `<name>.failed.txt` written beside it carrying the last lines of stderr, ledger marked `failed`, log line emitted. A `.failed.txt` marker SHALL exclude the file from future ticks; deleting the marker is the retry gesture. |
| **FR-12** | A transcript whose body is `_No speech detected._` SHALL NOT produce a vault note. The audio moves to `processed/`, the ledger records `no_speech`, and a log line says so. Pocket recordings must not become vault noise. |
| **FR-13** | The §7 frontmatter (language, language_confidence, duration, word_count, model) SHALL be parsed and carried into the note's provenance. |

### 6.3 Note generation (Ozu vault conformance)

| ID | Requirement |
|----|-------------|
| **FR-20** | Notes SHALL be written under `topics/voice/` with filename `<slug>.md` and frontmatter `id: YYYYMMDD-<slug>`, applying `vault.py`'s exact collision rule (numeric suffix on both id and filename). The date is the **audio file's mtime date** (recording day), not the processing day. |
| **FR-21** | Frontmatter SHALL be single-line-per-key (the indexer's parser requires it) and carry: `id`, `title` (cleaned from the audio filename), `tags: [voice, transcript]`, `created` (recording date), `updated` (processing date), `source: voice:<original filename>`, `source_sha256`, `duration`, `language`, `needs_review`, `related: []`, `summary`. `needs_review: true` when `language_confidence < 0.8` — the same honesty rule Ozu applies to OCR confidence. |
| **FR-22** | **Vault-side dedup backstop:** before writing, the bridge SHALL scan `topics/voice/*.md` frontmatter for an existing `source_sha256` equal to this file's. A hit ends processing as `done` — so even a deleted ledger cannot duplicate a note (G2). |
| **FR-23** | The note body SHALL be the transcript's paragraph stream (with its `**[HH:MM:SS]**` timestamps), preceded by the `> Summary:` line, matching the house note shape. WHISPER already escapes `---` in transcript text; the bridge SHALL NOT emit a second frontmatter block. |
| **FR-24** | *(Superseded 2026-09-02 — the vault is an OKF v0.2 bundle, ozu `docs/okf.md`.)* The bridge SHALL NOT write any registry. The note SHALL carry `type: Voice Memo` (`Transcript` for YouTube), `description`, `generated: { by, at }` and `sources`, in strict YAML matching `okf.dump_frontmatter`, and SHALL be named `<id>.md`. `index.md` and `log.md` are derived by the VPS on its next `ozu index`. |

### 6.4 Governance (employer wall)

| ID | Requirement |
|----|-------------|
| **FR-25** | A filename whose stem starts (case-insensitively) with a walled prefix (`work` by default — mirroring Ozu's `OZU_WORK_PREFIXES`) SHALL be transcribed **locally only**: the transcript is written to `./data/walled/` (gitignored, never committed, never pushed), the audio moves to `processed/`, the ledger records `walled`. Nothing reaches the vault clone, the origin, or the VPS. |
| **FR-26** | Because transcription is on-device, this wall is **pre-egress** — stronger than Ozu's post-API wall, and the docs SHALL say so plainly rather than overclaiming Ozu's. The bridge cannot classify *content*; the documented rule remains: work memos don't go in VoiceInbox. The prefix is the backstop, same as Ozu's. |

### 6.5 Vault publish

| ID | Requirement |
|----|-------------|
| **FR-30** | Before writing, the bridge SHALL `git pull --rebase` its vault clone. On failure it SHALL `rebase --abort` and defer the file to the next tick (never force, never push over a failed rebase) — the same discipline as `sync_from_origin`. |
| **FR-31** | Each note SHALL be one commit: note file + both registries, message `voice: <title> (<original filename>)`, authored with the owner's git identity. |
| **FR-32** | Push failure (network, non-fast-forward) SHALL NOT lose work: the commit stays local, the ledger records `committed`, and every subsequent tick retries rebase+push until `pushed`. The ledger state machine (`seen → stable → transcribed → committed → pushed/done`) makes every step re-entrant; a crash between any two states resumes without duplication. |
| **FR-33** | The bridge SHALL refuse to push if the clone's `origin` URL is not the configured, approved remote (§1.2). A mismatch is a hard error, loudly logged — never a silent push elsewhere. |
| **FR-34** | On successful push the audio SHALL move to `VoiceInbox/processed/` (NAS-side move — audio never leaves the Mac/NAS). |

### 6.6 Observability and operation

| ID | Requirement |
|----|-------------|
| **FR-40** | Structured log at `./data/ozu-capture.log` with rotation. Every file logs: pickup (sha, size), each state transition, outcome. Every tick with nothing to do logs nothing (quiet). |
| **FR-41** | `ozu-capture status` SHALL print: mount state, ledger counts per state, files currently ineligible (unstable / failed with marker), and the last 5 outcomes. |
| **FR-42** | `ozu-capture run-once` SHALL execute a single tick in the foreground (the launchd job runs exactly this). `ozu-capture install-agent` SHALL render and load the LaunchAgent plist; `uninstall-agent` reverses it. |

---

## 7. Note output specification

```markdown
---
id: 20260829-garden-ideas
title: Garden ideas
tags: [voice, transcript]
created: 2026-08-29
updated: 2026-08-29
source: voice:Garden ideas.m4a
source_sha256: 3f9a1c...e77b
duration: "00:07:41"
language: es
needs_review: false
related: []
summary: Voice memo (7m 41s, Spanish), transcribed on-device 2026-08-29.
---

# Garden ideas

> Summary: Voice memo (7m 41s, Spanish), transcribed on-device 2026-08-29.

**[00:00:00]** Primer párrafo del memo...

**[00:01:47]** Siguiente párrafo...
```

**Notes on the schema**

- Everything the indexer reads (`id`, `title`, `tags`, `created`, `summary`,
  `needs_review`, `related`) matches `vault.py`'s conventions exactly; the
  additions (`source_sha256`, `duration`, `language`) are provenance the
  indexer ignores but FR-22 and humans rely on.
- `related` starts empty; wikilinks added later by hand become edges on the
  next VPS reindex — for free, because the indexer extracts `[[id]]` from
  bodies.
- No `## Transcript` heading: Ozu notes are body-first, and a future
  summarization pass (§13.1) would edit the note like any other.

---

## 8. Architecture

```
ozu-capture/
├── REQUIREMENTS.md            ← this document
├── README.md                  ← operation + phone-capture checklists (P5)
├── pyproject.toml             ← uv project, Python 3.12, zero deps
├── config.toml                ← paths, prefixes, poll/quiesce intervals, approved remote
├── src/ozu_capture/
│   ├── config.py              ← tomllib loader + validation
│   ├── ledger.py              ← sqlite3 state machine (sha-keyed)
│   ├── scan.py                ← mount check/remount, accept list, quiesce, wall prefixes, markers
│   ├── transcribe.py          ← WHISPER CLI subprocess + §7 parse
│   ├── note.py                ← §7 → Ozu-note transform (frontmatter map, registries)
│   ├── vaultops.py            ← clone mgmt: rebase, write, commit, push, remote guard
│   └── cli.py                 ← run-once | status | install-agent | uninstall-agent
├── launchd/dev.ozu.capture.plist   ← template, rendered by install-agent
├── tests/                     ← unittest; CLI mocked; throwaway bare repo as origin
└── data/                      ← gitignored: ledger.db, ozu-capture.log, vault/, walled/, tmp/
```

**Key design principle:** the bridge owns *no* content logic — WHISPER owns
transcription, Ozu owns the graph. The bridge is a state machine over
(file, sha) that happens to call one CLI and one git remote. Every stage is
re-entrant; the ledger plus the vault-side sha check make "run it again" the
universal recovery action.

---

## 9. Non-functional requirements

| ID | Requirement |
|----|-------------|
| **NFR-01** | **Privacy:** audio never leaves the Mac/NAS; transcript text egresses only via git push to the approved remote (§1.2, FR-33). No other network calls exist in the bridge. |
| **NFR-02** | **Idempotence** is end-to-end and survives: crash at any state, ledger loss (FR-22), file rename, re-drop, launchd re-runs. "At most one note per audio sha, ever." |
| **NFR-03** | **Footprint:** the bridge itself SHALL stay under 50 MB RSS (it is I/O and subprocess glue); WHISPER's memory budget is WHISPER's. |
| **NFR-04** | **Zero third-party dependencies.** `uv.lock` proves it. |
| **NFR-05** | **No secrets anywhere in the repo or config.** Git auth is the osxkeychain; SMB auth is the login keychain. |
| **NFR-06** | **The WHISPER and Ozu repos are not modified** (N1, N2). CI-style check: the build diff for every phase touches only `~/ozu-capture`. |
| **NFR-07** | **Quiet when idle:** an empty tick performs one `stat` sweep and exits; no log noise, no git traffic (rebase only happens when there is something to publish). |

---

## 10. Error handling

| Condition | Behavior |
|-----------|----------|
| NAS share not mounted | One remount attempt (FR-02); on failure, log on state change only, exit 0, retry next tick. |
| File still growing / just uploaded | Not eligible until stable ≥60 s across ticks (FR-04). No marker, no log. |
| Unsupported extension (`.amr`, `.3gp`, …) | `.failed.txt` marker with remediation ("set recorder to m4a", conversion hint), once. Audio stays. |
| WHISPER CLI exits non-zero | Audio stays; `.failed.txt` with stderr tail; ledger `failed`; log line. Delete the marker to retry. |
| WHISPER CLI missing / uv broken | Systemic, not per-file: no markers written; loud log; tick exits non-zero (visible in `launchctl` status and `ozu-capture status`). |
| Silence / no speech | No note; audio → `processed/`; ledger `no_speech`; log line (FR-12). |
| Walled prefix | Transcript to `./data/walled/` only; audio → `processed/`; ledger `walled`; log line (FR-25). |
| `git pull --rebase` fails | Abort rebase, defer to next tick, warn. Never force (FR-30). |
| Push rejected / network down | Commit stays local, state `committed`, retried every tick until pushed (FR-32). |
| Origin URL ≠ approved remote | Hard error, loud log, nothing pushed (FR-33). |
| Ledger lost/corrupt | Rebuilt conservatively from scratch; FR-22's vault-side sha scan prevents duplicates. |
| Duplicate audio (same sha, any name) | Moved to `processed/`, log line, nothing else (FR-05). |
| Crash mid-anything | Next tick resumes from the ledger state; partial temp files are in `data/tmp/` and swept. |

---

## 11. Acceptance criteria

The build is complete when all of the following pass:

1. **The real E2E:** a real memo dropped on the real `VoiceInbox` share is
   transcribed and its note is committed, pushed, and — after the next VPS
   cycle (or a manual `ozu run`) — returned by `search_notes` over MCP with
   its spoken words searchable. Measured, not assumed.
2. Re-dropping the same audio under a different filename produces no second
   note and no second commit; the duplicate file is tidied to `processed/`.
3. A file simulating a slow upload (append loop) is not picked up until it
   stabilizes, then processes normally.
4. `kill -9` during transcription: next tick re-picks the file; exactly one
   note results. `kill -9` after commit but before push: next tick pushes;
   no duplicate commit.
5. Deleting `data/ledger.db` and re-running against a vault already
   containing the note produces no duplicate (FR-22 proven).
6. A `work_*.m4a` memo yields a transcript in `data/walled/` only; the vault
   clone shows no new file and no new commit; grep of the pushed history
   confirms absence.
7. An `.amr` file receives exactly one `.failed.txt` with remediation and is
   skipped on subsequent ticks; deleting the marker retries it.
8. A silent recording yields no vault note and a `no_speech` ledger entry.
9. The bridge touches no registry file; a VPS `ozu index` shows the note
   with topic `voice` and the correct id, lists it in `topics/voice/index.md`,
   and `ozu okf check` reports no error for it.
10. The LaunchAgent survives logout/login and never runs two instances
    (rapid manual `launchctl kickstart` attempts included).
11. Unplugging the NAS (or unmounting) mid-operation loses nothing: ticks go
    quiet, and everything resumes when the mount returns.
12. **Phone capture, both platforms:** one memo recorded on the iPhone and
    one on the Android phone each land in `VoiceInbox` via the documented
    §12 checklists and flow end to end. Confirmed by the owner, not simulated.

---

## 12. Configuration checklists (owner-confirmed, not code)

### 12.0 NAS: create the dedicated share (blocking for P4)

On the NAS admin UI:

- [ ] Create shared folder **`VoiceInbox`** (Control Panel → Shared Folder →
      Create; on the main storage pool).
- [ ] Grant the user the Mac mounts it as read/write on it. Grant the phone
      user(s) (§12.1/§12.2) read/write as well — create a dedicated
      low-privilege `phone` user if preferred.
- [ ] Create subfolder `processed/` inside it (the bridge will also create it
      if absent).
- [ ] On the Mac: Finder → Go → Connect to Server → `smb://user@nas.local/VoiceInbox`,
      tick **"Remember this password in my keychain"** (that keychain entry is
      what FR-02's auto-remount uses).
- [ ] Drop a short test memo, `Garden ideas.m4a`, into the new share —
      it becomes AC 1's test input.

### 12.1 iPhone capture (exact steps at P5; the shape is)

Voice Memos → share sheet → **NAS vendor's app** ("Save to NAS" →
`VoiceInbox`), or Files.app with the SMB server connected → Save to
`VoiceInbox`. Documented with exact taps and values in README at P5;
confirmed by AC 12.

### 12.2 Android capture (exact steps at P5; the shape is)

**FolderSync** (or the NAS vendor's app's auto-upload): pair recorder output folder
→ SMB2 target `nas.local/VoiceInbox`, upload-only, delete-after-upload
off; **recorder set to m4a** (C6). Documented with exact values in README at
P5; confirmed by AC 12.

---

## 13. Deferred work (explicitly out of scope for v1)

### 13.1 Summarization / spoken routing
A local LLM pass (or an Ozu-side agent) adding `> Summary:` quality summaries,
action extraction, and spoken "at-project" tags routing to real topics.
Touches Ozu conventions; needs its own decision. The §7 transform keeps the
note shape ready for it.

### 13.2 Multi-machine resilience
Anything that removes the "Mac must be awake" constraint (a VPS whisper.cpp
lane, a NAS-side container) trades away on-device MLX; not worth it at
current volume.

### 13.3 Other candidates
Per-memo language forcing via filename hint · translate lane (needs a
non-turbo model, per WHISPER's amendment) · retention policy for
`processed/` · Scribe delivery of transcripts (Ozu Mail lane).

---

## 14. Risks

| ID | Risk | Severity | Mitigation |
|----|------|----------|------------|
| R1 | SMB quirks: mtime granularity, phantom sizes during upload | Medium | Quiesce on (size, mtime) across ≥60 s; AC 3 tests a simulated slow upload on the real share. |
| R2 | Push races with the VPS's own `ExecStartPost` vault push | Low | Rebase-before-push each attempt; dedicated dir means conflicts are structurally impossible; the retry loop (FR-32) absorbs transient rejects. |
| R3 | osxkeychain HTTPS credential expires (PAT rotation) | Medium | Push failures are non-lossy (FR-32) and visible in `status`; README documents the re-auth step. |
| R4 | The phone apps silently stop uploading | Medium | Out of bridge scope by design (N4); the weekly digest's silence is the practical alarm. §12 prefers upload-only modes with visible app state. |
| R5 | A work memo without the prefix gets pushed | Medium | Cannot be prevented by v1 mechanically (no classifier); §6.4 says so plainly — the rule is behavioral, the prefix is the backstop, same honesty as Ozu's wall docs. |
| R6 | Registry-format drift if Ozu's `vault.py` conventions change | Low | AC 9 pins byte-compatibility today; Ozu invariant 6 changes are rare and would surface in the VPS reindex. |

---

## 15. Open questions

None blocking. Build-time judgment calls with defaults noted: poll interval
(120 s), quiesce window (60 s), walled-transcript retention (keep forever in
`data/walled/`), `processed/` retention (keep forever; NAS disk is cheap).

---

## 16. Build phases

| Phase | Deliverable | Exit criterion |
|-------|-------------|----------------|
| **P0 — Foundation** | uv project (3.12, zero deps), config loader + validation, ledger schema + state machine, logging, repo skeleton | Unit tests green for config + every ledger transition; `uv run ozu-capture status` runs on an empty world |
| **P1 — Scan** | Mount check/remount, accept list, quiesce gate, wall-prefix detection, `.failed` markers | AC 3 (simulated) and AC 7 logic pass against a fixture dir; unstable files provably ineligible |
| **P2 — Transcribe + transform** | WHISPER subprocess wrapper, §7 parser, note generator, registry writer | Golden-file tests: generated note + registries byte-match the §7 spec transform (CLI mocked); FR-12/FR-25 paths covered |
| **P3 — Publish** | Vault clone mgmt, rebase/commit/push state machine, remote guard, FR-22 backstop | AC 2, 4, 5, 6 pass against a **throwaway local bare repo** as origin — no real push yet |
| **P4 — Live** | LaunchAgent install, real clone of the real vault, first real push (gated on §12.0 checklist done) | AC 1, 10, 11 measured on the real share, real origin, real VPS cycle — `Garden ideas.m4a` end to end |
| **P5 — Capture + docs** | README, §12.1/§12.2 exact-step checklists, handover note | AC 12 confirmed by the owner from both phones; AC 8, 9 verified on real artifacts |

One commit per phase, message written for a reader who wasn't here. The
first real push to the vault origin happens in P4 and not before (§1.2).

---

## 17. Sign-off

Approval of this document authorizes building phases P0–P5 as specified.
Changes to §2.2 (non-goals) or §6 (functional requirements) require an
amendment to this document before implementation. The privacy boundary in
§1.2 was approved 2026-08-29; the approved remote is
`https://github.com/you/vault.git`.

---

# Amendment 1 — the YouTube source (2026-09-02)

**Approved 2026-09-01. Governing document: the
Learn feature's own spec in Ozu**, which owns the requirement ids cited as
"Amendment 1 FR-xx" in the code. This amendment
records what changes *here*, because v1.0's shape — one source, one
deployment, the Mac — no longer describes the repo.

## A1.1 What the amendment overturns

| v1.0 said | Now |
|---|---|
| §1 "a memo dropped on the NAS becomes a note" — voice is the only source | Two sources: **voice** (NAS share → WHISPER on the Mac) and **youtube** (a playlist → captions). The bridge is the *capture bridge*, not the voice bridge. |
| §2.2 N2 "no change to Ozu" | Superseded. Ozu gains the `learn:` directive, the extraction run and the evening sheet; this repo gains the source that feeds them. The two were designed together. |
| §4 C1 "the Mac must be awake" | True for voice only. The youtube source runs in a container on the NAS, which is always on. The Mac is now the *fallback* transcriber: a caption-less video is handed to it as audio in `VoiceInbox` (its lane, unchanged). |
| §5 "Scheduling: launchd" | Two deployments of one codebase: the Mac LaunchAgent runs `run-once` (voice); the NAS container runs `youtube-loop`. Neither knows about the other except through `VoiceInbox` and the vault. |
| §5 "Vault access: HTTPS + osxkeychain" | Still true on the Mac. The container authenticates with a **per-repo write deploy key** (added to `you/vault`), mounted read-only at `/ssh`; `approved_remote` is therefore the ssh URL there. FR-33's guard is unchanged and compares the exact string. |

Non-goals that still hold: no Watch Later or history (login cookies), no
playlist editing, no transcription on the NAS, no cookies anywhere.

## A1.2 What was added

- `src/ozu_capture/youtube.py` — listing, caption choice, fetch, the
  VTT→paragraph transform, the audio hand-off. Every yt-dlp call is an
  argument list through a configured `ytdlp_cmd`, so tests substitute a fake.
- `ledger.py` — a `videos` table keyed by **video id** (the identity, as sha
  is for audio) plus `meta` for the poll clock and the yt-dlp upgrade stamp.
- `note.py` — `write_youtube_note()` and `vault_has_video()`, the vault-side
  backstop that makes a lost ledger harmless.
- `run.py` — `youtube_tick`/`youtube_once`, and the voice tick now reads a
  `yt-<id>.json` sidecar so a handed-off video becomes a *learning* note
  rather than a voice note.
- `cli.py` — `youtube-once`, `youtube-loop`, and the status lines.
- `deploy/nas/` — Dockerfile, compose file, config template, README.
- `tests/` — `fake_ytdlp.py`, `test_youtube.py` (23 tests), the golden VTT.

## A1.3 What running it taught us (2026-09-02)

**The remote goes away, for minutes, without failing.** The first live ticks
wrote and pushed transcripts to the vault while logging `TimeoutExpired`
120 s later, as if nothing had worked — GitHub had the commits the whole
time. The first guess (git's `maintenance --auto --detach` inheriting the
captured pipe) was wrong: with auto-maintenance disabled the stalls
continued, and a trace put them squarely in the ssh transport. Timed from
inside the container: `git ls-remote` 1.4 s, then five consecutive attempts
45 s apart all timing out, then 1.4 s again — DNS, TCP and the local repo
healthy throughout. GitHub throttling a key that had just pushed six times,
or the NAS's own networking; the bridge cannot tell the difference and must
not try.

So: network gits get **300 s** (clone 900 s), and a stall raises
`SyncDeferred`/`PushDeferred` rather than escaping as a crash — the deferral
paths FR-30 and FR-32 already existed, they simply were not reached. Nothing
was ever at risk: a commit that cannot be pushed stays local and every
subsequent tick retries it, which is how all six of the first day's
transcripts arrived despite the logs. The `-c gc.auto=0
-c maintenance.auto=false` flags stayed in as cheap insurance against a real
failure mode we had ruled out rather than seen.

- The container's half-core cap is otherwise ample: a listing plus one
  caption fetch is ~7 s per video, and a three-video tick runs in ~40 s.
- Manual subtitles are picked over auto when a video has them (measured on a
  video carrying a real subtitle track).
