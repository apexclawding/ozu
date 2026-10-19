#!/bin/sh
# Start whisper-notes and open the browser (AC 1). The only manual
# prerequisites are Homebrew's ffmpeg and uv itself.
set -e
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required. Install it from https://docs.astral.sh/uv/" >&2
    exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg is missing. Install it with:" >&2
    echo "" >&2
    echo "    brew install ffmpeg" >&2
    exit 1
fi

uv sync --frozen

PORT=$(uv run python -c "from whisper_notes.config import load_config; print(load_config().port)")

(
    # give the server a moment to bind, then open the UI
    sleep 2
    open "http://127.0.0.1:${PORT}"
) &

exec uv run whisper-notes serve
