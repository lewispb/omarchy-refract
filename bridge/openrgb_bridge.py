#!/usr/bin/env python3
"""Bridge between the Refract Quickshell plugin and the hardware backends.

Reads JSON commands from stdin (one per line), writes JSON events to stdout
(one per line). Two backends sit behind one device list:

- OpenRGB: the SDK binary protocol over TCP, so it can set one color per
  LED — which is what gradients need — and read device state back, so the
  plugin mirrors what the server reports rather than what was last sent.
- Philips Hue (hue.py): the bridge's CLIP v2 API over HTTPS, on its own
  thread. Hue devices carry string indexes prefixed "hue:".

Commands:
  {"op": "connect", "host": "127.0.0.1", "port": 6742}
  {"op": "hue", "enabled": true, "address": "", "room": ""}
  {"op": "refresh"}
  {"op": "apply", "anchors": ["RRGGBB", ...], "style": "gradient"|"solid",
   "vivid": true, "exclude": ["device name", ...],
   "offsets": {"device name": N, ...}}
  {"op": "apply", "device": 3 | "hue:<id>", "anchors": [...], "style": ...}
  {"op": "off", "device": 3 | "hue:<id>"}
  {"op": "start_server"}
  {"op": "quit"}

Events:
  {"event": "hello", "openrgbBinary": "/usr/bin/openrgb"}
  {"event": "state", "connected": true, "host": ..., "port": ..., "protocol": N,
   "hue": {"status": ..., "message": ..., "address": ..., "bridge": ...,
           "room": ..., "rooms": [...]},
   "devices": [{"index", "name", "type", "leds", "activeMode", "colors",
                "detail"?}]}
  {"event": "result", "op": ..., "ok": true|false, "error": ...}
  {"event": "result", "op": "start_server", "ok": true, "started": true|false,
   "reason": ...}

`connected` describes the OpenRGB server; the Hue bridge reports through
`hue.status`. Only the Python standard library is used.
"""

import json
import os
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

from hue import INDEX_PREFIX as HUE_PREFIX, Hue
from palette import spread, vivid

# Cap the negotiated protocol at 3: the controller-data layout this file
# parses stops at protocol 3 fields, and servers accept older clients.
CLIENT_PROTOCOL = 3

# NetworkProtocol.h packet ids.
REQUEST_CONTROLLER_COUNT = 0
REQUEST_CONTROLLER_DATA = 1
REQUEST_PROTOCOL_VERSION = 40
SET_CLIENT_NAME = 50
DEVICE_LIST_UPDATED = 100
RGBCONTROLLER_UPDATELEDS = 1050
RGBCONTROLLER_SETCUSTOMMODE = 1100

POLL_SECONDS = 3.0

# Limits on what the server may send. The SDK server is normally local and
# trusted, but the host and port are settings, so every size and count read
# from the wire is checked before it drives an allocation or a loop. A
# violation raises ProtocolError, which disconnects with the reason shown in
# the panel. The caps are far above real hardware: the largest controllers
# have a few thousand LEDs and blobs under 100 KB.
MAX_PACKET_BYTES = 1 << 20
MAX_CONTROLLERS = 64
MAX_MODES = 128
MAX_ZONES = 128
MAX_LEDS = 8192
MAX_MODE_COLORS = 256
MAX_STRING_BYTES = 512
MAX_NAME_CHARS = 128

DEVICE_TYPES = [
    "Motherboard", "DRAM", "GPU", "Cooler", "LED Strip", "Keyboard",
    "Mouse", "Mouse Mat", "Headset", "Headset Stand", "Gamepad", "Light",
    "Speaker", "Virtual", "Storage", "Case", "Microphone", "Accessory",
    "Keypad",
]


