# Local Audio → Markdown Notes — Requirements

**Project codename:** WHISPER
**Status:** Draft v1.0 — awaiting approval to build
**Author:** Sergio dos Santos
**Date:** 2026-08-28
**Target machine:** Apple M1, 8 GB unified memory, macOS 26.5.2 (arm64)

---

## 1. Overview

A locally-installed macOS application that ingests a recorded audio file and produces a
clean, structured Markdown transcript. All speech recognition runs on-device. No audio,
transcript, or metadata ever leaves the machine.

The user drops an audio file onto a local web page, watches progress, and downloads a
`.md` file. A copy is also written to disk automatically.

### 1.1 Problem statement

Recorded meetings, interviews, voice memos and lectures are unsearchable. Cloud
transcription services solve this but require uploading potentially confidential audio to
a third party. This tool provides the same convenience with zero data egress.

---

## 2. Goals and non-goals

### 2.1 Goals

| # | Goal |
|---|------|
| G1 | Transcribe a local audio file to Markdown with 100% on-device inference |
| G2 | Support ~99 languages with automatic language detection |
| G3 | Provide a drag-and-drop local web UI with live progress and a download button |
| G4 | Produce Markdown that is immediately usable as notes — frontmatter, headings, timestamps, readable paragraphs |
| G5 | Run comfortably within 8 GB of unified memory alongside a browser |
| G6 | Operate fully offline after a one-time model download |

### 2.2 Non-goals (v1)

| # | Non-goal | Rationale |
|---|----------|-----------|
| N1 | AI summarization / action-item extraction | Explicitly deferred. Requires a second local LLM; adds memory pressure and non-determinism. See §13.1. |
| N2 | Speaker diarization ("who said what") | Explicitly deferred. Requires PyTorch + pyannote (~2 GB deps, HF token, large slowdown). See §13.2. |
| N3 | Live / real-time microphone transcription | v1 ingests completed files only. |
| N4 | Cloud sync, accounts, multi-user, or any network service | Directly contradicts G1. |
| N5 | Editing the transcript in the UI | Output is a `.md` file; edit it in any editor. |
| N6 | Native `.app` bundle / code signing / notarization | v1 launches via a shell script. |

---

## 3. Users and primary flows

**Primary user:** the machine's owner, working locally, technically competent.

### 3.1 Happy path

1. User runs `./run.sh` (or the app is already running).
2. Browser opens at `http://127.0.0.1:8420`.
3. User drags `board-meeting.m4a` onto the drop zone.
4. UI shows: file accepted → decoding audio → detecting language → transcribing (live % + elapsed/ETA).
5. On completion the UI shows a preview of the first ~500 characters and a **Download .md** button.
6. User clicks Download; browser saves `board-meeting.md`.
7. A copy already exists at `./data/transcripts/board-meeting.md`.

### 3.2 Secondary flows

- **Queue:** user drops a second file while one is processing → it queues, UI shows position.
- **History:** the page lists previously completed jobs from this and prior sessions, each re-downloadable.
- **CLI:** `uv run whisper-notes transcribe board-meeting.m4a` performs the same pipeline headlessly. The web layer is a client of the core, not the other way around.

---

## 4. Constraints

| # | Constraint | Impact |
|---|-----------|--------|
| C1 | 8 GB unified memory, shared with OS and browser | Hard cap of **one concurrent transcription**. Model choice must leave ≥3 GB headroom. |
| C2 | Apple M1 (arm64), no CUDA | Rules out CUDA-only engines. Favors Metal / MLX / Core ML. |
| C3 | No network at inference time | Model weights must be resolved from local cache; a documented offline flag must be enforceable. |
| C4 | `ffmpeg` is not currently installed | Hard prerequisite; must be installed and verified at startup (see FR-02). |
| C5 | Python 3.10.14 via pyenv is the system default | The project pins its own interpreter via `uv` to avoid contaminating or depending on the pyenv environment. |
| C6 | Prefer stdlib and native paths over new dependencies | Frontend is vanilla HTML/CSS/JS with no build step, bundler, or framework. |

---

## 5. Technology decisions

### 5.1 Inference engine

**Decision: `mlx-whisper` (Apple MLX)** — primary engine, abstracted behind an interface so it can be swapped.

