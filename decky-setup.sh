#!/usr/bin/env bash
# One-time setup for the Flicker DECKY PLUGIN.  Run once with sudo:  sudo ./decky-setup.sh
#
# Decky runs plugins as root but with ZERO Linux capabilities, and screen capture
# (kmsgrab) needs CAP_SYS_ADMIN.  So the plugin launches the capture engine as a
# transient systemd unit (which gets full caps).  This installs a polkit rule that
# lets the plugin start/stop just that one unit without an interactive password
# prompt.  (The standalone install.sh path doesn't need this — its service runs
# with full caps directly.)
set -euo pipefail

RULE=/etc/polkit-1/rules.d/10-flicker.rules
install -Dm644 /dev/stdin "$RULE" <<'EOF'
// Flicker: allow the (capability-stripped) Decky plugin to manage its capture unit.
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units") {
        var unit = action.lookup("unit") || action.lookup("name") || "";
        if (unit == "flicker-engine.service") {
            return polkit.Result.YES;
        }
    }
});
EOF

systemctl restart polkit 2>/dev/null || true
echo "Installed $RULE"
echo "Toggle Flicker in Decky (Game Mode) — it should drive the rings now."
