#!/usr/bin/python3
"""Flicker — real-time ambient lighting for the ASUS ROG Ally.

Samples the screen, picks the dominant vivid color(s), and drives the
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
import numpy as np

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
BINS = 24
CONFIG = os.environ.get("FLICKER_CONFIG")

# Live-tunable params: env sets the initial value; FLICKER_CONFIG overrides at runtime.
CFG = {
    "mode":      _env("FLICKER_MODE", "unified"),
    "sat_boost": _env("FLICKER_SAT_BOOST", 1.5, float),
    "ema":       _env("FLICKER_EMA", 0.25, float),
    "norm_max":  _env("FLICKER_NORM_MAX", 210.0, float),
}


def refresh_cfg():
    if not CONFIG:
        return
    try:
        with open(CONFIG) as f:
            d = json.load(f)
        if d.get("mode") in ("unified", "split", "quad"):
            CFG["mode"] = d["mode"]
        for k in ("sat_boost", "ema", "norm_max"):
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
def dominant_color(a):
    """a:(H,W,3) float32 0-255 -> the prominent vivid RGB (0-255).  Hue
    histogram weighted by saturation^2 * value; a plain mean is muddy grey."""
    px = a.reshape(-1, 3) / 255.0
    r, g, b = px[:, 0], px[:, 1], px[:, 2]
    mx = px.max(1); mn = px.min(1); diff = mx - mn
    v = mx
    s = np.where(mx > 1e-9, diff / np.maximum(mx, 1e-9), 0.0)
    h = np.zeros_like(mx); nz = diff > 1e-9
    rm = nz & (mx == r); gm = nz & (mx == g) & ~rm; bm = nz & (mx == b) & ~rm & ~gm
    h[rm] = ((g[rm] - b[rm]) / diff[rm]) % 6.0
    h[gm] = ((b[gm] - r[gm]) / diff[gm]) + 2.0
    h[bm] = ((r[bm] - g[bm]) / diff[bm]) + 4.0
    h = (h / 6.0) % 1.0
    w = (s ** 2) * v
    if w.sum() < 1e-3:
        k = max(1, v.size // 10)
        return px[np.argpartition(v, -k)[-k:]].mean(0) * 255
    bi = np.minimum((h * BINS).astype(np.int64), BINS - 1)
    hist = np.bincount(bi, weights=w, minlength=BINS)
    dom = int(hist.argmax())
    near = (np.abs(bi - dom) <= 1) | (np.abs(bi - dom) >= BINS - 1)
    sw = w[near]
    return (px[near] * sw[:, None]).sum(0) / sw.sum() * 255


def vivid(col):
    """Normalize to bright + saturated — dim colors render as muddy teal on the
    rings, so never send a dark value."""
    r, g, b = float(col[0]), float(col[1]), float(col[2])
    m = max(r, g, b)
    if m < 1:
        return 0.0, 0.0, 0.0
    scale = CFG["norm_max"] / m
    r, g, b = r * scale, g * scale, b * scale
    hh, ss, vv = colorsys.rgb_to_hsv(min(1.0, r/255.0), min(1.0, g/255.0), min(1.0, b/255.0))
    ss = min(1.0, ss * CFG["sat_boost"])
    r, g, b = colorsys.hsv_to_rgb(hh, ss, vv)
    return r * 255, g * 255, b * 255


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
                col = dominant_color(region)
                e = CFG["ema"]
                ema[key] = col if ema.get(key) is None else e * col + (1 - e) * ema[key]
                R, G, B = vivid(ema[key])
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