| Candidate | Verdict | Rationale |
|-----------|---------|-----------|
| **mlx-whisper 0.4.3** | **Chosen** | Apple's own MLX framework, built for Apple Silicon unified memory. `pip`-installable, pure Python integration (backend is already Python), Metal-accelerated, actively maintained by Apple. Model weights load lazily and release cleanly between jobs. |
| whisper.cpp / pywhispercpp 1.5.1 | Documented fallback | Excellent and extremely mature, self-contained C++ with Metal. Chosen as the fallback if MLX shows instability or memory issues under C1. Requires managing GGUF files and a compiled binary. |
| faster-whisper 1.2.1 (CTranslate2) | Rejected | No Metal backend — CPU-only on Apple Silicon. Materially slower on M1 than either alternative. |
| openai-whisper (reference PyTorch) | Rejected | PyTorch adds ~2 GB of dependencies and MPS support for Whisper is slower and less stable than MLX. |
| parakeet-mlx 0.5.2 (NVIDIA Parakeet) | Rejected for v1 | Genuinely faster than Whisper and excellent quality, but covers ~25 European languages rather than ~99. Conflicts with G2. Worth revisiting if the language set proves sufficient. |
| WhisperKit (Swift/CoreML) | Rejected for v1 | The right choice for a native `.app` (N6), not for a Python-backed local service. |

### 5.2 Model

**Default: `mlx-community/whisper-large-v3-turbo`** (~1.6 GB fp16 weights).

`large-v3-turbo` is a distilled 4-decoder-layer variant of `large-v3`. It retains
near-large-v3 accuracy on transcription while being several times faster, and it keeps
full multilingual coverage — which `distil-large-v3` (English-only) does not.

Configurable alternatives, exposed in config and documented in the README:

| Model | Weights | When to use |
|-------|---------|-------------|
| `whisper-large-v3-turbo` | ~1.6 GB | **Default.** Best accuracy/speed balance. |
| `whisper-large-v3-turbo-q4` | ~0.45 GB | If memory pressure under C1 proves problematic, or for battery/thermal-constrained runs. Small accuracy cost. |
| `whisper-large-v3-mlx` | ~3.1 GB | Maximum accuracy for difficult audio. Will be tight on 8 GB; single-job only, close other apps. |
| `whisper-medium-mlx` | ~1.5 GB | Legacy fallback. Only if turbo underperforms on a specific language. |

Model resolution order: local cache → download from Hugging Face (one time, with explicit
UI consent and a progress bar) → hard failure with actionable message if `WHISPER_OFFLINE=1`.

> **Amendment 2026-08-28 (measured in-build):** the `turbo` distillations were trained on
> transcription only and silently ignore `task=translate` — verified on this machine
> (Spanish audio + translate → Spanish output; same pipeline with `whisper-tiny` → English).
> Translate-to-English (FR-13) therefore requires a non-turbo multilingual model
> (`whisper-medium-mlx`, `whisper-large-v3-mlx`). With a turbo model configured, the system
> refuses translate requests with that remedy rather than silently emitting the source
> language (see §10).

### 5.3 Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Package/env manager | `uv` (0.11.2, already installed) | Fast, reproducible, manages its own Python. Pins **Python 3.12**. |
| Audio decode | `ffmpeg` via subprocess | Universal format support. Single system dependency, installed via Homebrew. |
| Backend | FastAPI 0.141+ / Uvicorn 0.52+ | Async, native SSE support, minimal. |
| Frontend | Vanilla HTML + CSS + JS, single file | Per C6. No build step, no npm, no framework. |
| Job state | SQLite via stdlib `sqlite3` | Per C6 — no ORM. Survives restarts, gives §3.2 history for free. |
| Config | TOML via stdlib `tomllib` | Read-only config file; no dependency. |

---

## 6. Functional requirements

### 6.1 Ingestion

