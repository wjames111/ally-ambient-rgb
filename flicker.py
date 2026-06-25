#!/usr/bin/python3
"""Flicker — real-time ambient lighting for the ASUS ROG Ally.

Samples the screen, computes a vibrancy-weighted color per zone, and drives the
controller's RGB joystick rings to match, ~20 fps.  Three modes:

  unified  one color from the whole screen -> both rings
  split    left half -> left ring, right half -> right ring
  quad     each screen corner -> the matching ring half (4 zones)

Drives the Ally's Aura RGB MCU directly over HID (per-zone), with Handheld
Daemon told to stand down — this is what allows independent left/right rings
(the emulated DualSense lightbar can only show one mirrored color).

Runs as root (kmsgrab and the RGB hidraw both need it).  ROG Ally / Ally X only.

Config via environment variables (all optional):
  FLICKER_MODE       unified | split | quad          (default unified)
  FLICKER_CARD       DRM device for kmsgrab          (default /dev/dri/card1)
  FLICKER_GRID       downscale grid size             (default 48)
  FLICKER_FPS        capture rate                    (default 20)
  FLICKER_EMA        smoothing, new-frame weight     (default 0.25)
  FLICKER_SAT_BOOST  saturation multiplier           (default 1.5)
  FLICKER_NORM_MAX   brightness of dominant channel  (default 210)
  FLICKER_HIDRAW     override the RGB hidraw path (auto-detected otherwise)
  FLICKER_CONFIG     live-tuning JSON {mode,sat_boost,ema,norm_max} (Decky plugin)

MIT licensed.  https://github.com/wjames111/flicker
"""
import os, sys, time, json, colorsys, subprocess, threading, urllib.request
try:
    import numpy as np
except ImportError:
    sys.stderr.write(
        "flicker: numpy is required but isn't installed for %s\n"
        "  Install it for that interpreter, e.g.:  pip install --user numpy hid\n"
        % sys.executable)
    raise

try:
    import hid  # hidapi — used only to locate the Aura RGB interface
except Exception:
    hid = None


def _env(name, default, cast=str):
    v = os.environ.get(name)
    try:
        return cast(v) if v is not None else default
    except Exception:
        return default


CARD = _env("FLICKER_CARD", "/dev/dri/card1")
GRID = _env("FLICKER_GRID", 48, int)
FPS  = _env("FLICKER_FPS", 20, int)
CONFIG = os.environ.get("FLICKER_CONFIG")

# Live-tunable params: env sets the initial value; FLICKER_CONFIG overrides at runtime.
CFG = {
    "mode":      _env("FLICKER_MODE", "unified"),
    "sat_boost": _env("FLICKER_SAT_BOOST", 1.5, float),
    "ema":       _env("FLICKER_EMA", 0.25, float),
    "norm_max":  _env("FLICKER_NORM_MAX", 235.0, float),   # brightness when the scene is bright
    "floor":     _env("FLICKER_FLOOR", 100.0, float),      # bias-light floor for dark scenes (never off)
}


def refresh_cfg():
    if not CONFIG:
        return
    try:
        with open(CONFIG) as f:
            d = json.load(f)
        if d.get("mode") in ("unified", "split", "quad"):
            CFG["mode"] = d["mode"]
        for k in ("sat_boost", "ema", "norm_max", "floor"):
            if k in d:
                CFG[k] = float(d[k])
    except Exception:
        pass


# ---------- Handheld Daemon: take / release the RGB ----------
TOKEN_PATH = "/tmp/hhd/token"
API = "http://127.0.0.1:5335/api/v1/state"


