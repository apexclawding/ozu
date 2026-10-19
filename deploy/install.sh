#!/usr/bin/env bash
# Install Ozu on a Linux box with systemd, as your normal user.
#
#   git clone https://github.com/<you>/ozu ~/ozu && ~/ozu/deploy/install.sh
#
# The systemd units expect the code at ~/ozu and the data at ~/ozu-data; edit
# the units if you put either somewhere else. A project-local venv keeps the
# system Python untouched.
set -euo pipefail
echo "== Ozu install =="

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
if [ "$REPO" != "$HOME/ozu" ]; then
  echo ">> note: the systemd units point at ~/ozu, but this clone is at $REPO"
fi

python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e .

mkdir -p ~/ozu-data
if [ ! -f ~/ozu-data/.env ]; then
  sed "s|/home/USER|$HOME|g" .env.example > ~/ozu-data/.env
  chmod 600 ~/ozu-data/.env
  echo ">> Created ~/ozu-data/.env — set ANTHROPIC_API_KEY and OZU_RCLONE_REMOTE before the first live run"
fi

.venv/bin/ozu init

mkdir -p ~/.config/systemd/user
cp deploy/systemd/*.service deploy/systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ozu-nightly.timer ozu-tick.timer \
  ozu-morning.timer ozu-evening.timer
# The MCP HTTP server is optional (stdio needs no service). Enable it once
# OZU_MCP_TOKEN is set:  systemctl --user enable --now ozu-mcp.service

# Without linger, the user manager (and therefore every timer) dies on logout.
loginctl enable-linger "$USER" || sudo loginctl enable-linger "$USER"

echo "== Done. Check: systemctl --user list-timers | grep ozu =="
