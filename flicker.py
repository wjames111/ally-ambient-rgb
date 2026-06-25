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
  FLICKER_STICK      joystick brightness boost, 0..1  (default 0.4; 0 = off)
  FLICKER_HIDRAW     override the RGB hidraw path (auto-detected otherwise)
  FLICKER_CONFIG     live-tuning JSON {mode,sat_boost,ema,norm_max} (Decky plugin)

MIT licensed.  https://github.com/wjames111/flicker
"""
import os, sys, time, json, math, glob, fcntl, select, struct, colorsys, subprocess, threading, urllib.request
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
    "stick_gain":_env("FLICKER_STICK", 0.4, float),        # how far a deflected stick lifts brightness (0 = off)
}


def refresh_cfg():
    if not CONFIG:
        return
    try:
        with open(CONFIG) as f:
            d = json.load(f)
        if d.get("mode") in ("unified", "split", "quad"):
            CFG["mode"] = d["mode"]
        for k in ("sat_boost", "ema", "norm_max", "floor", "stick_gain"):
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
def zone_color(a, boost=0.0):
    """a:(H,W,3) -> LED RGB (numpy [3], 0-255).

    Vibrancy-weighted average: each pixel is weighted by saturation² × value, so
    saturated/bright pixels dominate — a mostly-grey zone with a vivid accent
    glows that accent's color (not a muddy true-average), while brightness tracks
    the scene so dark zones dim to a bias-light floor instead of switching off.

    boost (0..1) lifts this zone's brightness toward full — used to flare the ring
    up when its joystick is pushed (see Sticks)."""
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
    if boost > 0.0:
        bright += (1.0 - bright) * min(1.0, boost)               # joystick lift toward full
    return np.array(colorsys.hsv_to_rgb(hh, ss, bright)) * 255.0


# ---------- joystick deflection (rings flare up when you move a stick) ----------
# Read the gamepad's analog sticks straight from evdev — no extra deps. A background
# thread tracks how far each stick is pushed (0..1); run_once lifts the matching
# ring's brightness by that × CFG["stick_gain"] (left stick -> left ring, right ->
# right, both in unified mode). Best-effort: if no gamepad is found — or Steam has it
# grabbed — the deflection stays 0 and nothing changes. ioctl numbers are x86_64.
EV_KEY, EV_ABS = 0x01, 0x03
ABS_X, ABS_Y, ABS_RX, ABS_RY = 0x00, 0x01, 0x03, 0x04
BTN_GAMEPAD = 0x130
STICK_DEADZONE = 0.12
_EVSZ = struct.calcsize("@llHHi")        # struct input_event on 64-bit (24 bytes)


def _ioc_E(nr, size):                    # _IOC(_IOC_READ, 'E', nr, size) on x86_64
    return (2 << 30) | (size << 16) | (ord("E") << 8) | nr


def _gamepad_axes(fd):
    """If fd is a gamepad, return ({axis: (center, half_range)}, {axis: current}) for
    the stick axes it reports (needs at least ABS_X/ABS_Y); else None. The ABS_X/Y
    requirement plus the gamepad-button check keeps absolute mice / touchpads out."""
    try:
        nbytes = BTN_GAMEPAD // 8 + 1
        keys = fcntl.ioctl(fd, _ioc_E(0x20 + EV_KEY, nbytes), bytes(nbytes))   # EVIOCGBIT(EV_KEY)
        if not (keys[BTN_GAMEPAD // 8] & (1 << (BTN_GAMEPAD % 8))):
            return None
    except Exception:
        return None
    axes, cur = {}, {}
    for ax in (ABS_X, ABS_Y, ABS_RX, ABS_RY):
        try:
            info = fcntl.ioctl(fd, _ioc_E(0x40 + ax, 24), bytes(24))           # EVIOCGABS(ax)
            value, mn, mx, _, _, _ = struct.unpack("@6i", info)
            if mx > mn:
                axes[ax] = ((mn + mx) / 2.0, (mx - mn) / 2.0)
                cur[ax] = value                                               # seed from real position
        except Exception:
            pass
    return (axes, cur) if (ABS_X in axes and ABS_Y in axes) else None


def _deflection(raw, axes, ax_x, ax_y):
    """0..1 distance the (ax_x, ax_y) stick is pushed from center, past a deadzone."""
    if ax_x not in axes or ax_y not in axes:
        return 0.0
    (cx, hx), (cy, hy) = axes[ax_x], axes[ax_y]
    m = math.hypot((raw[ax_x] - cx) / hx, (raw[ax_y] - cy) / hy)
    if m <= STICK_DEADZONE:
        return 0.0
    return min(1.0, (m - STICK_DEADZONE) / (1.0 - STICK_DEADZONE))


class Sticks:
    """Background evdev reader. .l / .r are each stick's deflection, 0..1."""

    def __init__(self):
        self.l = 0.0
        self.r = 0.0
        self._devs = {}          # path -> [fd, axes, raw]

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _rescan(self):
        # add gamepads that appeared (controller woke / hot-plugged); leave open ones be
        for path in glob.glob("/dev/input/event*"):
            if path in self._devs:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except Exception:
                continue
            res = _gamepad_axes(fd)
            if res:
                axes, cur = res
                self._devs[path] = [fd, axes, cur]
            else:
                try:
                    os.close(fd)
                except Exception:
                    pass

    def _drop(self, path):
        d = self._devs.pop(path, None)
        if d:
            try:
                os.close(d[0])
            except Exception:
                pass

    def _recompute(self):
        l = r = 0.0
        for _, axes, raw in self._devs.values():
            l = max(l, _deflection(raw, axes, ABS_X, ABS_Y))
            r = max(r, _deflection(raw, axes, ABS_RX, ABS_RY))
        self.l, self.r = l, r

    def _loop(self):
        last_scan = 0.0
        while True:
            now = time.time()
            if now - last_scan > 3.0:
                self._rescan()
                last_scan = now
            if not self._devs:
                self.l = self.r = 0.0
                time.sleep(1.0)
                continue
            by_fd = {d[0]: p for p, d in self._devs.items()}
            try:
                ready, _, _ = select.select(list(by_fd), [], [], 0.5)
            except Exception:
                for p in list(self._devs):
                    self._drop(p)
                continue
            for fd in ready:
                path = by_fd.get(fd)
                if path is None:
                    continue
                _, axes, raw = self._devs[path]
                try:
                    data = os.read(fd, _EVSZ * 64)
                    if not data:
                        self._drop(path)
                        continue
                except Exception:
                    self._drop(path)                 # controller slept / unplugged -> rescan picks it back up
                    continue
                for off in range(0, len(data) - _EVSZ + 1, _EVSZ):
                    _, _, etype, code, value = struct.unpack_from("@llHHi", data, off)
                    if etype == EV_ABS and code in raw:
                        raw[code] = value
            self._recompute()