| ID | Requirement |
|----|-------------|
| **FR-01** | The system SHALL accept audio and video containers decodable by `ffmpeg`, explicitly: `.m4a .mp3 .wav .aac .flac .ogg .opus .wma .aiff .mp4 .mov .webm .mkv`. Audio is extracted from video containers. |
| **FR-02** | On startup the system SHALL verify `ffmpeg` is present and executable. If absent it SHALL refuse to start and print the exact remediation command (`brew install ffmpeg`). |
| **FR-03** | The system SHALL reject files larger than **2 GB** or longer than **6 hours** with a clear message. These limits SHALL be configurable. |
| **FR-04** | The system SHALL normalize all input to 16 kHz mono PCM float32 before inference. |
| **FR-05** | The system SHALL compute and record a SHA-256 of the source file for provenance and duplicate detection. |
| **FR-06** | If a file with an identical SHA-256 has already been transcribed with the same model and settings, the system SHALL offer the cached result instead of re-transcribing. |
| **FR-07** | Uploaded audio SHALL be stored under `./data/uploads/` and SHALL be deletable from the UI. |

### 6.2 Transcription

| ID | Requirement |
|----|-------------|
| **FR-10** | The system SHALL transcribe using the configured local model with **zero network calls** during inference. |
| **FR-11** | The system SHALL auto-detect the spoken language and record it, with its confidence score, in the output frontmatter. |
| **FR-12** | The user SHALL be able to override auto-detection by forcing a language code. |
| **FR-13** | The system SHALL support an optional **translate-to-English** mode, off by default, recorded in frontmatter when used. |
| **FR-14** | The system SHALL produce word-or-segment-level timestamps and retain them internally at segment granularity. |
| **FR-15** | The system SHALL apply silence/no-speech suppression to mitigate Whisper's known hallucination-on-silence failure mode (see R3). Segments below the no-speech threshold SHALL be dropped, not emitted as text. |
| **FR-16** | Exactly **one** transcription SHALL run at a time. Additional submissions SHALL queue in FIFO order, and each SHALL report its queue position. |
| **FR-17** | Model weights SHALL be loaded once and kept warm across jobs while the process lives, and SHALL be released if idle beyond a configurable timeout (default 10 minutes) to return memory to the system. |
| **FR-18** | A running job SHALL be cancellable from the UI, freeing its resources within 5 seconds. |

### 6.3 Markdown generation

| ID | Requirement |
|----|-------------|
| **FR-20** | Output SHALL be a single UTF-8 `.md` file conforming to the schema in §7. |
| **FR-21** | Output SHALL begin with YAML frontmatter carrying full provenance (source file, hash, duration, language, model, engine version, timestamp, processing time, word count). |
| **FR-22** | Raw Whisper segments SHALL be merged into **readable paragraphs**, not emitted one-line-per-segment. Merge rules: break on a pause exceeding `paragraph_pause_seconds` (default 2.0s), or when a paragraph exceeds `paragraph_max_chars` (default 750), preferring a sentence boundary. |
| **FR-23** | Each paragraph SHALL be prefixed with its start timestamp as `**[HH:MM:SS]**`. Timestamps SHALL be toggleable off in config. |
| **FR-24** | The output filename SHALL default to the source basename with a `.md` extension, sanitized for the filesystem, with a numeric suffix on collision. |
| **FR-25** | Generated Markdown SHALL be valid CommonMark and SHALL render correctly in Obsidian and GitHub. Any `---` occurring in transcript text SHALL be escaped so it is not parsed as a frontmatter delimiter. |
| **FR-26** | A copy SHALL be written to `./data/transcripts/` automatically on completion, independent of whether the user clicks Download. |

### 6.4 Web interface

| ID | Requirement |
|----|-------------|
| **FR-30** | The server SHALL bind to `127.0.0.1` **only** — never `0.0.0.0`. Port default 8420, configurable. |
| **FR-31** | The UI SHALL accept files by drag-and-drop and by a file picker. |
| **FR-32** | The UI SHALL display live progress with distinct phases: `queued → decoding → detecting language → transcribing (n%) → writing → done`. |
| **FR-33** | Progress SHALL be delivered via Server-Sent Events, with polling as an automatic fallback. |
| **FR-34** | The UI SHALL show elapsed time and an ETA derived from observed throughput on the current file. |
| **FR-35** | On completion the UI SHALL show a preview of the transcript and a **Download .md** button serving `text/markdown` with `Content-Disposition: attachment`. |
| **FR-36** | The UI SHALL list prior jobs with date, filename, duration, language and a re-download link, persisted across restarts. |
| **FR-37** | The UI SHALL be legible in both light and dark mode, and usable at 1280×800. |
| **FR-38** | Errors SHALL surface in the UI as plain-language messages with a remediation hint — never a raw stack trace. |

