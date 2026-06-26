#!/usr/bin/env bash
# OPTIONAL one-time setup for the Flicker Decky plugin.
#
# Flicker's default UNIFIED mode is zero-setup — it needs none of this.  Run this
# ONLY to unlock the per-zone modes (Split / Quad):
#
#     sudo ./decky-setup.sh
#
# Why it's needed for per-zone: Split/Quad write the Aura RGB MCU directly over
# hidraw and capture via kmsgrab — but Handheld Daemon locks that hidraw to root,
# and kmsgrab needs CAP_SYS_ADMIN.  So the per-zone engine runs as a root systemd
# unit, and this installs a polkit rule letting the (non-root) plugin manage just
# that one unit without a password prompt.  It then drops a marker the plugin reads
# to enable the Mode dropdown.  (Unified needs none of this and is unaffected.)
set -euo pipefail

USER_NAME="${SUDO_USER:-$USER}"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"

# 1. polkit rule: let the user plugin manage the per-zone unit.
RULE=/etc/polkit-1/rules.d/10-flicker.rules
install -Dm644 /dev/stdin "$RULE" <<'EOF'
// Flicker: allow the (non-root) Decky plugin to manage its per-zone capture unit.
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

# 2. marker the plugin checks to unlock Split/Quad in the UI.  Decky's settings dir
#    is named for the plugin; write to the likely candidates and own them by the user.
for d in "$USER_HOME/homebrew/settings/flicker" "$USER_HOME/homebrew/settings/Flicker"; do
    mkdir -p "$d" 2>/dev/null || true
    if [ -d "$d" ]; then
        touch "$d/.perzone_unlocked"
        chown -R "$USER_NAME":"$USER_NAME" "$d" 2>/dev/null || true
    fi
done

echo "Installed $RULE + per-zone unlock marker."
echo "Reopen the Flicker panel in Decky — the Mode dropdown now offers Split / Quad."
