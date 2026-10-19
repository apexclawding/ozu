# Transcribe — local audio → Markdown, on Apple Silicon

Ozu's on-device transcriber: [`capture/`](../capture) calls its CLI for every
voice memo. It stands on its own too — a drag-and-drop page and a CLI.

Drop an audio file on a local web page, get a clean Markdown transcript.
**100% on-device inference on Apple Silicon** — no audio, transcript, or
metadata ever leaves the machine. The only network access WHISPER ever makes
is a one-time, explicitly confirmed model download.

The full specification is [REQUIREMENTS.md](REQUIREMENTS.md).

## Requirements

- Apple Silicon Mac (arm64). The app refuses to start anywhere else.
- macOS with [Homebrew](https://brew.sh) and [uv](https://docs.astral.sh/uv/).
- `ffmpeg` — the one manual prerequisite:

```bash
brew install ffmpeg
```

## Quick start

```bash
./run.sh
```

That installs dependencies, verifies `ffmpeg`, starts the server on
`http://127.0.0.1:8420` (loopback only, never exposed to the network), and
opens the browser. On first run the page asks for consent to download the
speech model (`mlx-community/whisper-large-v3-turbo`, ~1.6 GB) from
Hugging Face — once cached, everything works with Wi-Fi off.

Drag any of `.m4a .mp3 .wav .aac .flac .ogg .opus .wma .aiff .mp4 .mov .webm
.mkv` onto the page. Files queue and process one at a time; each transcript is
downloadable from the page and also written to `data/transcripts/`.

### Mac app

```bash
macos/make-app.sh
```

Builds and installs `/Applications/WHISPER.app` — a double-clickable launcher
that starts the server if it isn't running (or just reopens the page if it is)
and opens the UI. Errors show as dialogs; the server log goes to
`data/whisper-app.log`. The repo path is baked in at build time, so rerun the
script if the repo moves or you edit `macos/icon.svg` or `macos/launcher.sh`.

## CLI

The web UI and the CLI are thin clients of the same pipeline:

```bash
uv run whisper-notes transcribe recording.m4a
```

Options: `--model M` (repo or local path) · `--language pt` (force a language
instead of auto-detect) · `--translate` (to English) · `--no-timestamps` ·
`--out PATH`.

Other commands: `prefetch-model` (do the one-time download ahead of time),
`serve [--port N]`.

## Output

One UTF-8 Markdown file per recording: YAML frontmatter with full provenance
(source hash, duration, detected language + confidence, model, timing,
realtime factor, word count), then readable timestamped paragraphs — never
one line per segment. Opens cleanly in Obsidian with frontmatter as
properties. A recording with no detectable speech produces
`_No speech detected._`, never hallucinated text.

## Configuration — `config.toml`

| Key | Default | Meaning |
|---|---|---|
| `[server] port` | `8420` | UI port. Host is always `127.0.0.1`, not configurable. |
| `[model] repo` | `…/whisper-large-v3-turbo` | Model (see table below). |
| `[model] idle_timeout_seconds` | `600` | Release model memory after idling this long. |
| `[output] timestamps` | `true` | `**[HH:MM:SS]**` paragraph prefixes. |
| `[output] summary_line` | `true` | The one-line blockquote under the title. |
| `[output] paragraph_pause_seconds` | `2.0` | Pause that starts a new paragraph. |
| `[output] paragraph_max_chars` | `750` | Length that forces a paragraph break. |
| `[limits] max_file_gb` / `max_duration_hours` | `2` / `6` | Upload rejection limits. |
| `[storage] data_dir` | `data` | Uploads, transcripts, job DB, log. |

### Models

| Model | Weights | Translate to English | When to use |
|---|---|---|---|
| `whisper-large-v3-turbo` | ~1.6 GB | ✗ | **Default.** Best accuracy/speed balance, ~99 languages. |
| `whisper-large-v3-turbo-q4` | ~0.45 GB | ✗ | If memory pressure bites (8 GB machines under load). |
| `whisper-large-v3-mlx` | ~3.1 GB | ✓ | Maximum accuracy; tight on 8 GB, close other apps. |
| `whisper-medium-mlx` | ~1.5 GB | ✓ | Legacy fallback — and the go-to when you need translation. |

All are `mlx-community/…` repos, switched with one line in `config.toml`.

**Translation caveat (measured):** the turbo models were distilled on
transcription data only and ignore Whisper's translate task, so WHISPER
refuses a translate request on them instead of silently returning the source
language. To use *Translate to English*, switch to a ✓ model above — the UI
walks you through the one-time download for the new model.

## Offline mode

With the model cached, set `WHISPER_OFFLINE=1` to make any network attempt a
hard failure instead of a silent fallback. The test suite verifies the full
pipeline succeeds with network access poisoned (`tests/test_offline.py`).

## Measured baseline (this machine)

Apple M1 · 8 GB unified memory · macOS 26.5.2 · `whisper-large-v3-turbo`
(fp16) · mlx-whisper 0.4.3 — measured with `/usr/bin/time -l`, cold model
load included:

| Recording | Duration | Wall time | Realtime factor | Peak RSS | Peak memory footprint |
|---|---|---|---|---|---|
| English `.m4a` | 4 m 30 s | 31.3 s | **8.7×** | 0.83 GB | 2.62 GB |
| English `.m4a` | 62 m 58 s | 7 m 50 s | **8.0×** | 0.63 GB | 5.06 GB |

The NFR-03 target (≥5× realtime) and the NFR-02 budget (peak RSS < 4.5 GB) are
both met with real margin. One honest caveat: *peak memory footprint* — the
unified-memory number that includes MLX's Metal buffers, which plain RSS does
not — reaches ~5 GB on hour-long recordings (transient; released when the job
ends or the model idles out). On an 8 GB machine, close heavyweight apps
before transcribing multi-hour files, or switch to `whisper-large-v3-turbo-q4`.
The engine already caps MLX's Metal cache at 1 GB (measured: −44% RSS for
~3% speed on the hour benchmark).

Timestamps stay monotonic across the full hour (last stamp 01:02:53 on a
62:58 recording — 97 paragraphs, no drift).

## Data layout

```
data/
├── uploads/       # files submitted through the UI (deletable from the page)
├── transcripts/   # every finished .md
├── jobs.db        # job history (SQLite)
└── whisper.log    # rotating structured log
```

Everything under `data/` stays on this machine and is gitignored.

## Troubleshooting

- **"ffmpeg was not found"** — `brew install ffmpeg`, then restart.
- **"model … is not in the local cache"** — `uv run whisper-notes prefetch-model`.
- **Out-of-memory / worker died** — close other apps or switch `config.toml`
  to `whisper-large-v3-turbo-q4`.
- **"cannot translate"** — the configured turbo model is transcription-only;
  set `[model] repo = "mlx-community/whisper-medium-mlx"` and retry.
- **A job shows *failed* after a crash or restart** — by design; completed
  transcripts survive, interrupted jobs are marked instead of hanging forever.

## Development

```bash
uv run python -m unittest discover -s tests
```

The suite covers the pipeline, paragraph merging, silence suppression, the
Markdown schema, the SQLite store, the queue (real worker process: FIFO,
kill-based cancellation, crash recovery), every API endpoint against a real
server, and the offline guarantee. Engine access goes through an ABC
(`core/engine.py`), so `whisper.cpp` (`pywhispercpp`) can replace MLX without
touching anything above it.
