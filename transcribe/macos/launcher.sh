#!/bin/bash
# WHISPER.app — double-click launcher for the local voice→Markdown server.
# Starts the server if it isn't running, then opens the UI in the browser.
# @APP_DIR@ is replaced with the repo path by make-app.sh at build time.
set -u

APP_DIR="@APP_DIR@"
# Finder launches apps with a bare PATH; add uv's and Homebrew's locations.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

fail() {
    /usr/bin/osascript -e "display dialog \"$1\" with title \"WHISPER\" buttons {\"OK\"} default button 1 with icon caution" >/dev/null 2>&1
    exit 1
}

[ -d "$APP_DIR" ] || fail "Project folder not found at $APP_DIR — rerun macos/make-app.sh if the repo moved."
cd "$APP_DIR"

PORT=$(sed -n 's/^port *= *\([0-9][0-9]*\).*/\1/p' config.toml | head -1)
PORT=${PORT:-8420}
URL="http://127.0.0.1:${PORT}"

# Already running? Just open the page.
if curl -s -o /dev/null --max-time 2 "$URL"; then
    open "$URL"
    exit 0
fi

command -v uv >/dev/null 2>&1 || fail "uv was not found. Install it from docs.astral.sh/uv, then relaunch."
command -v ffmpeg >/dev/null 2>&1 || fail "ffmpeg was not found. Run in Terminal:  brew install ffmpeg"

mkdir -p data
uv sync --frozen >> data/whisper-app.log 2>&1 || fail "Dependency install failed — see data/whisper-app.log"

nohup uv run whisper-notes serve >> data/whisper-app.log 2>&1 &
disown

# Wait up to 60 s for the server to bind, then open the UI.
for _ in $(seq 1 120); do
    sleep 0.5
    if curl -s -o /dev/null --max-time 1 "$URL"; then
        open "$URL"
        exit 0
    fi
done
fail "The server did not start — see data/whisper-app.log"