sticks = Sticks()


def _stick_boost(key):
    """Brightness lift for a region, from the stick that drives it."""
    g = CFG["stick_gain"]
    if g <= 0.0:
        return 0.0
    if key in ("l", "lt", "lb"):
        d = sticks.l
    elif key in ("r", "rt", "rb"):
        d = sticks.r
    else:                                            # "all" (unified): whichever stick leads
        d = max(sticks.l, sticks.r)
    return d * g


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


def run_once(rgb, on_first_frame=None):
    """Capture + drive until ffmpeg dies.  Returns the number of frames driven —
    0 means capture never produced a usable frame (display asleep, or a scanout
    kmsgrab can't grab, e.g. Desktop Mode's 10-bit XR30 framebuffer).

    on_first_frame() runs once, when the first real frame arrives, letting the
    caller defer taking the LEDs from HHD until capture is actually working."""
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
            if n == 0 and on_first_frame is not None:
                on_first_frame()                  # capture works -> now take the rings
            if n % 10 == 0:
                refresh_cfg()
            mode = CFG["mode"]
            force = (n % 40 == 0) or (mode != cur_mode)   # periodic re-assert + on mode switch
            if mode != cur_mode:
                ema = {}; last = {}; cur_mode = mode
            a = np.frombuffer(buf, np.uint8).reshape(GRID, GRID, 3).astype(np.float32)
            for key, zones, region in _regions(mode, a):
                col = zone_color(region, _stick_boost(key))
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
    return n


# ---------- don't grab the display before the compositor ----------
# kmsgrab needs DRM and, at boot, can become DRM master *before* gamescope/KDE —
# which black-screens the device (the compositor then can't acquire the display).
# So wait until a compositor already owns it before we capture; then kmsgrab just
# reads its output (the in-session case that always worked).
COMPOSITORS = ("gamescope", "kwin_wayland", "kwin_x11", "plasmashell",
               "gnome-shell", "mutter", "sway", "weston", "Xorg", "Xwayland")


def _compositor_up():
    for name in COMPOSITORS:
        try:
            if subprocess.run(["pgrep", "-x", name],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                return True
        except Exception:
            pass
    return False


def main():
    rgb = AllyRGB()
    if not rgb.ok():
        sys.stderr.write("flicker: Aura RGB interface not found (ASUS 0B05, "
                         "usage_page 0xFF31). Is this a ROG Ally running HHD?\n")
    sticks.start()                       # joystick-driven brightness boost (best-effort)
    controlled = False                   # True once we've taken the MCU from HHD

    def take_control():
        # Defer until capture actually works.  The old code disabled HHD and
        # blacked the rings up front, so any capture failure (e.g. Desktop Mode's
        # 10-bit scanout, which kmsgrab can't grab) left them stuck dead-black.
        nonlocal controlled
        hhd_rgb_mode("disabled")         # take sole control of the MCU
        if rgb.ok():
            rgb.init()
        controlled = True

    while True:
        # Never grab the display before a compositor owns it — at boot, kmsgrab
        # becoming DRM master before gamescope/KDE black-screens the device.
        waited = 0
        while not _compositor_up() and waited < 120:
            if controlled:                   # release the rings to HHD while we wait
                hhd_rgb_mode("solid")
                controlled = False
            time.sleep(1)
            waited += 1
        try:
            if not rgb.ok():
                rgb.path = _find_rgb_hidraw()
                if rgb.ok() and controlled:
                    rgb.init()
            frames = run_once(rgb, None if controlled else take_control)
        except Exception:
            frames = 0
        if frames == 0 and controlled:
            # Capture stopped producing frames (display asleep, or a scanout we
            # can't grab) — hand the rings back to HHD rather than leaving them
            # stuck on black until a capturable scene returns.
            hhd_rgb_mode("solid")
            controlled = False
        time.sleep(2)                    # ffmpeg died / display asleep / mode switch -> retry


if __name__ == "__main__":
    main()
