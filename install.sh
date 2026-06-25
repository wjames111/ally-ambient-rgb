#!/usr/bin/env bash
# Install flicker as a system service.  Needs sudo.
set -euo pipefail
cd "$(dirname "$0")"

PREFIX=/opt/flicker
UNIT=/etc/systemd/system/flicker.service

echo ">> Installing to $PREFIX (requires sudo)…"
sudo install -Dm755 flicker.py "$PREFIX/flicker.py"
sudo install -Dm644 flicker.service "$UNIT"

echo ">> Enabling + starting the service…"
sudo systemctl daemon-reload
sudo systemctl enable --now flicker.service

echo
echo ">> Done.  flicker is running and will start on every boot."
echo "   stop:    sudo systemctl stop flicker     (restores your normal RGB)"
echo "   start:   sudo systemctl start flicker"
echo "   status:  systemctl status flicker"
echo "   logs:    journalctl -u flicker -f"
