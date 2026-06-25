#!/usr/bin/env bash
# Install ally-ambient-rgb as a system service.  Needs sudo.
set -euo pipefail
cd "$(dirname "$0")"

PREFIX=/opt/ally-ambient-rgb
UNIT=/etc/systemd/system/ambient-rgb.service

echo ">> Installing to $PREFIX (requires sudo)…"
sudo install -Dm755 ambient_rgb.py "$PREFIX/ambient_rgb.py"
sudo install -Dm644 ambient-rgb.service "$UNIT"

echo ">> Enabling + starting the service…"
sudo systemctl daemon-reload
sudo systemctl enable --now ambient-rgb.service

echo
echo ">> Done.  ally-ambient-rgb is running and will start on every boot."
echo "   stop:    sudo systemctl stop ambient-rgb     (restores your normal RGB)"
echo "   start:   sudo systemctl start ambient-rgb"
echo "   status:  systemctl status ambient-rgb"
echo "   logs:    journalctl -u ambient-rgb -f"
