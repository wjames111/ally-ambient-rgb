import os
import json
import time
import subprocess

import decky

ENGINE = os.path.join(decky.DECKY_PLUGIN_DIR, "flicker.py")
SETTINGS_PATH = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
PY = "/usr/bin/python3"

# The engine runs as a transient systemd unit, NOT as a child of this backend.
# Decky sandboxes plugins with zero capabilities (CapEff=0) even as root, but
# kmsgrab needs CAP_SYS_ADMIN — so a direct subprocess can't capture. systemd-run
# gives the engine full root caps and a clean library environment (which also
# sidesteps Decky's leaked PyInstaller LD_LIBRARY_PATH that breaks ffmpeg's libssl).
UNIT = "flicker-engine"

# polkit rule letting this (capability-stripped) plugin manage just the capture
# unit. Installed once by decky-setup.sh — the plugin itself CAN'T write it (Decky's
# sandbox denies /etc/polkit-1 with EACCES; its "root" isn't real-root for system
# files), so _ensure_polkit below is only a best-effort attempt + a fallback hint.
POLKIT_RULE = "/etc/polkit-1/rules.d/10-flicker.rules"
POLKIT_CONTENT = (
    "// Flicker: allow the capability-stripped Decky plugin to manage its capture unit.\n"
    "polkit.addRule(function(action, subject) {\n"
    '    if (action.id == "org.freedesktop.systemd1.manage-units") {\n'
    '        var unit = action.lookup("unit") || action.lookup("name") || "";\n'
    '        if (unit == "flicker-engine.service") {\n'
    "            return polkit.Result.YES;\n"
    "        }\n"
    "    }\n"
    "});\n"
)

DEFAULTS = {"enabled": False, "mode": "unified", "sat_boost": 1.5, "ema": 0.25, "norm_max": 235, "floor": 100, "fps": 20, "stick_gain": 0.4}
TUNABLE = ("sat_boost", "ema", "norm_max", "floor", "stick_gain")


class Plugin:
    settings = dict(DEFAULTS)

    # ---------- lifecycle ----------
    async def _main(self):
        self.settings = self._load()
        self._ensure_polkit()
        decky.logger.info("flicker loaded (enabled=%s)" % self.settings.get("enabled"))
        if self.settings.get("enabled"):
            await self.start()

    async def _unload(self):
        self._stop_unit()
        self._restore()
        decky.logger.info("flicker unloaded")

    async def _uninstall(self):
        self._stop_unit()
        self._restore()

    # ---------- helpers ----------
    def _load(self):
        try:
            with open(SETTINGS_PATH) as f:
                return {**DEFAULTS, **json.load(f)}
        except Exception:
            return dict(DEFAULTS)

    def _save(self):
        try:
            os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
            with open(SETTINGS_PATH, "w") as f:
                json.dump(self.settings, f)
        except Exception as e:
            decky.logger.error("save failed: %s" % e)

    def _clean_env(self):
        # Decky leaks its PyInstaller LD_LIBRARY_PATH (/tmp/_MEI…); strip it so the
        # system binaries we invoke (systemd-run, systemctl, python) load the right
        # libs instead of Decky's bundled ones ("OPENSSL_… not found").
        env = dict(os.environ)
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig is not None:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
        env.pop("LD_PRELOAD", None)
        return env

    def _ensure_polkit(self):
        # Best-effort: only attempt if the rule is missing. Decky's sandbox usually
        # denies writing /etc/polkit-1, so this typically just logs the fallback.
        if os.path.exists(POLKIT_RULE):
            return
        try:
            with open(POLKIT_RULE, "w") as f:
                f.write(POLKIT_CONTENT)
            decky.logger.info("installed polkit rule -> %s" % POLKIT_RULE)
        except Exception as e:
            decky.logger.error("polkit rule missing and can't auto-install — run "
                               "'sudo ./decky-setup.sh' once: %s" % e)

    def _setenv_args(self):
        kv = {
            "FLICKER_MODE": str(self.settings["mode"]),
            "FLICKER_SAT_BOOST": str(self.settings["sat_boost"]),
            "FLICKER_EMA": str(self.settings["ema"]),
            "FLICKER_NORM_MAX": str(self.settings["norm_max"]),
            "FLICKER_FLOOR": str(self.settings.get("floor", 100)),
            "FLICKER_STICK": str(self.settings.get("stick_gain", 0.4)),
            "FLICKER_FPS": str(int(self.settings["fps"])),
            "FLICKER_CONFIG": SETTINGS_PATH,        # engine re-reads this live for the sliders
        }
        return ["--setenv=%s=%s" % (k, v) for k, v in kv.items()]

    def _stop_unit(self):
        for args in (["stop", UNIT], ["reset-failed", UNIT]):
            try:
                subprocess.run(["systemctl"] + args, timeout=8, env=self._clean_env(),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

    def _restore(self):
        # hand the LEDs back to Handheld Daemon
        try:
            subprocess.run([PY, ENGINE, "--restore"], timeout=5, env=self._clean_env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            decky.logger.error("restore failed: %s" % e)

    # ---------- callable from the frontend ----------
    async def start(self):
        self._stop_unit()
        self._ensure_polkit()
        self.settings["enabled"] = True
        self._save()                                # write before launch so the engine sees it
        cmd = ["systemd-run", "--unit=" + UNIT, "--collect",
               *self._setenv_args(), PY, ENGINE]
        for attempt in (1, 2):
            try:
                r = subprocess.run(cmd, timeout=10, env=self._clean_env(),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                if r.returncode == 0:
                    decky.logger.info("engine started as %s.service" % UNIT)
                    return True
                decky.logger.error("systemd-run failed (try %d): %s"
                                   % (attempt, r.stderr.decode(errors="replace").strip()))
            except Exception as e:
                decky.logger.error("start failed: %s" % e)
                return False
            # most likely polkit hasn't reloaded the freshly-written rule yet
            self._ensure_polkit()
            time.sleep(0.7)
        return False

    async def stop(self):
        self._stop_unit()
        self._restore()
        self.settings["enabled"] = False
        self._save()
        decky.logger.info("engine stopped")
        return True

    async def is_running(self):
        # journalctl -u flicker-engine has the engine's output if anything's wrong.
        try:
            r = subprocess.run(["systemctl", "is-active", UNIT], timeout=5, env=self._clean_env(),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            return r.stdout.strip() == b"active"
        except Exception:
            return False

    async def get_settings(self):
        return self.settings

    async def set_setting(self, key, value):
        if key in TUNABLE:
            self.settings[key] = value
            self._save()                            # engine picks it up live (FLICKER_CONFIG)
        return True

    async def set_mode(self, mode):
        if mode in ("unified", "split", "quad"):
            self.settings["mode"] = mode
            self._save()                            # engine switches live (FLICKER_CONFIG)
        return True
