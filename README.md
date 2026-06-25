# Flicker

**Real-time ambient lighting for the ASUS ROG Ally.**

Flicker samples whatever's on screen, finds the dominant vivid color, and drives
the controller's **RGB joystick rings** to match — in games and on the desktop.
Red boss room → red rings. Forest → green. Fire → orange. ~20 fps, with the
capture + downscale done on the GPU so it's nearly free.

Three modes:

- **Unified** — one color from the whole screen → both rings
- **Split** — left half → left ring, right half → right ring
- **Quad** — each screen corner → the matching ring half (4 zones; each ring
  shows two colors)

Ships two ways: a **Decky Loader plugin** (mode selector + sliders in Game Mode)
and a **standalone systemd service**.

> 📸 *Add a short clip/photo of the rings reacting to a game here.*

---

## How it works

```
ffmpeg kmsgrab (DRM scanout)
  → VAAPI GPU detile + downscale to 48×48
  → per region: dominant-hue pick  (saturation² × value weighted histogram)
  → vivid-normalize                (scale dominant channel up, boost saturation)
  → write the Aura RGB zones over HID
```

The screen is captured from the DRM scanout (works under gamescope / Game Mode),
detiled and shrunk on the GPU, then — per region — reduced to a single
**prominent** color. A plain average comes out a muddy grey, so instead it bins
pixels by hue weighted by `saturation² × value` and locks onto the most colorful
region. Each color is normalized to a bright, saturated value and written to the
matching **Aura RGB zone** on the controller's MCU over HID.

Two details that make it robust:

- A **reader thread** drains the capture pipe at full rate (keeping only the
  latest frame) so the LED writes can never stall the capture.
- Flicker tells **Handheld Daemon to release the LEDs** while it runs, so it's
  the sole writer of the RGB MCU, then hands them back when it stops.

## Install — Decky plugin (recommended)

Once it's in the [Decky](https://decky.xyz/) store, install it from the
in-Game-Mode store. To build and sideload it now:

```bash
git clone https://github.com/wjames111/flicker.git
cd flicker
pnpm i && pnpm build          # needs Node 18+ and pnpm v9
PLUGIN=~/homebrew/plugins/flicker
sudo mkdir -p "$PLUGIN"
sudo cp -r plugin.json package.json main.py flicker.py dist "$PLUGIN"/
sudo systemctl restart plugin_loader
```

Then open the **Decky** menu in Game Mode → **Flicker**, turn it on, and pick a
**Mode** (Unified / Split / Quad). The panel also has live **Vividness**,
**Reactivity**, and **Brightness** sliders.

## Install — standalone (no Decky)

```bash
git clone https://github.com/wjames111/flicker.git
cd flicker
./install.sh                  # installs to /opt, enables a systemd service
```

```bash
sudo systemctl stop  flicker     # off → your normal HHD RGB is restored automatically
sudo systemctl start flicker     # on
journalctl -u flicker -f         # logs
```

## Requirements

- A **ROG Ally / Ally X** running **Handheld Daemon** (HHD) — Flicker drives the
  Asus Aura RGB MCU directly, and asks HHD to release the LEDs while it runs.
- `ffmpeg` with `kmsgrab` + **VAAPI** (AMD APU — the Ally's default).
- `python3` with `numpy` and `hid` (hidapi).
- Runs as **root** (kmsgrab and the RGB hidraw both require it). The Decky plugin
  declares the `_root` flag; the standalone runs as a root systemd service.

On Bazzite these are generally already present.

## Configuration (standalone)

Set these env vars in the `[Service]` block of
`/etc/systemd/system/flicker.service`, then `daemon-reload` + restart:

| Variable            | Default          | Meaning                                   |
|---------------------|------------------|-------------------------------------------|
| `FLICKER_MODE`      | `unified`        | `unified` / `split` / `quad`              |
| `FLICKER_SAT_BOOST` | `1.5`            | Saturation multiplier (vividness)         |
| `FLICKER_EMA`       | `0.25`           | Smoothing — lower = smoother/slower       |
| `FLICKER_NORM_MAX`  | `210`            | Brightness of the dominant channel        |
| `FLICKER_FPS`       | `20`             | Capture / update rate                     |
| `FLICKER_GRID`      | `48`             | Downscale grid size                       |
| `FLICKER_CARD`      | `/dev/dri/card1` | DRM device for capture                    |
| `FLICKER_HIDRAW`    | *(auto)*         | RGB hidraw path, if auto-detect picks wrong |

(The Decky plugin exposes Mode + Vividness / Reactivity / Brightness live.)

## Notes / gotchas

Things that took real debugging — useful if you adapt this to another device:

- **Per-ring color needs the Aura MCU, not the lightbar.** In DualSense mode the
  rings mirror the emulated DualSense *lightbar*, which is a single color — so
  independent left/right (and the four quad zones) only work by writing the
  Asus Aura RGB MCU directly over HID (auto-detected: ASUS `0x0B05`, usage_page
  `0xFF31`, usage `0x0080`). Zones: `0x01` left-bottom, `0x02` left-top, `0x03`
  right-bottom, `0x04` right-top, `0x00` all.
- **Tell HHD to step aside.** HHD also drives the RGB, so Flicker POSTs HHD
  `rgb.mode = disabled` on start (becomes sole writer) and `= solid` on stop.
- **Dark colors render as muddy teal** (a hardware color cast at low PWM);
  bright/saturated colors render true. That's why every color is
  vivid-normalized before it's sent — never a dim value.
- **Decouple capture from output.** The per-frame LED writes are slow enough
  that, done inline, they back the capture pipe up until ffmpeg broken-pipes and
  dies. A dedicated reader thread drains the pipe so this can't happen.
- The RGB **hidraw number isn't hardcoded** — it's matched by USB id + HID usage,
  since `/dev/hidrawN` can change across reboots.

## Compatibility

Developed and tested on a **ROG Ally X** running **Bazzite** (Fedora 43) with
**Handheld Daemon**. The original **ROG Ally** uses the same Aura protocol and
should work. Other handhelds (Legion Go, AYANEO, …) have different RGB
controllers and aren't supported by the per-zone MCU path.

## License

[MIT](LICENSE) © 2026 William James
