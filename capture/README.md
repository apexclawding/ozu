# Capture: voice and Learn from YouTube

**Talk to your vault. Save a video to learn from it. Nothing you say leaves your machines.**

ozu-capture is Ozu's capture bridge: two lanes, two machines, one codebase.
It adds two front doors to the same Markdown vault that your handwriting feeds:

- **Voice.** Record a memo on your phone and drop it in a NAS folder. A Mac
  transcribes it *on-device* with Whisper and pushes a note into the vault.
  Ozu indexes it on its next cycle, and it is searchable over MCP like any
  handwritten page.
- **Learn from YouTube.** Save a video to a playlist. A small container on the NAS
  fetches its captions and writes a transcript note with `learn: requested`.
  Ozu writes a brief from it, and the brief lands on the evening
  edition's **Learn** sheet on your tablet, then comes back 2, 7 and 30 days
  later.

```
phone ──► NAS VoiceInbox (SMB) ──► Mac: ozu-capture (launchd, every 2 min)
                                      └► whisper-notes (on-device, Apple Silicon)
                                      └► topics/voice/<id>.md ──► git push ──► vault
playlist ──► NAS container: ozu-capture youtube-loop (every 30 min)
                                      └► captions ──► topics/learning/<id>.md ──► vault
vault ──► Ozu: index, MCP, briefs, the Learn sheet on paper
```

Zero Python dependencies (standard library only), 66 tests.

## What it guarantees

- **Audio and inference never leave your machines.** Transcription runs on
  the Mac ([`transcribe/`](../transcribe), MLX).
  Only transcript *text* is pushed, and only to the vault remote you
  approved: any other `origin` is a hard refusal, logged loudly.
- **At most one note per recording, ever.** Identity is the audio's SHA-256,
  not its filename. Re-drops, crashes at any point, even deleting the
  bridge's own ledger cannot duplicate a note: the vault itself is scanned
  for the hash before writing. Videos work the same way, keyed by video id.
- **Half-uploaded files are never picked up.** A file must hold still for
  60 s first.
- **Failures are visible.** A failed file stays in the share with a
  `<name>.failed.txt` beside it saying why; delete the marker to retry.
- **Silence produces nothing.** A pocket recording becomes a `no_speech`
  ledger entry, not a note.
- **A work wall before egress.** A filename starting with `work`
  (configurable) is transcribed locally only and never committed. The bridge
  cannot classify speech, so this is a backstop: work memos don't go in the
  folder.

The full contract, with requirement ids the code cites, is
[`REQUIREMENTS.md`](REQUIREMENTS.md).

## Requirements

- **Voice:** an Apple Silicon Mac with [uv](https://docs.astral.sh/uv/),
  `ffmpeg` and [`transcribe/`](../transcribe); a
  folder the phone can upload to (an SMB share on a NAS is what this was
  built on).
- **Learn from YouTube:** Docker on an always-on box on your *home* network: YouTube
  answers "confirm you're not a bot" to datacenter IPs, so a cloud server
  won't do.
- **Both:** an [Ozu](../README.md) vault in a git repo the
  bridge can push to.

## Lane 1: voice memos

1. **The share.** Create a `VoiceInbox` shared folder on the NAS, with
   read/write for the Mac's user and the phone's. On the Mac, connect once
   with Finder → Go → Connect to Server → `smb://user@nas.local/VoiceInbox`
   and tick **Remember this password in my keychain**. That keychain entry is
   what lets the bridge remount the share by itself after sleep.
2. **Configure.** Edit `config.toml`: the share URL, the transcriber's
   command and directory (`transcribe/` in this repo) (absolute paths, because launchd has no PATH), and
   `approved_remote`, your vault's URL.
3. **Run once by hand**, then install the agent:

   ```bash
   cd ~/ozu/capture && uv sync
   uv run ozu-capture run-once          # clones the vault, processes the share
   uv run ozu-capture install-agent     # LaunchAgent, every 120 s
   uv run ozu-capture status
   ```

4. **Full Disk Access, once.** A LaunchAgent does not inherit the terminal's
   file access, and network volumes are protected. Without the grant, every
   tick dies silently with `Operation not permitted`. System Settings →
   Privacy & Security → Full Disk Access → **+** → the Python binary uv
   installed (`~/.local/share/uv/python/cpython-3.12-…/bin/python3.12`).

The Mac must be awake and logged in; memos dropped while it sleeps are picked
up on wake.

### Phone capture

The bridge only watches the folder, so capture is configuration. Record in
**m4a** where you can (`.amr`/`.3gp` are refused with a marker).

- **iPhone:** Files → ⋯ → Connect to Server → `smb://nas.local` once. Then,
  per memo: Voice Memos → share → Save to Files → `VoiceInbox`.
- **Android:** FolderSync (free tier): an SMB2 account for the NAS, and a
  folder pair from the recorder's output folder to `/VoiceInbox`, upload
  only. Don't enable "delete after upload": the bridge tidies the share into
  `processed/` itself, and the phone's copy is your backup until then.

## Lane 2: learn from YouTube

The YouTube lane runs as a container next to the share. Full steps, including
the deploy key it pushes with, are in [`deploy/nas/README.md`](deploy/nas/README.md).

```bash
docker compose -f /path/to/ozu-capture/compose.yml up -d
docker exec ozu-capture ozu-capture status
```

Set `playlist_url` to a playlist you save videos to (unlisted is fine). A
video with captions becomes a transcript note within the poll interval. A
video with no captions is handed to the Mac as audio and transcribed there.
A video longer than `max_minutes` is skipped and reported once on the Learn
sheet.

## Operations

- `uv run ozu-capture status` shows the world: queued, done, failed, pushes
  waiting.
- Logs: `data/ozu-capture.log`. The LaunchAgent's own stdout/err go to
  `~/Library/Logs/ozu-capture/`, *outside* `~/Documents` on purpose: launchd
  opens them before the program starts, and a denied open fails every tick
  as `EX_CONFIG` with nothing logged anywhere. That bug cost four weeks.
- A push that cannot reach the remote stays committed locally and every
  tick retries it, so nothing is lost when the network is down.

## Tests

```bash
uv run python -m unittest discover -s . -p "test_*.py"
```

Hermetic: Whisper and yt-dlp are replaced by fakes (`tests/fake_whisper.py`,
`tests/fake_ytdlp.py`), and the caption fixture is synthetic
(`tests/fixtures/make_vtt.py`).

## License

MIT. See [`LICENSE`](LICENSE).
