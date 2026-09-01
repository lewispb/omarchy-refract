# omarchy-rgb-sync

Makes RGB hardware follow the current [Omarchy](https://omarchy.org) theme.
When you run `omarchy theme set`, every RGB device OpenRGB supports — mice,
keyboards, DRAM, GPUs, motherboards, Corsair iCUE Link fans and coolers — is
set to a gradient built from the theme's colors.

## How it works

Three pieces:

- **`rgb-sync`** — a script installed as an Omarchy `theme-set` hook (runs on
  every theme change) and `post-boot` hook (re-applies colors when the desktop
  starts). It reads color values from the active theme's `colors.toml`, builds
  a gradient through them, and applies it with the OpenRGB CLI.
- **`openrgb-server.service`** — a systemd user service running
  `openrgb --server`. Some devices, including Logitech wireless mice and
  Corsair iCUE Link hubs, revert to their onboard lighting profile a moment
  after host software disconnects. The server stays connected, so applied
  colors persist.
- **[OpenRGB](https://openrgb.org)** — does the device detection and control.
  Installed as a dependency.

For each device, the script picks a mode in this order:

1. **direct** — one color per LED; the device shows the gradient.
2. **custom** — same, for devices that name their per-LED mode "custom".
3. **static** — a single color; the device shows the first gradient color.

Devices with none of these modes are left unchanged. If a device has more
LEDs than gradient steps, OpenRGB applies the last color to the remaining
LEDs.

## Install

```bash
git clone https://github.com/lewispb/omarchy-rgb-sync.git
cd omarchy-rgb-sync
./install.sh
```

The installer adds the `openrgb` package if missing, enables the user
service, installs both hooks, and applies the current theme's colors.

## Configure

Optional. Create `~/.config/omarchy/rgb-sync.conf` (shell syntax):

```bash
STYLE=gradient                        # "solid" uses one color everywhere
ANCHORS="accent magenta cyan blue"    # colors.toml keys, in gradient order
STOPS=24                              # number of gradient steps generated
```

`ANCHORS` accepts any keys defined in a theme's `colors.toml`, such as
`accent`, `red`, `green`, `blue`, `cyan`, `magenta`, `yellow`, `foreground`.
Keys a theme does not define are skipped. If a theme defines none of them,
the script falls back to the theme's `keyboard.rgb` file, and exits without
changing anything if that is also absent.

Re-apply after editing:

```bash
~/.config/omarchy/hooks/theme-set.d/rgb-sync
```

## Device notes

- **Logitech wireless mice** work through the Lightspeed USB receiver with no
  extra setup; OpenRGB addresses the mouse's HID++ device directly.
- **Keyboards with onboard-only modes** (no direct mode) get the static
  fallback, so they show a single theme color rather than the gradient.
- **DRAM, motherboard, and GPU control** uses I2C/SMBus. The OpenRGB package
  installs the udev rules this needs; a reboot after first install helps if
  those devices are not detected.
- OpenRGB may log `iCUE LINK ADAPTER has 0 LEDs` for unpopulated hub ports.
  This is harmless.

## Uninstall

```bash
./uninstall.sh
```

Devices return to their onboard lighting on next reconnect or reboot.

## License

MIT