### 6.5 CLI

| ID | Requirement |
|----|-------------|
| **FR-40** | A CLI SHALL expose the same core pipeline: `transcribe <file> [--model M] [--language L] [--translate] [--no-timestamps] [--out PATH]`. |
| **FR-41** | The CLI SHALL exit non-zero on failure and print errors to stderr. |
| **FR-42** | The CLI and web UI SHALL share one implementation of the pipeline. No logic duplication. |

---

## 7. Markdown output specification

```markdown
---
title: "Board Meeting 2026-08-28"
source_file: "board-meeting.m4a"
source_sha256: "3f9a1c...e77b"
duration: "01:04:22"
duration_seconds: 3862
language: "en"
language_confidence: 0.98
translated: false
model: "mlx-community/whisper-large-v3-turbo"
engine: "mlx-whisper 0.4.3"
transcribed_at: "2026-08-28T17:42:11+01:00"
processing_seconds: 287
realtime_factor: 13.5
word_count: 9412
tags: [transcript]
---

# Board Meeting 2026-08-28

> Transcribed locally with Whisper large-v3-turbo · 1h 04m · English · 2026-08-28

## Transcript

**[00:00:00]** Good morning everyone, thanks for joining. Let's get started with the
first item on the agenda, which is the quarterly review.

**[00:01:47]** Before we do that, I want to flag one thing about the timeline...
```

**Notes on the schema**

- Frontmatter keys are stable and machine-parseable; downstream tooling (Obsidian Dataview, scripts) can rely on them.
- `realtime_factor` = `duration_seconds / processing_seconds`. Useful for benchmarking §9.
- The blockquote line is a human-readable summary of the frontmatter. Toggleable.
- `## Transcript` is a fixed heading so a future summarization phase (§13.1) can insert `## Summary` and `## Action Items` above it without ambiguity.

---

## 8. Architecture

```
WHISPER/
├── REQUIREMENTS.md          ← this document
├── README.md                 ← install + usage
├── pyproject.toml            ← uv project, pinned deps, Python 3.12
├── config.toml               ← user-editable settings
├── run.sh                    ← start server + open browser
├── src/whisper_notes/
│   ├── core/
│   │   ├── audio.py          ← ffmpeg probe + decode to 16kHz mono
│   │   ├── engine.py         ← ABC: TranscriptionEngine
│   │   ├── engine_mlx.py     ← mlx-whisper implementation
│   │   ├── segments.py       ← segment → paragraph merging (FR-22)
│   │   ├── markdown.py       ← frontmatter + body writer (§7)
│   │   └── pipeline.py       ← orchestration, the single source of truth
│   ├── store/
│   │   ├── db.py             ← sqlite3 schema + queries
│   │   └── files.py          ← uploads/transcripts paths, sanitization
│   ├── web/
│   │   ├── app.py            ← FastAPI routes
│   │   ├── queue.py          ← single-slot worker + FIFO
│   │   └── static/index.html ← the entire frontend, one file
│   └── cli.py                ← argparse entry point
├── tests/
└── data/                     ← gitignored
    ├── uploads/
    ├── transcripts/
    └── jobs.db
```

**Key design principle:** `core/pipeline.py` knows nothing about HTTP. The web layer and
the CLI are both thin clients of it (FR-42). The engine sits behind an ABC so
`pywhispercpp` can be dropped in without touching anything above it (§5.1).

### 8.1 API contract

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Serve the UI |
| `GET` | `/api/health` | `{ok, engine, model, model_loaded, ffmpeg_ok, version}` |
| `POST` | `/api/jobs` | Multipart upload → `{job_id, queue_position}` |
| `GET` | `/api/jobs` | List jobs (history) |
| `GET` | `/api/jobs/{id}` | `{status, phase, percent, elapsed, eta, error}` |
| `GET` | `/api/jobs/{id}/events` | SSE progress stream |
| `GET` | `/api/jobs/{id}/markdown` | Download `.md` as attachment |
| `GET` | `/api/jobs/{id}/preview` | First N chars, for the UI |
| `DELETE` | `/api/jobs/{id}` | Cancel if running; delete artifacts |
| `GET` | `/api/config` | Current effective settings |

