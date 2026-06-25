# ally-ambient-rgb

**Real-time ambient lighting for the ASUS ROG Ally** (and similar
[Handheld Daemon](https://github.com/hhd-dev/hhd) devices running Bazzite /
SteamOS-likes).

It samples whatever's on screen, finds the dominant vivid color, and drives the
controller's **RGB joystick rings** to match — in games and on the desktop.
Red boss room → red rings. Forest → green. Fire → orange. About 20 fps, no
noticeable CPU cost (capture + downscale run on the GPU).

> 📸 *Add a short clip/photo of the rings reacting to a game here.*

---

## How it works

```
ffmpeg kmsgrab (DRM scanout)
  → VAAPI GPU detile + downscale to 48×48
  → dominant-hue pick  (saturation² × value weighted histogram)
  → vivid-normalize    (scale dominant channel up, boost saturation)
  → write the lightbar LED sysfs, every frame
```

The screen is captured straight from the DRM scanout (works under gamescope /
Game Mode), detiled and shrunk on the GPU, then reduced to a single **prominent**
color — a plain whole-screen average comes out a muddy grey, so instead it bins
pixels by hue weighted by `saturation² × value` and locks onto the most colorful
region. That color is normalized to a bright, saturated value and written to the
controller's RGB LED every frame.

## Requirements

- A handheld whose RGB rings are exposed as the emulated **DualSense lightbar**
  via Handheld Daemon (HHD), in **DualSense controller mode**.
- `ffmpeg` with `kmsgrab` + **VAAPI** (AMD APU — the ROG Ally's default).
- `python3` with `numpy`.
- Runs as **root** (kmsgrab and the LED sysfs both require it).

On Bazzite these are generally already present.

## Install

```bash
git clone https://github.com/wjames111/ally-ambient-rgb.git
cd ally-ambient-rgb
./install.sh          # installs to /opt, enables the systemd service
```

That's it — the rings start following the screen, and the service comes back on
every boot.

## Usage

```bash
sudo systemctl stop  ambient-rgb     # off  → your normal HHD RGB is restored automatically
sudo systemctl start ambient-rgb     # on
sudo systemctl disable ambient-rgb   # don't start at boot
journalctl -u ambient-rgb -f         # logs
```

## Configuration

Tune via environment variables (set them in the `[Service]` block of
`/etc/systemd/system/ambient-rgb.service`, then
`sudo systemctl daemon-reload && sudo systemctl restart ambient-rgb`):

| Variable            | Default          | Meaning                                   |
|---------------------|------------------|-------------------------------------------|
| `AMBIENT_SAT_BOOST` | `1.5`            | Saturation multiplier (vividness)         |
| `AMBIENT_EMA`       | `0.25`           | Smoothing — lower = smoother/slower fades  |
| `AMBIENT_NORM_MAX`  | `210`            | Brightness of the dominant channel        |
| `AMBIENT_FPS`       | `20`             | Capture / update rate                      |
| `AMBIENT_GRID`      | `48`             | Downscale grid size                       |
| `AMBIENT_CARD`      | `/dev/dri/card1` | DRM device for capture                     |
| `AMBIENT_LED`       | *(auto)*         | LED sysfs dir, if auto-detect picks wrong |

## Notes / gotchas

Things that took real debugging — useful if you adapt this to another device:

- **Write the lightbar LED sysfs directly**
  (`/sys/class/leds/*:rgb:indicator/multi_intensity`). In DualSense mode the
  lightbar *overrides* HHD's own solid-color RGB mode, so posting a color to
  HHD's API does **not** drive the rings — you have to write the lightbar.
- **Tell HHD to step aside.** HHD's solid RGB mode fights you (both write the
  lightbar → muddy blend). The service POSTs HHD `rgb.mode = disabled` on start
  and `= solid` on stop, so it's the sole writer while running.
- **Dark colors render as muddy teal** on the rings (a hardware color cast at
  low PWM); bright/saturated colors render true. That's why every color is
  vivid-normalized before it's sent — never a dim value.
- **Write every frame**, not just on change. If you stop writing on a static
  screen, the ring drifts back to the lightbar default within seconds.
- The `input<N>` in the LED path is kernel-assigned and can change across
  reboots, so the device is globbed, not hardcoded.

## Compatibility

Developed and tested on a **ROG Ally X** running **Bazzite** (Fedora 43) with
**Handheld Daemon** in DualSense mode. It should work on other HHD devices with
RGB rings and an AMD APU (original ROG Ally, Legion Go, etc.), but those are
untested — reports welcome.

## Uninstall

```bash
./uninstall.sh
```

## License

[MIT](LICENSE) © 2026 William James
