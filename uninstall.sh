#!/usr/bin/env bash
# Remove ally-ambient-rgb.  Needs sudo.
set -euo pipefail

echo ">> Stopping + disabling the service…"
sudo systemctl disable --now ambient-rgb.service 2>/dev/null || true

echo ">> Removing files…"
sudo rm -f /etc/systemd/system/ambient-rgb.service
sudo rm -rf /opt/ally-ambient-rgb
sudo systemctl daemon-reload

echo ">> Done.  (Your normal Handheld-Daemon RGB takes over again.)"