def emit(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def result(op, ok, error=""):
    msg = {"event": "result", "op": op, "ok": ok}
    if error:
        msg["error"] = error
    emit(msg)


class ProtocolError(ConnectionError):
    """The server sent something outside the protocol or the limits above."""


def check(condition, what):
    if not condition:
        raise ProtocolError("server sent %s" % what)


class Reader:
    """Cursor over a controller-data blob. Every read is bounds-checked."""

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def take(self, n):
        check(0 <= n <= len(self.data) - self.pos, "a truncated controller blob")
        start = self.pos
        self.pos += n
        return self.data[start:self.pos]

    def u16(self):
        return struct.unpack("<H", self.take(2))[0]

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def i32(self):
        return struct.unpack("<i", self.take(4))[0]

    def skip(self, n):
        self.take(n)

    def string(self):
        n = self.u16()
        check(n <= MAX_STRING_BYTES, "an oversized string")
        return self.take(n).rstrip(b"\x00").decode("utf-8", "replace")[:MAX_NAME_CHARS]


def hid_name(location):
    """The kernel's name for the device behind an OpenRGB HID location.

    OpenRGB names a device after the detector that matched its USB ids, and
    some vendors share ids: a Lofree Flow84 reports Apple's 05AC:024F, which
    the Keychron gaming keyboard detector claims, so OpenRGB reports it as
    "Keychron Gaming Keyboard 1". The kernel's HID name comes from the
    device's own USB descriptor strings.
    """
    m = re.search(r"/dev/(hidraw\d+)", location or "")
    if not m:
        return ""
    try:
        with open("/sys/class/hidraw/%s/device/uevent" % m.group(1)) as f:
            for line in f:
                if line.startswith("HID_NAME="):
                    return line[len("HID_NAME="):].strip()
    except OSError:
        pass
    return ""


def display_name(openrgb_name, location):
    """OpenRGB's name, unless it shares no word with what the kernel reports.

    A shared word ("Logitech", "Corsair", a model number) means the detector
    matched the actual product and OpenRGB's tidier name is kept. No shared
    word means the detector matched a USB id another vendor reuses, and the
    kernel's name is the accurate one.
    """
    kernel = hid_name(location)
    if not kernel:
        return openrgb_name
    words = lambda text: [w for w in re.findall(r"[A-Za-z0-9]+", text.lower()) if len(w) >= 3]
    # A prefix counts as shared: "ASUS" against the kernel's "AsusTek".
    for a in words(openrgb_name):
        for b in words(kernel):
            if a.startswith(b) or b.startswith(a):
                return openrgb_name
    return kernel


def parse_controller(data, protocol):
    r = Reader(data)
    check(r.u32() <= len(data), "a controller blob with a size past its data")
    dev = {"type": r.i32()}
    dev["name"] = r.string()
    if protocol >= 1:
        r.string()  # vendor
    r.string()  # description
    r.string()  # fw version
    r.string()  # serial
    dev["name"] = display_name(dev["name"], r.string())  # location
    num_modes = r.u16()
    check(num_modes <= MAX_MODES, "too many modes")
    active_mode = r.i32()
    modes = []
    for _ in range(num_modes):
        m = {"name": r.string(), "value": r.i32(), "flags": r.u32()}
        r.u32()  # speed min
        r.u32()  # speed max
        if protocol >= 3:
            r.u32()  # brightness min
            r.u32()  # brightness max
        r.u32()  # colors min
        r.u32()  # colors max
        r.u32()  # speed
        if protocol >= 3:
            r.u32()  # brightness
        r.u32()  # direction
        r.u32()  # color mode
        n = r.u16()
        check(n <= MAX_MODE_COLORS, "too many mode colors")
        r.skip(4 * n)  # mode colors
        modes.append(m)
    num_zones = r.u16()
    check(num_zones <= MAX_ZONES, "too many zones")
    for _ in range(num_zones):
        r.string()  # zone name
        r.i32()  # zone type
        r.u32()  # leds min
        r.u32()  # leds max
        r.u32()  # leds count
        r.skip(r.u16())  # matrix map
    num_leds = r.u16()
    check(num_leds <= MAX_LEDS, "too many LEDs")
    for _ in range(num_leds):
        r.string()  # led name
        r.u32()  # led value
    num_colors = r.u16()
    check(num_colors <= MAX_LEDS, "too many colors")
    colors = []
    for _ in range(num_colors):
        raw = r.u32()
        colors.append("%02X%02X%02X" % (raw & 0xFF, (raw >> 8) & 0xFF, (raw >> 16) & 0xFF))
    dev["modes"] = modes
    dev["active_mode"] = active_mode
    dev["leds"] = num_leds
    dev["colors"] = colors
    return dev


def is_hue(index):
    return isinstance(index, str) and index.startswith(HUE_PREFIX)


def openrgb_running():
    """True if any process on this machine is the openrgb binary."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open("/proc/%s/comm" % pid) as f:
                if f.read().strip() == "openrgb":
                    return True
        except OSError:
            continue
    return False


class Bridge:
    def __init__(self):
        self.sock = None
        self.host = "127.0.0.1"
        self.port = 6742
        self.protocol = 0
        self.devices = []
        self.last_state_json = ""
        self.pending_list_update = False
        self.hue_changed = threading.Event()
        self.hue = Hue(notify=self.hue_changed.set)

    # ---- Socket protocol

    def send_packet(self, device_id, packet_id, payload=b""):
        header = b"ORGB" + struct.pack("<III", device_id, packet_id, len(payload))
        self.sock.sendall(header + payload)

    def recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("server closed the connection")
            buf += chunk
        return buf

    def recv_packet(self):
        header = self.recv_exact(16)
        check(header[:4] == b"ORGB", "a packet without the ORGB magic")
        device_id, packet_id, size = struct.unpack("<III", header[4:])
        check(size <= MAX_PACKET_BYTES, "a %d byte packet" % size)
        payload = self.recv_exact(size) if size else b""
        return device_id, packet_id, payload

    def request(self, device_id, packet_id, payload=b""):
        """Send a request and wait for the reply with the same packet id.

        The server can interleave DEVICE_LIST_UPDATED notifications; those are
        flagged for the main loop rather than treated as the reply.
        """
        self.send_packet(device_id, packet_id, payload)
        while True:
            rdev, rid, rpayload = self.recv_packet()
            if rid == DEVICE_LIST_UPDATED:
                self.pending_list_update = True
                continue
            if rid == packet_id:
                return rdev, rpayload

    # ---- Operations

    def connect(self, host, port):
        self.disconnect()
        self.host = host
        self.port = port
        try:
            self.sock = socket.create_connection((host, port), timeout=5)
            self.sock.settimeout(10)
            _, payload = self.request(0, REQUEST_PROTOCOL_VERSION,
                                      struct.pack("<I", CLIENT_PROTOCOL))
            check(len(payload) >= 4, "a short protocol version reply")
            server = struct.unpack("<I", payload[:4])[0]
            self.protocol = min(CLIENT_PROTOCOL, server)
            name = b"omarchy-rgb-sync\x00"
            self.send_packet(0, SET_CLIENT_NAME, name)
            self.refresh(force=True)
            return True
        except OSError as e:
            self.disconnect()
            # Forced: the shell's reconnect timer runs from this event, and
            # a repeat of the same error would otherwise be deduplicated.
            self.emit_state(error=str(e), force=True)
            return False

    def disconnect(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.devices = []

    def refresh(self, force=False):
        if not self.sock:
            self.emit_state(error="not connected")
            return
        try:
            _, payload = self.request(0, REQUEST_CONTROLLER_COUNT)
            check(len(payload) >= 4, "a short controller count reply")
            count = struct.unpack("<I", payload[:4])[0]
            check(count <= MAX_CONTROLLERS, "%d controllers" % count)
            devices = []
            for i in range(count):
                _, blob = self.request(i, REQUEST_CONTROLLER_DATA,
                                       struct.pack("<I", self.protocol))
                dev = parse_controller(blob, self.protocol)
                dev["index"] = i
                devices.append(dev)
            self.devices = devices
            self.emit_state(force=force)
        except (OSError, ConnectionError, struct.error) as e:
            self.disconnect()
            self.emit_state(error="lost connection: %s" % e)

    def emit_state(self, error="", force=False):
        devices = []
        for d in self.devices:
            modes = d["modes"]
            active = d["active_mode"]
            colors = d["colors"]
            # Up to 12 swatches per device keeps the event small; the panel
            # shows a strip, not every LED of a 100-key keyboard.
            step = max(1, len(colors) // 12)
            devices.append({
                "index": d["index"],
                "name": d["name"],
                "type": DEVICE_TYPES[d["type"]] if 0 <= d["type"] < len(DEVICE_TYPES) else "Unknown",
                "leds": d["leds"],
                "activeMode": modes[active]["name"] if 0 <= active < len(modes) else "",
                "colors": colors[::step][:12],
            })
        hue_info, hue_devices = self.hue.snapshot()
        state = {
            "event": "state",
            "connected": self.sock is not None,
            "host": self.host,
            "port": self.port,
            "protocol": self.protocol,
            "hue": hue_info,
            "devices": devices + hue_devices,
        }
        if error:
            state["error"] = error
        encoded = json.dumps(state, separators=(",", ":"), sort_keys=True)
        if force or encoded != self.last_state_json:
            self.last_state_json = encoded
            sys.stdout.write(encoded + "\n")
            sys.stdout.flush()

    def update_leds(self, index, hex_colors):
        colors = b"".join(
            struct.pack("<BBBB", int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 0)
            for h in hex_colors
        )
        body = struct.pack("<H", len(hex_colors)) + colors
        payload = struct.pack("<I", 4 + len(body)) + body
        self.send_packet(index, RGBCONTROLLER_SETCUSTOMMODE)
        self.send_packet(index, RGBCONTROLLER_UPDATELEDS, payload)

    def apply(self, msg):
        anchors = [a for a in msg.get("anchors", [])
                   if isinstance(a, str) and len(a) == 6]
        if not anchors:
            result("apply", False, "no colors to apply")
            return
        if msg.get("style") == "solid":
            anchors = anchors[:1]
        if msg.get("vivid", True):
            anchors = [vivid(a) for a in anchors]
        exclude = set(msg.get("exclude", []))
        offsets = msg.get("offsets", {}) if isinstance(msg.get("offsets"), dict) else {}
        only = msg.get("device", None)
        if only is None or is_hue(only):
            self.hue.apply(anchors, only, exclude, offsets)
        if is_hue(only):
            result("apply", True)
            return
        if not self.sock:
            result("apply", False, "not connected")
            return
        try:
            for d in self.devices:
                if only is not None and d["index"] != only:
                    continue
                if only is None and d["name"] in exclude:
                    continue
                if d["leds"] == 0:
                    continue
                self.update_leds(d["index"], spread(anchors, d["leds"], offsets.get(d["name"], 0)))
            result("apply", True)
            self.refresh()
        except (OSError, ConnectionError) as e:
            self.disconnect()
            self.emit_state(error="lost connection: %s" % e)
            result("apply", False, str(e))

    def off(self, index):
        if is_hue(index):
            self.hue.off(index)
            result("off", True)
            return
        if not self.sock:
            result("off", False, "not connected")
            return
        try:
            for d in self.devices:
                if d["index"] == index and d["leds"] > 0:
                    self.update_leds(index, ["000000"] * d["leds"])
            result("off", True)
            self.refresh()
        except (OSError, ConnectionError) as e:
            self.disconnect()
            self.emit_state(error="lost connection: %s" % e)
            result("off", False, str(e))

    # ---- Server start

    def start_server(self):
        """Spawn `openrgb --server` unless an openrgb process already exists.

        The server accepts clients only after device detection finishes, so a
        connect that fails right after login does not mean no server is
        starting. A second server detects the same hardware at once and the
        second to bind the port exits with a failure.
        """
        if openrgb_running():
            emit({"event": "result", "op": "start_server", "ok": True,
                  "started": False, "reason": "an openrgb process is already running"})
            return
        binary = shutil.which("openrgb")
        if not binary:
            emit({"event": "result", "op": "start_server", "ok": False,
                  "started": False, "reason": "openrgb is not installed"})
            return
        try:
            subprocess.Popen([binary, "--server"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            emit({"event": "result", "op": "start_server", "ok": True, "started": True})
        except OSError as e:
            emit({"event": "result", "op": "start_server", "ok": False,
                  "started": False, "reason": str(e)})

    # ---- Main loop

    def run(self):
        emit({"event": "hello", "openrgbBinary": shutil.which("openrgb") or ""})
        self.hue.start()
        last_poll = 0.0
        inbuf = b""
        while True:
            rlist = [sys.stdin]
            if self.sock:
                rlist.append(self.sock)
            try:
                ready, _, _ = select.select(rlist, [], [], 1.0)
            except (OSError, ValueError):
                ready = []
            if self.sock in ready:
                # Unsolicited traffic outside a request is a notification.
                try:
                    _, pid, _ = self.recv_packet()
                    if pid == DEVICE_LIST_UPDATED:
                        self.pending_list_update = True
                except (OSError, ConnectionError):
                    self.disconnect()
                    self.emit_state(error="lost connection")
            if sys.stdin in ready:
                # Raw reads, not readline: two commands written together
                # arrive in one chunk, and a line left in a buffered reader
                # would wait for the next write before select reports it.
                chunk = os.read(sys.stdin.fileno(), 65536)
                if chunk == b"":
                    self.hue.stop()
                    return
                inbuf += chunk
                while b"\n" in inbuf:
                    line, inbuf = inbuf.split(b"\n", 1)
                    self.handle_command(line)
            now = time.monotonic()
            if self.sock and (self.pending_list_update or now - last_poll >= POLL_SECONDS):
                self.pending_list_update = False
                last_poll = now
                self.refresh()
            if self.hue_changed.is_set():
                self.hue_changed.clear()
                self.emit_state()

    def handle_command(self, line):
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        op = msg.get("op", "")
        if op == "connect":
            self.connect(str(msg.get("host", "127.0.0.1")), int(msg.get("port", 6742)))
        elif op == "hue":
            self.hue.configure(msg.get("enabled", True), msg.get("address", ""), msg.get("room", ""))
        elif op == "refresh":
            self.hue.refresh()
            self.refresh(force=True)
        elif op == "apply" or op == "apply_device":
            self.apply(msg)
        elif op == "off":
            device = msg.get("device", -1)
            self.off(device if is_hue(device) else int(device))
        elif op == "start_server":
            self.start_server()
        elif op == "quit":
            self.hue.stop()
            raise SystemExit(0)


if __name__ == "__main__":
    try:
        Bridge().run()
    except KeyboardInterrupt:
        pass
