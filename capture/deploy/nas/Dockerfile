# ozu-capture youtube source, packaged for the NAS (Amendment 1 §5).
#
# Build context is the repo tree (deploy/nas/README.md ships it as a tarball
# of a committed HEAD, so what runs is exactly a commit). x86-64 only:
# deno's asset name says so.
#
# Runs as PUID/PGID (build args, set from the `.env` beside compose.yml) so
# everything it writes on the bind mounts — ledger, vault clone, hand-off
# audio in VoiceInbox — is owned by the NAS user that owns those shares.
# The user must exist in /etc/passwd or ssh refuses to start ("No user
# exists for uid"), which is why it is created here rather than only named
# in compose.yml.
FROM python:3.12-slim

ARG DENO_VERSION=v2.9.6
ARG PUID=1000
ARG PGID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git openssh-client ca-certificates curl unzip tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL "https://github.com/denoland/deno/releases/download/${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" \
        -o /tmp/deno.zip \
 && unzip -q /tmp/deno.zip -d /usr/local/bin \
 && rm /tmp/deno.zip \
 && chmod 755 /usr/local/bin/deno \
 && (getent group "$PGID" >/dev/null || groupadd -g "$PGID" ozu) \
 && useradd -u "$PUID" -g "$PGID" -M -d /data -s /usr/sbin/nologin ozu

# yt-dlp is pinned by the image build date; the loop upgrades it daily into
# the user site (/data/.local), which PATH puts first (FR-11).
RUN pip install --no-cache-dir --root-user-action=ignore yt-dlp

COPY . /app
RUN pip install --no-cache-dir --root-user-action=ignore /app

ENV HOME=/data \
    PATH=/data/.local/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONUNBUFFERED=1 \
    TZ=UTC \
    OZU_CAPTURE_DATA=/data \
    OZU_CAPTURE_CONFIG=/data/config.toml

WORKDIR /data
ENTRYPOINT ["ozu-capture"]
CMD ["youtube-loop"]
