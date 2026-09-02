<p align="center">
  <img src="assets/logo.svg" width="128" alt="Refract logo">
</p>

<h1 align="center">Refract</h1>

<p align="center">
  Multi-color theme sync for RGB hardware, in the <a href="https://omarchy.org">Omarchy</a> bar.
</p>

<p align="center">
  <img src="assets/panel.png" width="420" alt="The Refract panel: a gradient strip and every RGB device with its live colors">
</p>

Refract builds a gradient from the active Omarchy theme's palette — accent
through magenta, cyan, and blue by default — and spreads it across every LED
of every device OpenRGB supports: mice, keyboards, DRAM, GPUs, motherboards,
Corsair iCUE Link fans and coolers. Philips Hue lights join the same list
through the Hue bridge on your network. Switch themes and the hardware
switches with it, as a spectrum rather than a single color.

Dots in the bar show the gradient the current theme produces. The panel
behind them lists every device with its colors as the OpenRGB server and the
Hue bridge report them — changes made from the OpenRGB app, the Hue app, or
another client show up within a few seconds — plus a re-apply and a power
button per device, and a toggle for following the theme. Clicking a device's
color swatches starts its gradient from the next theme color; a one-LED lamp
steps through the anchors one at a time. The choice is saved per device and
kept across theme switches.

## Install

```bash
omarchy plugin add https://github.com/lewispb/omarchy-refract.git --enable
```

That is the whole setup. The widget lands in the bar's right section; move it
with `omarchy bar move io.github.lewispb.refract --section center`.

OpenRGB is the one dependency for RGB hardware (`sudo pacman -S openrgb`).
When the SDK server is not running, Refract starts `openrgb --server` itself
once per session — or connects to one you manage, such as a systemd user
service.

Philips Hue needs no extra software. Refract finds the bridge on the local
network and the panel shows a prompt to press the link button on it; the
lights appear a few seconds later. Details under [Philips Hue](#philips-hue).

## How it works

Three pieces, all inside the plugin:

- **A service** (`Service.qml`) owns the connection and watches the active
  theme's `colors.toml`. When the file changes — which is what
  `omarchy theme set` causes — the gradient is rebuilt from the configured
  anchor keys and re-applied to every device not switched off.
- **A Python bridge** (`bridge/openrgb_bridge.py`) speaks the OpenRGB SDK
  binary protocol over TCP, with no dependency beyond the standard library.
  It resamples the gradient to each device's exact LED count — a device with
  ten or more LEDs gets the full spectrum, a smaller one a blend of the first
  two anchors, since four diodes showing four hues reads as noise — and polls
  the server so the panel mirrors reality, not just the last command. A
  second backend (`bridge/hue.py`) does the same for a Philips Hue bridge
  over its HTTPS API, on its own thread.
- **A bar widget and panel** show the gradient and the devices, with
  re-apply, per-device power, and the theme-follow toggle.

## Settings

In the bar's widget settings (or `omarchy bar set io.github.lewispb.refract <key> <value>`):

| Key | Default | Meaning |
|-----|---------|---------|
| `themeSync` | `true` | Re-apply the gradient whenever the theme changes. |
| `style` | `gradient` | `solid` sends only the first anchor color. |
| `vivid` | `true` | LEDs render mid-saturation colors as washed out, so hardware colors are sent with lifted saturation and brightness. Turn off to send the exact theme colors. |
| `anchors` | `accent magenta cyan blue` | `colors.toml` keys the gradient passes through, in order. Keys a theme does not define are skipped. |
| `hue` | `true` | Find the Philips Hue bridge on the local network and set its color-capable lights. |
| `hueBridge` | `""` | IP address or hostname of the Hue bridge. Blank discovers it by mDNS, then through Signify's discovery service. |
| `hueRoom` | `""` | Sync only the lights in this room or zone, by the name shown in the Hue app. The panel's room picker sets this. Blank syncs every color-capable light. |
| `autoStartServer` | `true` | Start `openrgb --server` once per session if nothing answers. |
| `host` / `port` | `127.0.0.1` / `6742` | Where the OpenRGB SDK server listens. |

## Device notes

- **Logitech wireless mice** work through the Lightspeed USB receiver;
  OpenRGB addresses the mouse's HID++ device directly.
- **Devices without a per-LED mode** are set through their custom mode when
  the server offers one; a device with neither is left unchanged.
- **Devices OpenRGB names after another vendor** are listed under the name the
  kernel reports for them. OpenRGB names a device after the detector that
  matched its USB ids, and some ids are shared: a Lofree Flow84 reports
  Apple's `05AC:024F`, which the Keychron gaming keyboard detector claims, so
  OpenRGB calls it "Keychron Gaming Keyboard 1". Refract shows it as the
  kernel does, "Compx Flow84@Lofree".
- **Philips Hue** is handled directly, not through OpenRGB; see below. If
  you paired OpenRGB with the bridge earlier, turn off either OpenRGB's
  Philips Hue detector or Refract's `hue` setting so two clients do not set
  the same lights.
- **DRAM, motherboard, and GPU control** uses I2C/SMBus; the OpenRGB package
  installs the udev rules this needs.
- Some devices revert to their onboard lighting when no SDK client is
  connected. Refract's bridge stays connected while the shell runs, so
  applied colors persist.

## Philips Hue

Refract talks to the Hue bridge itself, over the bridge's local HTTPS API.
Nothing is installed and no account is involved.

1. With `hue` enabled (the default), the bridge is found by an mDNS query
   for `_hue._tcp`; if that gets no answer, Signify's discovery service is
   queried for the bridge on your public IP address. Set `hueBridge` to skip
   discovery.
2. The panel then reads "Hue Bridge found at 192.168.1.15. Press the link
   button on the bridge to pair." Press the round button on top of the
   bridge. Refract retries pairing every three seconds, so the lights appear
   within a few seconds of the press.
3. The application key the bridge issues is saved to
   `~/.local/state/refract/hue.json` (mode 0600), keyed by bridge id, so a
   bridge whose address changes keeps its pairing. Removing the entry in the
   Hue app (Settings → Apps) invalidates the key; the panel then shows the
   link-button prompt again.

What the lights receive:

- Only color-capable lights are listed. White and white-ambiance lights are
  left alone.
- The picker in the panel's Philips Hue header chooses a room or zone (or
  "All lights"); it writes the `hueRoom` setting. With a room set, only the
  lights in it are listed and synced; the rest of the house is not touched,
  and the gradient is spread across the room's lights alone. A name the
  bridge does not know is shown as "(not found)".
