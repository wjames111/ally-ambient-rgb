import os
import json
import subprocess

import decky

# Flicker Decky backend — NON-root plugin (runs as the user).
#
# Two engines, picked by mode:
#   * unified      -> flicker_unified.py, spawned as a plain USER subprocess.
#                     Captures gamescope's PipeWire node + drives the rings through
#                     Handheld Daemon's HSV API.  NO root, NO caps, NO setup.
#   * split / quad -> flicker.py, started as a ROOT systemd unit via systemd-run.
#                     Per-zone needs kmsgrab caps + the Aura MCU hidraw (root-locked
#                     by HHD), so it runs with full caps.  This path is gated behind
#                     a one-time `sudo ./decky-setup.sh` (installs a polkit rule that
#                     lets this user plugin manage the unit, and drops the marker we
#                     check below to unlock the Split/Quad modes in the UI).
PLUGIN_DIR = decky.DECKY_PLUGIN_DIR
SETTINGS_DIR = decky.DECKY_PLUGIN_SETTINGS_DIR
UNIFIED_ENGINE = os.path.join(PLUGIN_DIR, "flicker_unified.py")
PERZONE_ENGINE = os.path.join(PLUGIN_DIR, "flicker.py")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")
PERZONE_MARKER = os.path.join(SETTINGS_DIR, ".perzone_unlocked")   # dropped by decky-setup.sh
PY = "/usr/bin/python3"
UNIT = "flicker-engine"

DEFAULTS = {"enabled": False, "mode": "unified", "sat_boost": 1.5, "ema": 0.25,
            "norm_max": 120, "floor": 100, "fps": 20, "stick_gain": 0.4}
TUNABLE = ("sat_boost", "ema", "norm_max", "floor", "stick_gain")


def _is_unified(mode):
    return mode == "unified"


class Plugin:
    settings = dict(DEFAULTS)

    # ---------- lifecycle ----------
    async def _main(self):
        self.settings = self._load()
        decky.logger.info("flicker loaded (enabled=%s mode=%s)"
                          % (self.settings.get("enabled"), self.settings.get("mode")))
        if self.settings.get("enabled"):
            await self.start()

    async def _unload(self):
        self._stop_all()
        decky.logger.info("flicker unloaded")

    async def _uninstall(self):
        self._stop_all()

    # ---------- settings ----------
    def _load(self):
        try:
            with open(SETTINGS_PATH) as f:
                return {**DEFAULTS, **json.load(f)}
        except Exception:
            return dict(DEFAULTS)

    def _save(self):
        try:
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w") as f:
                json.dump(self.settings, f)
        except Exception as e:
            decky.logger.error("save failed: %s" % e)

    def _env(self):
        # Strip Decky's leaked PyInstaller LD_LIBRARY_PATH so system binaries load
        # the right libs; restore XDG_RUNTIME_DIR (Decky strips it, and the unified
        # engine needs it to reach PipeWire).
        env = dict(os.environ)
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig is not None:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
        env.pop("LD_PRELOAD", None)
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
        return env

    def _flicker_env(self):
        s = self.settings
        return {
            "FLICKER_MODE": str(s["mode"]),
            "FLICKER_SAT_BOOST": str(s["sat_boost"]),
            "FLICKER_EMA": str(s["ema"]),
            "FLICKER_NORM_MAX": str(s["norm_max"]),
            "FLICKER_FLOOR": str(s.get("floor", 100)),
            "FLICKER_STICK": str(s.get("stick_gain", 0.4)),
            "FLICKER_FPS": str(int(s["fps"])),
            "FLICKER_CONFIG": SETTINGS_PATH,        # engines re-read this live (sliders)
        }

    # ---------- per-zone unlock ----------
    def _per_zone_unlocked(self):
        return os.path.exists(PERZONE_MARKER)

    async def can_per_zone(self):
        return self._per_zone_unlocked()

    # ---------- engine control ----------
    def _stop_all(self):
        # stop the per-zone root unit ...
        for args in (["stop", UNIT], ["reset-failed", UNIT]):
            try:
                subprocess.run(["systemctl"] + args, timeout=8, env=self._env(),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        # ... and any unified user engine ...
        try:
            subprocess.run(["pkill", "-f", UNIFIED_ENGINE], timeout=5, env=self._env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        # ... then hand the rings back to HHD (both engines support --restore).
        for eng in (UNIFIED_ENGINE, PERZONE_ENGINE):
            try:
                subprocess.run([PY, eng, "--restore"], timeout=5, env=self._env(),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

    def _start_unified(self):
        env = self._env()
        env.update(self._flicker_env())
        subprocess.Popen([PY, UNIFIED_ENGINE], env=env, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        decky.logger.info("unified engine started (user subprocess)")
        return True

    def _start_perzone(self):
        setenv = ["--setenv=%s=%s" % (k, v) for k, v in self._flicker_env().items()]
        cmd = ["systemd-run", "--unit=" + UNIT, "--collect", *setenv, PY, PERZONE_ENGINE]
        r = subprocess.run(cmd, timeout=10, env=self._env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode != 0:
            decky.logger.error("per-zone start failed (run 'sudo ./decky-setup.sh' once): %s"
                               % r.stderr.decode(errors="replace").strip())
            return False
        decky.logger.info("per-zone engine started (root unit)")
        return True

    # ---------- callable from the frontend ----------
    async def start(self):
        self._stop_all()
        mode = self.settings.get("mode", "unified")
        # per-zone requires the unlock; otherwise fall back to unified
        if not _is_unified(mode) and not self._per_zone_unlocked():
            decky.logger.info("per-zone not unlocked; falling back to unified")
            mode = "unified"
            self.settings["mode"] = "unified"
        self.settings["enabled"] = True
        self._save()
        return self._start_unified() if _is_unified(mode) else self._start_perzone()

    async def stop(self):
        self._stop_all()
        self.settings["enabled"] = False
        self._save()
        decky.logger.info("flicker stopped")
        return True

    async def is_running(self):
        try:
            r = subprocess.run(["pgrep", "-f", UNIFIED_ENGINE], timeout=5, env=self._env(),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if r.stdout.strip():
                return True
        except Exception:
            pass
        try:
            r = subprocess.run(["systemctl", "is-active", UNIT], timeout=5, env=self._env(),
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
        if mode not in ("unified", "split", "quad"):
            return False
        if not _is_unified(mode) and not self._per_zone_unlocked():
            return False                            # locked until decky-setup.sh
        old = self.settings.get("mode", "unified")
        self.settings["mode"] = mode
        self._save()
        # Crossing the unified<->per-zone boundary swaps the ENGINE, so restart;
        # split<->quad is the same engine and switches live via FLICKER_CONFIG.
        if (_is_unified(old) != _is_unified(mode)) and await self.is_running():
            await self.start()
        return True
