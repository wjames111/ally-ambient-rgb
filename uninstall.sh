#!/usr/bin/env bash
# Remove flicker.  Needs sudo.
set -euo pipefail

echo ">> Stopping + disabling the service…"
sudo systemctl disable --now flicker.service 2>/dev/null || true

echo ">> Removing files…"
sudo rm -f /etc/systemd/system/flicker.service
sudo rm -rf /opt/flicker
sudo systemctl daemon-reload

echo ">> Done.  (Your normal Handheld-Daemon RGB takes over again.)"