- A light with gradient support (Hue Play gradient lightstrip, Signe,
  Festavia) gets the full gradient across its points, usually five.
- Every other light is one point of a gradient spread across all such
  lights, in the order the bridge numbers them: with one light, the accent
  color; with several, a blend from accent into the second anchor.
- Only the color changes. Brightness stays as set in the Hue app.
- A theme switch leaves a light that is off alone, so changing themes at
  night does not turn the bedroom on. The panel's power button turns a light
  on with the theme color, or off; the Hue app's state shows in the panel
  within a few seconds either way.

## Uninstall

```bash
omarchy plugin remove io.github.lewispb.refract
```

Devices return to their onboard lighting on next reconnect or reboot. If
Refract started the OpenRGB server, it ends with the session; the `openrgb`
package stays installed (`sudo pacman -R openrgb` removes it). Hue lights
keep their last color. The pairing key stays in
`~/.local/state/refract/hue.json`; delete the file, and remove the "refract"
entry under Settings → Apps in the Hue app, to revoke it.

## Credits

- [OmaRGB](https://github.com/ilkaydnc/omargb) by Ilkay Dinc set the
  architecture this plugin follows — a shell service owning a Python bridge
  to the OpenRGB SDK, with the panel mirroring server state instead of only
  sending to it. Refract exists because OmaRGB syncs one accent color and I
  wanted the whole palette; if you want per-device mode, brightness, and
  speed controls or stealth mode, use OmaRGB (running both at once means two
  clients writing to the same devices).
- [OpenRGB](https://openrgb.org) does the actual device support — hundreds of
  controllers behind one SDK.
- [Omarchy](https://omarchy.org)'s plugin system hosts the whole thing:
  service, widget, and panel run inside the shell with no extra process
  beyond the bridge.

## License

MIT
