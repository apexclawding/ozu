# The youtube source on the NAS

The Learning booster's capture half (`Ozu's Learn sheet (ozu `DESIGN.md`, invariant 11)`
§6.1): every 30 minutes the container lists the "Learn" playlist, turns
each new video's captions into a transcript note under `topics/learning/`
in `you/vault`, and pushes it. Videos without captions are
handed to the Mac's WHISPER lane by dropping the audio plus a `yt-<id>.json`
sidecar into `VoiceInbox` (the share is local disk on this box).

The VPS does the rest (`ozu run` → `learn.py` → the 20:00 sheet).

Pick a directory for it on the NAS (below, `DIR`, for example
`/srv/ozu-capture`) and the path of the `VoiceInbox` share:

```
$DIR/
├── .env             ← PUID, PGID (the NAS user that owns the shares) and
│                       VOICE_INBOX (the share's path on the NAS)
├── compose.yml      ← this directory's compose.yml, copied
├── src/             ← the repo tree at a committed HEAD (build context)
├── data/            ← /data in the container: config.toml, ledger.db,
│                       vault/ (its own clone), tmp/, ozu-capture.log, .local/
└── ssh/             ← id_ed25519 (deploy key, chmod 600), known_hosts; :ro
```

Runs as `PUID`/`PGID`, so what it writes on the shares belongs to their
owner, capped at half a core and 512 MB. It does no transcription — a small NAS is usually busy with other services.

## First run

Once, on the NAS (over `ssh nas.local`):

1. Create `$DIR/.env`:
   ```
   PUID=1000
   PGID=1000
   VOICE_INBOX=/path/to/VoiceInbox
   ```
   with the uid and gid of the user that owns the share (`id <user>`).
2. Create the keypair and pin GitHub's host key under `$DIR/ssh/` with
   the commands in [Re-keying](#re-keying) plus
   `ssh-keyscan github.com > $DIR/ssh/known_hosts`. Add the public half to
   `you/vault` as a **write** deploy key.
3. `data/config.toml` is `deploy/nas/config.nas.toml` copied verbatim.
   The only value that should ever change is `playlist_url`.

Then, from the Mac, ship a commit and build:

```bash
cd ~/ozu
DIR=/srv/ozu-capture        # the directory chosen above
ssh nas.local "rm -rf $DIR/src && mkdir -p $DIR/src"
git archive HEAD:capture | ssh nas.local "tar -x -C $DIR/src"
scp deploy/nas/compose.yml nas.local:$DIR/compose.yml
ssh nas.local "cd $DIR && docker compose build"
```

Start it:

```bash
ssh nas.local "cd $DIR && docker compose up -d"
```

## Operating it

```bash
# what the world looks like: playlist, last poll, yt-dlp, videos per state
ssh nas.local "cd $DIR && docker compose exec ozu-capture ozu-capture status"
# one tick right now, in the foreground (does not need the loop to be up)
ssh nas.local "cd $DIR && docker compose run --rm ozu-capture youtube-once"
# the log
ssh nas.local "tail -50 $DIR/data/ozu-capture.log"
ssh nas.local "cd $DIR && docker compose logs --tail 50"
```

- A video that failed to fetch three times is written as a `learn: skipped`
  stub so the sheet reports it once. To retry it after the cause is fixed,
  delete its ledger row; the next tick rewrites the stub in place:
  `sqlite3 data/ledger.db "DELETE FROM videos WHERE video_id='<id>'"`
  (or, without sqlite3 on the host,
  `docker compose run --rm --entrypoint python ozu-capture -c "import sqlite3; c=sqlite3.connect('/data/ledger.db'); c.execute(\"DELETE FROM videos WHERE video_id='<id>'\"); c.commit()"`).
  The same gesture re-reads a video that was skipped as too long after
  raising `max_minutes`.
- Pushes that fail (network, key revoked) leave the commit in `data/vault`
  with state `committed`; every tick retries; `status` shows them.
- yt-dlp upgrades itself once a day into `data/.local` (FR-11); the image's
  copy is the fallback if an upgrade breaks. Rebuilding the image
  (`docker compose build --no-cache`) refreshes the baseline.

## Upgrading the code

Repeat the ship-and-build block above with the new HEAD, then
`docker compose up -d` (compose recreates the container from the new image).
`data/` is untouched by a rebuild.

## Re-keying

```bash
ssh nas.local "cd $DIR/ssh && ssh-keygen -q -t ed25519 -N \"\" -C ozu-capture -f id_ed25519.new && mv id_ed25519.new id_ed25519 && mv id_ed25519.new.pub id_ed25519.pub && chmod 600 id_ed25519 && cat id_ed25519.pub"
```

Add the printed public key on `github.com/you/vault/settings/keys`
with **Allow write access**, delete the old one, restart the container.

## Validating against a throwaway origin

The build was proven this way before the first real push, and it is how to
test a code change without touching the vault: point a second config at a
bare repo inside `/data` and run one tick against it.

On the NAS, in `$DIR`:

```bash
docker compose run --rm --entrypoint sh ozu-capture -c "
  git init -q --bare -b main /data/test-origin.git &&
  git clone -q /data/test-origin.git /data/test-seed &&
  printf '# Knowledge Base — Index\n\n## Registry\n' > /data/test-seed/INDEX.md &&
  git -C /data/test-seed add -A && git -C /data/test-seed commit -q -m scaffold &&
  git -C /data/test-seed push -q origin main && rm -rf /data/test-seed &&
  sed -e 's#git@github.com:you/vault.git#/data/test-origin.git#' \
      -e 's#max_per_tick = 3#max_per_tick = 1#' /data/config.toml > /data/config.test.toml"
docker compose run --rm -e OZU_CAPTURE_CONFIG=/data/config.test.toml \
  -e OZU_CAPTURE_DATA=/data/test ozu-capture youtube-once
docker compose run --rm --entrypoint git ozu-capture -C /data/test-origin.git log --stat -1
```

Clean up with `rm -rf data/test-origin.git data/test data/config.test.toml`
so the real first tick starts from nothing.

## What is deliberately not here

- No cookies, ever. If YouTube starts refusing the home IP, the right
  answer is a slower poll, not a login (Amendment 1 R1).
- No ffmpeg: the hand-off keeps the native audio container (m4a or webm),
  both of which WHISPER accepts.
- No transcription. The sidecar is the whole hand-off protocol.