---

## 9. Non-functional requirements

| ID | Requirement |
|----|-------------|
| **NFR-01** | **Privacy:** zero network egress during transcription. Verified by a test that runs the pipeline with outbound network blocked and asserts success. |
| **NFR-02** | **Memory:** peak RSS SHALL stay under **4.5 GB** with the default model, leaving ≥3 GB for the OS and browser under C1. Measured and recorded during benchmarking. |
| **NFR-03** | **Throughput target:** ≥ 5× realtime on M1 with `large-v3-turbo` — i.e. a 60-minute recording completes in ≤ 12 minutes. This is a target to be **measured and recorded**, not an assumed figure; the benchmark result becomes the documented baseline. |
| **NFR-04** | **Startup:** the server SHALL be serving the UI in under 3 seconds. Model loading is lazy and SHALL NOT block startup. |
| **NFR-05** | **Reliability:** a crash or restart SHALL NOT lose completed transcripts. Interrupted jobs SHALL be marked `failed` on restart, not left `running` forever. |
| **NFR-06** | **Offline:** with `WHISPER_OFFLINE=1` and a warm model cache, the app SHALL be fully functional with networking disabled. |
| **NFR-07** | **Security:** bound to loopback only (FR-30); uploaded filenames sanitized against path traversal; no `eval`/`exec` on user input; `ffmpeg` invoked via `subprocess` with an argument list, never a shell string. |
| **NFR-08** | **Portability:** documented as Apple Silicon–only for v1. On non-arm64 the app SHALL fail at startup with a clear message rather than misbehaving. |
| **NFR-09** | **Observability:** structured log to `./data/whisper.log` with rotation. Every job logs start, phase transitions, duration, peak memory, and outcome. |

---

## 10. Error handling

| Condition | Behavior |
|-----------|----------|
| `ffmpeg` missing | Refuse startup. Print `brew install ffmpeg`. (FR-02) |
| Unsupported / corrupt file | Reject at upload with the `ffprobe` reason, in plain language. |
| File exceeds size/duration limit | Reject with the actual vs allowed value. (FR-03) |
| Audio contains no detectable speech | Complete successfully; emit a `.md` with frontmatter and an explicit `_No speech detected._` body. Do not emit hallucinated text. |
| Model not cached and offline | Fail with the exact model id and the command to fetch it. |
| Translate requested on a transcription-only (turbo) model | Reject at submission — API 422 / CLI error / checkbox disabled — naming a translation-capable model (§5.2 amendment). Never silently emit the source language. |
| Model download interrupted | Resume on retry; never leave a partial file that looks valid. |
| Out of memory during inference | Catch, fail the job cleanly, and suggest switching to `whisper-large-v3-turbo-q4` in the error message. |
| Job cancelled by user | Release resources within 5s, mark `cancelled`, delete partial output. |
| Disk full while writing | Fail the job, leave no partial `.md`. |
| Unhandled exception | Job marked `failed`, full traceback to the log, plain-language message to the UI. (FR-38) |

---

## 11. Acceptance criteria

The build is complete when all of the following pass:

1. `./run.sh` on a clean checkout installs dependencies, verifies `ffmpeg`, starts the server, and opens the browser — with no manual steps beyond `brew install ffmpeg`.
2. Dragging a 5-minute English `.m4a` produces a downloadable `.md` matching §7 exactly, with correct frontmatter values.
3. A Portuguese and a Spanish recording are each auto-detected correctly and transcribed, with `language` set accordingly.
4. A 60-minute recording completes without OOM, and its measured `realtime_factor` and peak RSS are recorded in `README.md` as the machine baseline.
5. Timestamps are monotonic and paragraph breaks are readable — no one-line-per-segment output.
6. A 30-second file of pure silence produces `_No speech detected._` and zero hallucinated words.
7. Submitting three files queues them, processes them one at a time, and reports queue positions correctly.
8. Cancelling a running job frees memory within 5 seconds and leaves no partial artifacts.
9. Killing the server mid-job and restarting shows that job as `failed`, and all previously completed transcripts remain downloadable.
10. With Wi-Fi off and a warm cache, the entire flow works end to end. (NFR-01, NFR-06)
11. `uv run whisper-notes transcribe <file>` produces a byte-identical `.md` to the web UI for the same input and settings.
12. Output opens cleanly in Obsidian with frontmatter parsed as properties.

