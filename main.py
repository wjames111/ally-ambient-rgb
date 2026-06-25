import os
import json
import subprocess

import decky

ENGINE = os.path.join(decky.DECKY_PLUGIN_DIR, "flicker.py")
SETTINGS_PATH = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
PY = "/usr/bin/python3"

DEFAULTS = {"enabled": False, "sat_boost": 1.5, "ema": 0.25, "norm_max": 210, "fps": 20}
TUNABLE = ("sat_boost", "ema", "norm_max")


class Plugin:
    proc = None
    settings = dict(DEFAULTS)

    # ---------- lifecycle ----------
    async def _main(self):
        self.proc = None
        self.settings = self._load()
        decky.logger.info("flicker loaded (enabled=%s)" % self.settings.get("enabled"))
        if self.settings.get("enabled"):
            await self.start()

    async def _unload(self):
        self._kill()
        self._restore()
        decky.logger.info("flicker unloaded")

    async def _uninstall(self):
        self._kill()
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

    def _engine_env(self):
        env = dict(os.environ)
        env["FLICKER_SAT_BOOST"] = str(self.settings["sat_boost"])
        env["FLICKER_EMA"] = str(self.settings["ema"])
        env["FLICKER_NORM_MAX"] = str(self.settings["norm_max"])
        env["FLICKER_FPS"] = str(int(self.settings["fps"]))
        env["FLICKER_CONFIG"] = SETTINGS_PATH      # engine re-reads this live for the sliders
        return env

    def _running(self):
        return self.proc is not None and self.proc.poll() is None

    def _kill(self):
        if self.proc is None:
            return
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except Exception:
                    self.proc.kill()
        except Exception as e:
            decky.logger.error("kill failed: %s" % e)
        self.proc = None

    def _restore(self):
        # hand the LEDs back to Handheld Daemon
        try:
            subprocess.run([PY, ENGINE, "--restore"], timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            decky.logger.error("restore failed: %s" % e)

    # ---------- callable from the frontend ----------
    async def start(self):
        self._kill()
        self.settings["enabled"] = True
        self._save()                                # write before spawn so the engine sees it
        try:
            self.proc = subprocess.Popen([PY, ENGINE], env=self._engine_env(),
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            decky.logger.info("engine started (pid=%s)" % self.proc.pid)
            return True
        except Exception as e:
            decky.logger.error("start failed: %s" % e)
            return False

    async def stop(self):
        self._kill()
        self._restore()
        self.settings["enabled"] = False
        self._save()
        decky.logger.info("engine stopped")
        return True

    async def is_running(self):
        return self._running()

    async def get_settings(self):
        return self.settings

    async def set_setting(self, key, value):
        if key in TUNABLE:
            self.settings[key] = value
            self._save()                            # engine picks it up live (FLICKER_CONFIG)
        return True