def hhd_rgb_mode(mode):
    """Ask HHD to release ('disabled') or restore ('solid') the LEDs so we are
    the sole writer of the MCU.  No-op if HHD isn't running."""
    try:
        tok = open(TOKEN_PATH).read().strip()
        body = {"rgb": {"handheld": {"mode": {"mode": mode}}}}
        req = urllib.request.Request(API, data=json.dumps(body).encode(),
              headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass


if "--restore" in sys.argv:          # systemd ExecStopPost / Decky stop: hand LEDs back to HHD
    hhd_rgb_mode("solid")
    sys.exit(0)


# ---------- ROG Ally Aura RGB over HID (per-zone) ----------
# Protocol mirrors HHD's hhd/device/rog_ally.  Zones: 0x01 left-bottom,
# 0x02 left-top, 0x03 right-bottom, 0x04 right-top, 0x00 = all rings.
# Solid command = [0x5A,0xB3,zone,0x00,R,G,B,...].
ASUS_VID = 0x0B05
FK = 0x5A


def _pad(x):
    return bytes(bytearray(x) + bytearray(64 - len(x)))


def _find_rgb_hidraw():
    ov = os.environ.get("FLICKER_HIDRAW")
    if ov:
        return ov
    if hid is None:
        return None
    try:
        for d in hid.enumerate(ASUS_VID, 0):
            if d.get("usage_page") == 0xFF31 and d.get("usage") == 0x0080:
                p = d.get("path")
                return p.decode() if isinstance(p, (bytes, bytearray)) else p
    except Exception:
        pass
    return None


class AllyRGB:
    INIT   = _pad([FK, 0x41, 0x53, 0x55, 0x53, 0x20, 0x54, 0x65, 0x63, 0x68, 0x2E, 0x49, 0x6E, 0x63, 0x2E])
    BRIGHT = _pad([FK, 0xBA, 0xC5, 0xC4, 0x03])      # brightness: high
    SET    = _pad([FK, 0xB5])
    APPLY  = _pad([FK, 0xB4])

    def __init__(self):
        self.path = _find_rgb_hidraw()
        self.fd = None

    def ok(self):
        return self.path is not None

    def _open(self):
        if self.fd is None and self.path:
            self.fd = os.open(self.path, os.O_RDWR)

    def _w(self, b):
        try:
            self._open()
            if self.fd is not None:
                os.write(self.fd, b)
        except Exception:
            # hidraw number may have changed — drop the handle and re-find next time
            try:
                if self.fd is not None:
                    os.close(self.fd)
            except Exception:
                pass
            self.fd = None
            self.path = _find_rgb_hidraw()

    @staticmethod
    def _solid(zone, r, g, b):
        return _pad([FK, 0xB3, zone, 0x00, int(r), int(g), int(b), 0x00, 0x00, 0x00, 0, 0, 0])

    def init(self):
        for cmd in (self.INIT, self.BRIGHT,
                    self._solid(0x01, 0, 0, 0), self._solid(0x02, 0, 0, 0),
                    self._solid(0x03, 0, 0, 0), self._solid(0x04, 0, 0, 0),
                    self.SET, self.APPLY):
            self._w(cmd)

    def set_zone(self, zone, r, g, b):              # zone 0x00 = all rings
        self._w(self._solid(zone, r, g, b))

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None


# ---------- color extraction ----------
def zone_color(a):
    """a:(H,W,3) -> LED RGB (numpy [3], 0-255).

    Vibrancy-weighted average: each pixel is weighted by saturation² × value, so
    saturated/bright pixels dominate — a mostly-grey zone with a vivid accent
    glows that accent's color (not a muddy true-average), while brightness tracks
    the scene so dark zones dim to a bias-light floor instead of switching off."""
    px = a.reshape(-1, 3).astype(np.float32) / 255.0
    mx = px.max(1); mn = px.min(1)
    val = mx
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    w = (sat ** 2) * val + 1e-3                          # vibrancy weighting (+tiny base)
    ws = w.sum()
    avg = (px * w[:, None]).sum(0) / ws                  # vivid-weighted average color
    scene = float((val * w).sum() / ws)                  # vivid-weighted brightness (eye-drawn)
    hh, ss, _ = colorsys.rgb_to_hsv(float(min(1.0, avg[0])), float(min(1.0, avg[1])), float(min(1.0, avg[2])))
    ss = min(1.0, ss * CFG["sat_boost"])
    floor = CFG["floor"] / 255.0
    top = CFG["norm_max"] / 255.0
    bright = max(0.0, min(1.0, floor + (top - floor) * scene))   # bias-light floor .. top
    return np.array(colorsys.hsv_to_rgb(hh, ss, bright)) * 255.0


# ---------- capture ----------
FF = ["ffmpeg", "-hide_banner", "-loglevel", "error",
      "-f", "kmsgrab", "-device", CARD, "-framerate", str(FPS), "-i", "-",
      "-vf", f"hwmap=derive_device=vaapi,scale_vaapi=w={GRID}:h={GRID}:format=nv12,hwdownload,format=nv12",
      "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
NB = GRID * GRID * 3
THRESH = 5          # per-channel delta below which a zone isn't rewritten (MCU holds state)


def _regions(mode, a):
    """[(label, zones, subarray)] for the active mode.  Zones: 0x01 left-bottom,
    0x02 left-top, 0x03 right-bottom, 0x04 right-top; 0x00 = all."""
    h = GRID // 2
    if mode == "quad":
        return (("lt", (0x02,), a[:h, :h]),   # screen top-left     -> left ring top
                ("lb", (0x01,), a[h:, :h]),   # screen bottom-left  -> left ring bottom
                ("rt", (0x04,), a[:h, h:]),   # screen top-right    -> right ring top
                ("rb", (0x03,), a[h:, h:]))   # screen bottom-right -> right ring bottom
    if mode == "split":
        return (("l", (0x01, 0x02), a[:, :h]),
                ("r", (0x03, 0x04), a[:, h:]))
    return (("all", (0x00,), a),)


def run_once(rgb):
    p = subprocess.Popen(FF, stdout=subprocess.PIPE)
    state = {"buf": None, "alive": True}

    def reader():
        # drain the pipe at ffmpeg's full rate, keeping only the latest frame,
        # so slow LED writes never back the capture up (which broke the pipe).
        try:
            while state["alive"]:
                b = p.stdout.read(NB)
                if len(b) < NB:
                    break
                state["buf"] = b
        except Exception:
            pass
        finally:
            state["alive"] = False

    threading.Thread(target=reader, daemon=True).start()

    ema = {}; last = {}; cur_mode = None
    lastbuf = None
    n = 0
    try:
        while state["alive"]:
            buf = state["buf"]
            if buf is None or buf is lastbuf:     # no new frame yet
                time.sleep(0.005)
                continue
            lastbuf = buf
            if n % 10 == 0:
                refresh_cfg()
            mode = CFG["mode"]
            force = (n % 40 == 0) or (mode != cur_mode)   # periodic re-assert + on mode switch
            if mode != cur_mode:
                ema = {}; last = {}; cur_mode = mode
            a = np.frombuffer(buf, np.uint8).reshape(GRID, GRID, 3).astype(np.float32)
            for key, zones, region in _regions(mode, a):
                col = zone_color(region)
                e = CFG["ema"]
                ema[key] = col if ema.get(key) is None else e * col + (1 - e) * ema[key]
                R, G, B = ema[key]
                cur = (int(R), int(G), int(B))
                lk = last.get(key)
                if force or lk is None or abs(cur[0]-lk[0]) > THRESH or abs(cur[1]-lk[1]) > THRESH or abs(cur[2]-lk[2]) > THRESH:
                    for z in zones:
                        rgb.set_zone(z, *cur)
                    last[key] = cur
            n += 1
    finally:
        state["alive"] = False
        try:
            p.terminate()
        except Exception:
            pass


def main():
    rgb = AllyRGB()
    if not rgb.ok():
        sys.stderr.write("flicker: Aura RGB interface not found (ASUS 0B05, "
                         "usage_page 0xFF31). Is this a ROG Ally running HHD?\n")
    hhd_rgb_mode("disabled")             # take sole control of the MCU
    if rgb.ok():
        rgb.init()
    while True:
        try:
            if not rgb.ok():
                rgb.path = _find_rgb_hidraw()
                if rgb.ok():
                    rgb.init()
            run_once(rgb)
        except Exception:
            pass
        time.sleep(2)                    # ffmpeg died (e.g. display asleep) -> retry


if __name__ == "__main__":
    main()
