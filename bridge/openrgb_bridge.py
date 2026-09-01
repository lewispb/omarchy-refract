#!/usr/bin/env python3
"""Bridge between the rgb-sync Quickshell plugin and an OpenRGB SDK server.

Reads JSON commands from stdin (one per line), writes JSON events to stdout
(one per line). Talks the OpenRGB SDK binary protocol over TCP, so it can set
one color per LED — which is what gradients need — and read device state back,
so the plugin mirrors what the server reports rather than what was last sent.

Commands:
  {"op": "connect", "host": "127.0.0.1", "port": 6742}
  {"op": "refresh"}
  {"op": "apply", "anchors": ["RRGGBB", ...], "style": "gradient"|"solid",
   "exclude": ["device name", ...]}
  {"op": "apply_device", "device": 3, "anchors": [...], "style": "gradient"}
  {"op": "off", "device": 3}
  {"op": "quit"}

Events:
  {"event": "hello", "openrgbBinary": "/usr/bin/openrgb"}
  {"event": "state", "connected": true, "host": ..., "port": ..., "protocol": N,
   "devices": [{"index", "name", "type", "leds", "activeMode", "colors"}]}
  {"event": "result", "op": ..., "ok": true|false, "error": ...}

Only the Python standard library is used.
"""

import colorsys
import json
import select
import shutil
import socket
import struct
import sys
import time

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


class Reader:
    """Cursor over a controller-data blob."""

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def u8(self):
        v = self.data[self.pos]
        self.pos += 1
        return v

    def u16(self):
        v = struct.unpack_from("<H", self.data, self.pos)[0]
        self.pos += 2
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return v

    def i32(self):
        v = struct.unpack_from("<i", self.data, self.pos)[0]
        self.pos += 4
        return v

    def skip(self, n):
        self.pos += n

    def string(self):
        n = self.u16()
        raw = self.data[self.pos:self.pos + n]
        self.pos += n
        return raw.rstrip(b"\x00").decode("utf-8", "replace")


def parse_controller(data, protocol):
    r = Reader(data)
    r.u32()  # total size
    dev = {"type": r.i32()}
    dev["name"] = r.string()
    if protocol >= 1:
        r.string()  # vendor
    r.string()  # description
    r.string()  # fw version
    r.string()  # serial
    r.string()  # location
    num_modes = r.u16()
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
        r.skip(4 * n)  # mode colors
        modes.append(m)
    num_zones = r.u16()
    for _ in range(num_zones):
        r.string()  # zone name
        r.i32()  # zone type
        r.u32()  # leds min
        r.u32()  # leds max
        r.u32()  # leds count
        r.skip(r.u16())  # matrix map
    num_leds = r.u16()
    for _ in range(num_leds):
        r.string()  # led name
        r.u32()  # led value
    num_colors = r.u16()
    colors = []
    for _ in range(num_colors):
        raw = r.u32()
        colors.append("%02X%02X%02X" % (raw & 0xFF, (raw >> 8) & 0xFF, (raw >> 16) & 0xFF))
    dev["modes"] = modes
    dev["active_mode"] = active_mode
    dev["leds"] = num_leds
    dev["colors"] = colors
    return dev


# LEDs render mid-saturation screen colors as washed out, so hardware-bound
# colors get their saturation and value lifted. Screen surfaces (the panel,
# the bar) keep the exact theme colors.
def vivid(hexc):
    h, s, v = colorsys.rgb_to_hsv(int(hexc[0:2], 16) / 255,
                                  int(hexc[2:4], 16) / 255,
                                  int(hexc[4:6], 16) / 255)
    r, g, b = colorsys.hsv_to_rgb(h, min(1.0, s * 1.45), min(1.0, v * 1.1))
    return "%02X%02X%02X" % (round(r * 255), round(g * 255), round(b * 255))


def lerp_hex(a, b, t):
    def chan(i):
        av = int(a[i:i + 2], 16)
        bv = int(b[i:i + 2], 16)
        return round(av + (bv - av) * t)
    return "%02X%02X%02X" % (chan(0), chan(2), chan(4))


def gradient(anchors, count):
    """`count` colors interpolated through `anchors`, one per LED."""
    if not anchors:
        return []
    if len(anchors) == 1 or count == 1:
        return [anchors[0]] * count
    segs = len(anchors) - 1
    out = []
    for i in range(count):
        pos = i * segs / (count - 1)
        seg = min(int(pos), segs - 1)
        out.append(lerp_hex(anchors[seg], anchors[seg + 1], pos - seg))
    return out


class Bridge:
    def __init__(self):
        self.sock = None
        self.host = "127.0.0.1"
        self.port = 6742
        self.protocol = 0
        self.devices = []
        self.last_state_json = ""
        self.pending_list_update = False

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
        if header[:4] != b"ORGB":
            raise ConnectionError("bad packet magic")
        device_id, packet_id, size = struct.unpack("<III", header[4:])
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
            server = struct.unpack("<I", payload[:4])[0]
            self.protocol = min(CLIENT_PROTOCOL, server)
            name = b"omarchy-rgb-sync\x00"
            self.send_packet(0, SET_CLIENT_NAME, name)
            self.refresh(force=True)
            return True
        except OSError as e:
            self.disconnect()
            self.emit_state(error=str(e))
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
            count = struct.unpack("<I", payload[:4])[0]
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
        state = {
            "event": "state",
            "connected": self.sock is not None,
            "host": self.host,
            "port": self.port,
            "protocol": self.protocol,
            "devices": devices,
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
        only = msg.get("device", None)
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
                # A device with few LEDs gets the first two anchors rather
                # than the whole palette: four diodes showing four hues reads
                # as noise, not a gradient.
                use = anchors if d["leds"] >= 10 else anchors[:2]
                self.update_leds(d["index"], gradient(use, d["leds"]))
            result("apply", True)
            self.refresh()
        except (OSError, ConnectionError) as e:
            self.disconnect()
            self.emit_state(error="lost connection: %s" % e)
            result("apply", False, str(e))

    def off(self, index):
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

    # ---- Main loop

    def run(self):
        emit({"event": "hello", "openrgbBinary": shutil.which("openrgb") or ""})
        last_poll = 0.0
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
                line = sys.stdin.readline()
                if line == "":
                    return
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                op = msg.get("op", "")
                if op == "connect":
                    self.connect(str(msg.get("host", "127.0.0.1")),
                                 int(msg.get("port", 6742)))
                elif op == "refresh":
                    self.refresh(force=True)
                elif op == "apply" or op == "apply_device":
                    self.apply(msg)
                elif op == "off":
                    self.off(int(msg.get("device", -1)))
                elif op == "quit":
                    return
            now = time.monotonic()
            if self.sock and (self.pending_list_update or now - last_poll >= POLL_SECONDS):
                self.pending_list_update = False
                last_poll = now
                self.refresh()


if __name__ == "__main__":
    try:
        Bridge().run()
    except KeyboardInterrupt:
        pass