---

## 12. Build phases

| Phase | Deliverable | Exit criterion |
|-------|-------------|----------------|
| **P0 — Foundation** | `uv` project, pinned deps, Python 3.12, `ffmpeg` preflight, config loader, repo skeleton | `uv run python -c "import mlx_whisper"` succeeds; preflight correctly detects missing `ffmpeg` |
| **P1 — Core pipeline** | `audio.py`, `engine.py` + `engine_mlx.py`, `segments.py`, `markdown.py`, `pipeline.py`, CLI | CLI transcribes a real file to spec-compliant Markdown (AC 2, 5, 6, 11) |
| **P2 — Persistence & queue** | SQLite store, single-slot FIFO worker, cancellation, model lifecycle | AC 7, 8, 9 |
| **P3 — Web service** | FastAPI routes, SSE progress, upload handling, download endpoint | All §8.1 endpoints functional and tested |
| **P4 — UI** | Single-file drag-drop page, progress, preview, download, history, light/dark | AC 1, 3 |
| **P5 — Hardening & benchmark** | Error matrix, offline test, logging, long-file benchmark, README with measured baseline | All 12 acceptance criteria pass |

---

## 13. Deferred work (explicitly out of scope for v1)

### 13.1 Summarization / notes extraction
Add a local LLM step via Ollama (e.g. Qwen3 4B or Llama 3.1 8B, quantized) that consumes
the finished transcript and inserts `## Summary` and `## Action Items` above `## Transcript`.
Must run **sequentially** after the Whisper model is released (FR-17) — 8 GB cannot hold
both. The §7 schema already reserves the structural slot for this.

### 13.2 Speaker diarization
Add `sherpa-onnx` (lighter) or `pyannote.audio` (more accurate, heavier) to label speakers.
Would extend the paragraph model with a `speaker` field and change output to
`**Speaker 1** **[00:00:00]** ...`.

### 13.3 Other candidates
Live microphone capture · batch folder watching · a native `.app` bundle via WhisperKit ·
Obsidian vault auto-filing · Parakeet as an alternative engine for European-language speed
(§5.1) · custom vocabulary / initial-prompt biasing for names and jargon.

---

## 14. Risks

| ID | Risk | Severity | Mitigation |
|----|------|----------|------------|
| **R1** | 8 GB is tight; a large model plus a browser could trigger swap or OOM | High | Default to `turbo` not `large-v3`; enforce single-job concurrency (FR-16); release model when idle (FR-17); measure peak RSS in P5; ship `q4` as a one-line config change |
| **R2** | First run needs a ~1.6 GB model download — contradicts the "fully local" promise at t=0 | Medium | Make the one-time download explicit and consented in the UI with a progress bar; document it prominently; provide a `prefetch-model` command to do it ahead of time |
| **R3** | Whisper hallucinates text during silence and music — a well-known failure mode | Medium | Enforce `no_speech_threshold` and `logprob_threshold` (FR-15); AC 6 tests it directly |
| **R4** | MLX is younger than whisper.cpp; an upstream regression could break the build | Medium | Pin exact versions; keep the engine behind an ABC (§8) so `pywhispercpp` is a swap, not a rewrite |
| **R5** | Timestamp drift accumulates on multi-hour files | Low | Test with a 60-minute file (AC 4); verify monotonicity (AC 5) |
| **R6** | `ffmpeg` via Homebrew is an external dependency outside the app's control | Low | Preflight check with exact remediation (FR-02); pin nothing, tolerate any recent version |
| **R7** | Scope creep toward summarization/diarization mid-build | Medium | §2.2 and §13 are binding. Any addition requires an explicit decision to amend this document. |

---

## 15. Open questions

None blocking. The following are build-time judgment calls to be made with defaults noted:

- Port number — default 8420, configurable.
- Whether the history list should paginate — defer until >100 jobs is realistic.
- Retention policy for `./data/uploads/` — default keep, with manual delete (FR-07). Auto-purge could be added if disk pressure becomes real.

---

## 16. Sign-off

Approval of this document authorizes building phases P0–P5 as specified. Changes to
§2.2 (non-goals) or §6 (functional requirements) require an amendment to this document
before implementation.
