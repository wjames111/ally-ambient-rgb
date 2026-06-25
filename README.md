# ally-ambient-rgb

**Real-time ambient lighting for the ASUS ROG Ally** (and similar
[Handheld Daemon](https://github.com/hhd-dev/hhd) devices running Bazzite /
SteamOS-likes).

It samples whatever's on screen, finds the dominant vivid color, and drives the
controller's **RGB joystick rings** to match — in games and on the desktop.
Red boss room → red rings. Forest → green. Fire → orange. About 20 fps, with
the capture + downscale done on the GPU so it's nearly free.

Ships two ways: a **Decky Loader plugin** (toggle + sliders in Game Mode) and a
**standalone systemd service**.

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

The screen is captured from the DRM scanout (works under gamescope / Game
Mode), detiled and shrunk on the GPU, then reduced to a single **prominent**
color — a plain whole-screen average comes out a muddy grey, so instead it bins
pixels by hue weighted by `saturation² × value` and locks onto the most
colorful region. That color is normalized to a bright, saturated value and
written to the controller's RGB LED every frame.

## Install — Decky plugin (recommended)

Once it's in the [Decky](https://decky.xyz/) store, install it from the in-Game-Mode
store. To build and sideload it now:

```bash
git clone https://github.com/wjames111/ally-ambient-rgb.git
cd ally-ambient-rgb
pnpm i && pnpm build          # needs Node 18+ and pnpm v9
# copy the built plugin into Decky's plugins dir (root) and restart Decky:
PLUGIN=~/homebrew/plugins/ally-ambient-rgb
sudo mkdir -p "$PLUGIN"
sudo cp -r plugin.json package.json main.py ambient_rgb.py dist "$PLUGIN"/
sudo systemctl restart plugin_loader
```

Then open the **Decky** menu in Game Mode → **Ally Ambient RGB** → toggle it on.
The panel has sliders for **Vividness**, **Reactivity**, and **Brightness** that
tune it live.

## Install — standalone (no Decky)

```bash
git clone https://github.com/wjames111/ally-ambient-rgb.git
cd ally-ambient-rgb
./install.sh                  # installs to /opt, enables a systemd service
```

```bash
sudo systemctl stop  ambient-rgb     # off → your normal HHD RGB is restored automatically
sudo systemctl start ambient-rgb     # on
journalctl -u ambient-rgb -f         # logs
```

## Requirements

- A handheld whose RGB rings are exposed as the emulated **DualSense lightbar**
  via Handheld Daemon (HHD), in **DualSense controller mode**.
- `ffmpeg` with `kmsgrab` + **VAAPI** (AMD APU — the ROG Ally's default).
- `python3` with `numpy`.
- Runs as **root** (kmsgrab and the LED sysfs both require it). The Decky plugin
  declares the `_root` flag; the standalone runs as a root systemd service.

On Bazzite these are generally already present.

## Configuration (standalone)

Set these env vars in the `[Service]` block of
`/etc/systemd/system/ambient-rgb.service`, then `daemon-reload` + restart:

| Variable            | Default          | Meaning                                  |
|---------------------|------------------|------------------------------------------|
| `AMBIENT_SAT_BOOST` | `1.5`            | Saturation multiplier (vividness)        |
| `AMBIENT_EMA`       | `0.25`           | Smoothing — lower = smoother/slower      |
| `AMBIENT_NORM_MAX`  | `210`            | Brightness of the dominant channel       |
| `AMBIENT_FPS`       | `20`             | Capture / update rate                    |
| `AMBIENT_GRID`      | `48`             | Downscale grid size                      |
| `AMBIENT_CARD`      | `/dev/dri/card1` | DRM device for capture                   |
| `AMBIENT_LED`       | *(auto)*         | LED sysfs dir, if auto-detect picks wrong|

(The Decky plugin exposes Vividness / Reactivity / Brightness as live sliders.)

## Notes / gotchas

Things that took real debugging — useful if you adapt this to another device:

- **Write the lightbar LED sysfs directly**
  (`/sys/class/leds/*:rgb:indicator/multi_intensity`). In DualSense mode the
  lightbar *overrides* HHD's own solid-color RGB mode, so posting a color to
  HHD's API does **not** drive the rings — you have to write the lightbar.
- **Tell HHD to step aside.** HHD's solid RGB mode fights you (both write the
  lightbar → muddy blend). The engine POSTs HHD `rgb.mode = disabled` on start
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

## License

[MIT](LICENSE) © 2026 William James
