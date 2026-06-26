#!/usr/bin/python3
"""Flicker — UNIFIED zero-setup engine.

Captures gamescope's screen through its PipeWire screencast node (all user-level,
no root / no CAP_SYS_ADMIN / no kmsgrab) and drives both joystick rings through
Handheld Daemon's HSV solid mode.  No hidraw, no systemd-run, no polkit, no setup.

  pipewiresrc target-object=gamescope ! vapostproc (GPU scale+import the dmabuf)
    -> RGB frames -> one vibrancy-weighted colour -> HHD rgb.handheld.mode.solid

Decky strips the session env from plugin backends, so this self-sets
XDG_RUNTIME_DIR (pipewiresrc needs it to find the PipeWire socket).

HHD only drives the rings when the emulated DualSense LED is disabled, so we set
controller_mode.dualsense.led_support=false on start and restore it on stop
(toggling it does not re-enumerate the controller).  Unified mode only — HHD
mirrors one colour to both rings; split/quad use the per-zone kmsgrab engine
(flicker.py), which needs the one-time setup.

MIT.  https://github.com/wjames111/flicker
"""
import os, sys, json, time, colorsys, subprocess, threading, urllib.request

# pipewiresrc needs the user runtime dir; Decky doesn't pass it through.
os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())

try:
    import numpy as np
except ImportError:
    sys.stderr.write("flicker-unified: numpy is required for %s\n" % sys.executable)
    raise

GRID     = int(os.environ.get("FLICKER_GRID", "48"))
FPS      = int(os.environ.get("FLICKER_FPS", "20"))
DURATION = float(os.environ.get("FLICKER_DURATION", "0"))   # 0 = run forever (test override)
CONFIG   = os.environ.get("FLICKER_CONFIG")                 # live-tuning JSON (Decky sliders)
TOKEN_PATH = "/tmp/hhd/token"
API = "http://127.0.0.1:5335/api/v1/state"

CFG = {
    "sat_boost": float(os.environ.get("FLICKER_SAT_BOOST", "1.5")),
    "ema":       float(os.environ.get("FLICKER_EMA", "0.25")),
    "norm_max":  float(os.environ.get("FLICKER_NORM_MAX", "235")),  # brightness when scene is bright
    "floor":     float(os.environ.get("FLICKER_FLOOR", "100")),     # bias-light floor (never fully off)
}


def refresh_cfg():
    if not CONFIG:
        return
    try:
        d = json.load(open(CONFIG))
        for k in ("sat_boost", "ema", "norm_max", "floor"):
            if k in d:
                CFG[k] = float(d[k])
    except Exception:
        pass


# ---------- Handheld Daemon: HSV solid-colour drive ----------
def _post(body):
    try:
        tok = open(TOKEN_PATH).read().strip()
        req = urllib.request.Request(API, data=json.dumps(body).encode(),
              headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass


def take_rings():
    # Disable the emulated DualSense LED (else it owns the rings and HHD-solid is
    # ignored), then start solid at zero brightness.
    _post({"controllers": {"rog_ally": {"controller_mode": {"dualsense": {"led_support": False}}}}})
    _post({"rgb": {"handheld": {"mode": {"mode": "solid", "solid": {"hue": 0, "saturation": 100, "brightness": 0}}}}})


def release_rings():
    _post({"rgb": {"handheld": {"mode": {"mode": "solid"}}}})
    _post({"controllers": {"rog_ally": {"controller_mode": {"dualsense": {"led_support": True}}}}})


def drive_hsv(h, s, v):     # h 0..360, s/v 0..100
    _post({"rgb": {"handheld": {"mode": {"solid": {"hue": int(h), "saturation": int(s), "brightness": int(v)}}}}})


if "--restore" in sys.argv:     # Decky stop: hand the rings back to HHD
    release_rings()
    sys.exit(0)


# ---------- capture: gamescope PipeWire node -> raw RGB frames on stdout ----------
GST = ("gst-launch-1.0 -q -e pipewiresrc target-object=gamescope do-timestamp=true ! "
       "vapostproc ! video/x-raw,format=BGRx,width=%d,height=%d ! videoconvert ! "
       "video/x-raw,format=RGB ! fdsink fd=1" % (GRID, GRID))
NB = GRID * GRID * 3


def unified_hsv(a):
    """a:(GRID,GRID,3) -> (h,s,v) each 0..1, vibrancy-weighted (sat^2*value), with
    brightness tracking the scene down to a bias-light floor."""
    px = a.reshape(-1, 3).astype(np.float32) / 255.0
    mx = px.max(1); mn = px.min(1); val = mx
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    w = (sat ** 2) * val + 1e-3
    ws = w.sum()
    avg = (px * w[:, None]).sum(0) / ws
    scene = float((val * w).sum() / ws)
    hh, ss, _ = colorsys.rgb_to_hsv(float(min(1, avg[0])), float(min(1, avg[1])), float(min(1, avg[2])))
    ss = min(1.0, ss * CFG["sat_boost"])
    floor = CFG["floor"] / 255.0
    top = CFG["norm_max"] / 255.0
    bright = max(0.0, min(1.0, floor + (top - floor) * scene))
    return hh, ss, bright


def run_once(deadline):
    p = subprocess.Popen(GST, shell=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    state = {"buf": None, "alive": True}

    def reader():
        # drain at the source rate, keep only the latest frame, so HHD POSTs can
        # never back the capture up.
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

    ema = None; last = None; n = 0; t0 = time.time()
    try:
        while state["alive"]:
            if deadline and time.time() > deadline:
                break
            buf = state["buf"]
            if buf is None or buf is last:
                time.sleep(0.01)
                continue
            last = buf
            if n % 10 == 0:
                refresh_cfg()
            a = np.frombuffer(buf, np.uint8).reshape(GRID, GRID, 3)
            h, s, v = unified_hsv(a)
            # smooth in RGB so hue can't wrap 0<->360, then back to HSV for HHD
            r, g, b = colorsys.hsv_to_rgb(h, s, v)
            if ema is None:
                ema = [r, g, b]
            else:
                e = CFG["ema"]
                ema = [e * r + (1 - e) * ema[0], e * g + (1 - e) * ema[1], e * b + (1 - e) * ema[2]]
            hh, ss, vv = colorsys.rgb_to_hsv(*ema)
            drive_hsv(hh * 360, ss * 100, vv * 100)
            n += 1
            if n % 40 == 0:
                sys.stderr.write("flicker-unified: %d frames, %.1f fps, hsv=(%d,%d,%d)\n"
                                 % (n, n / max(1e-3, time.time() - t0), hh * 360, ss * 100, vv * 100))
                sys.stderr.flush()
            time.sleep(max(0.0, 1.0 / FPS))
    finally:
        state["alive"] = False
        try:
            p.terminate()
        except Exception:
            pass
    return n


def main():
    take_rings()
    deadline = (time.time() + DURATION) if DURATION > 0 else 0
    try:
        while True:
            if deadline and time.time() > deadline:
                break
            run_once(deadline)
            if deadline and time.time() > deadline:
                break
            time.sleep(1)     # capture died (display asleep / mode switch) -> retry
    finally:
        release_rings()


if __name__ == "__main__":
    main()
