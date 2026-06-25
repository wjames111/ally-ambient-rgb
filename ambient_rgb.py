#!/usr/bin/python3
"""ally-ambient-rgb — real-time ambient lighting for the ASUS ROG Ally
(and similar Handheld-Daemon devices).

Samples the screen, finds the dominant *vivid* color, and drives the
controller's RGB joystick rings (the emulated DualSense lightbar) to match it
at ~20 fps.

Pipeline:
    ffmpeg kmsgrab (DRM scanout)
      -> VAAPI GPU detile + downscale to GRID x GRID
      -> dominant-hue pick (saturation^2 * value weighted histogram)
      -> vivid-normalize (scale dominant channel up, boost saturation)
      -> write the lightbar LED sysfs, every frame.

Runs as root (kmsgrab and the LED sysfs both need it).

Config via environment variables (all optional):
    AMBIENT_CARD       DRM device for kmsgrab          (default /dev/dri/card1)
    AMBIENT_LED        LED sysfs dir; auto-detected if unset
    AMBIENT_GRID       downscale grid size             (default 48)
    AMBIENT_FPS        capture/update rate             (default 20)
    AMBIENT_EMA        smoothing, new-frame weight     (default 0.25)
    AMBIENT_SAT_BOOST  saturation multiplier           (default 1.5)
    AMBIENT_NORM_MAX   brightness of dominant channel  (default 210)

MIT licensed.  https://github.com/wjames111/ally-ambient-rgb
"""
import os, sys, glob, time, json, colorsys, subprocess, urllib.request
import numpy as np


def _env(name, default, cast=str):
    v = os.environ.get(name)
    try:
        return cast(v) if v is not None else default
    except Exception:
        return default


CARD      = _env("AMBIENT_CARD", "/dev/dri/card1")
GRID      = _env("AMBIENT_GRID", 48, int)
FPS       = _env("AMBIENT_FPS", 20, int)
EMA       = _env("AMBIENT_EMA", 0.25, float)
SAT_BOOST = _env("AMBIENT_SAT_BOOST", 1.5, float)
NORM_MAX  = _env("AMBIENT_NORM_MAX", 210.0, float)
BINS      = 24
TOKEN_PATH = "/tmp/hhd/token"
API = "http://127.0.0.1:5335/api/v1/state"


def hhd_rgb_mode(mode):
    """Ask Handheld Daemon to release ('disabled') or restore ('solid') the
    LEDs, so this process is the sole writer of the lightbar.  No-op if HHD is
    not running / no token."""
    try:
        tok = open(TOKEN_PATH).read().strip()
        body = {"rgb": {"handheld": {"mode": {"mode": mode}}}}
        req = urllib.request.Request(
            API, data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + tok,
                     "Content-Type": "application/json"},
            method="POST")
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass


if "--restore" in sys.argv:          # systemd ExecStopPost: hand the LEDs back to HHD
    hhd_rgb_mode("solid")
    sys.exit(0)


def find_led():
    """Locate the controller RGB LED sysfs dir.  The input<N> number is
    kernel-assigned and can change across reboots, so we glob rather than
    hardcode it."""
    override = os.environ.get("AMBIENT_LED")
    if override:
        return override
    for pat in ("/sys/class/leds/*:rgb:indicator", "/sys/class/leds/*:rgb:*"):
        c = sorted(glob.glob(pat))
        if c:
            return c[0]
    sys.stderr.write("ally-ambient-rgb: no RGB LED found under /sys/class/leds "
                     "(set AMBIENT_LED to the right dir)\n")
    sys.exit(1)


LED_DIR = find_led()
LED = LED_DIR + "/multi_intensity"
BRT = LED_DIR + "/brightness"


def set_led(r, g, b):
    with open(LED, "w") as f:
        f.write("%d %d %d" % (int(r), int(g), int(b)))


def dominant_color(a):
    """a:(GRID,GRID,3) float32 0-255 -> the prominent vivid RGB (0-255).

    Bins pixels by hue weighted by saturation^2 * value, takes the heaviest
    hue bin (+ neighbours) and averages it.  A whole-screen mean comes out a
    muddy grey; this locks onto the screen's most colorful region instead."""
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
    w = (s ** 2) * v                         # prominence = saturation^2 * brightness
    if w.sum() < 1e-3:                        # near-grey screen: use brightest pixels
        k = max(1, v.size // 10)
        return px[np.argpartition(v, -k)[-k:]].mean(0) * 255
    bi = np.minimum((h * BINS).astype(np.int64), BINS - 1)
    hist = np.bincount(bi, weights=w, minlength=BINS)
    dom = int(hist.argmax())
    near = (np.abs(bi - dom) <= 1) | (np.abs(bi - dom) >= BINS - 1)
    sw = w[near]
    return (px[near] * sw[:, None]).sum(0) / sw.sum() * 255


def vivid(col):
    """Normalize a color to be bright + saturated.  Dim colors render as a
    muddy teal on the rings (hardware color cast at low PWM), so we always send
    a vivid value."""
    r, g, b = float(col[0]), float(col[1]), float(col[2])
    m = max(r, g, b)
    if m < 1:
        return 0.0, 0.0, 0.0
    scale = NORM_MAX / m
    r, g, b = r * scale, g * scale, b * scale
    hh, ss, vv = colorsys.rgb_to_hsv(min(1.0, r/255.0), min(1.0, g/255.0), min(1.0, b/255.0))
    ss = min(1.0, ss * SAT_BOOST)
    r, g, b = colorsys.hsv_to_rgb(hh, ss, vv)
    return r * 255, g * 255, b * 255


FF = ["ffmpeg", "-hide_banner", "-loglevel", "error",
      "-f", "kmsgrab", "-device", CARD, "-framerate", str(FPS), "-i", "-",
      "-vf", f"hwmap=derive_device=vaapi,scale_vaapi=w={GRID}:h={GRID}:format=nv12,hwdownload,format=nv12",
      "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
NB = GRID * GRID * 3

try:
    with open(BRT, "w") as f:
        f.write("255")
except Exception:
    pass
hhd_rgb_mode("disabled")             # take sole control of the lightbar


def run_once():
    p = subprocess.Popen(FF, stdout=subprocess.PIPE)
    ema = None
    try:
        while True:
            buf = p.stdout.read(NB)
            if len(buf) < NB:
                return
            a = np.frombuffer(buf, np.uint8).reshape(GRID, GRID, 3).astype(np.float32)
            col = dominant_color(a)
            ema = col if ema is None else EMA * col + (1 - EMA) * ema
            R, G, B = vivid(ema)
            try:
                set_led(R, G, B)         # every frame, to hold against the lightbar default
            except Exception:
                pass
    finally:
        try:
            p.terminate()
        except Exception:
            pass


def main():
    while True:
        try:
            run_once()
        except Exception:
            pass
        time.sleep(2)                    # ffmpeg died (e.g. display asleep) -> retry


if __name__ == "__main__":
    main()
