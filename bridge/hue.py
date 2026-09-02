"""Philips Hue backend for the Refract bridge.

Talks to a Hue bridge on the local network over its CLIP v2 HTTPS API, with
nothing beyond the standard library. Runs on its own thread so a slow or
absent bridge never blocks the OpenRGB side or the command stream.

States, reported in the `hue` field of every state event:

  disabled     the `hue` setting is off
  searching    no bridge found yet; discovery runs every 30 seconds
  unpaired     a bridge answered but has not issued this machine a key;
               pairing is attempted every 3 seconds until the link button on
               the bridge is pressed
  connected    lights are listed as devices and polled every 3 seconds
  unreachable  a paired bridge is not answering

Discovery order: the configured address, then the address saved from a
previous pairing, then an mDNS query for _hue._tcp, then Signify's discovery
service. Keys are saved per bridge id in hue.json under the state directory,
so a bridge that changes address keeps its pairing.

Only color-capable lights are listed, and only those in the configured room
or zone when one is named. A light with `gradient` support (Hue Play gradient
lightstrip, Signe, Festavia) gets the full gradient across its points; every
other light is one point of a gradient spread across all listed such lights,
in the order the bridge numbers them.
"""

import http.client
import json
import os
import queue
import socket
import ssl
import struct
import threading
import time
import urllib.request

from palette import gradient, hex_to_xy, rotate, spread, xy_to_hex

POLL_SECONDS = 3.0
PAIR_SECONDS = 3.0
DISCOVER_SECONDS = 30.0
# Room and zone membership is re-read this often, so a light moved into the
# configured room in the Hue app is picked up without a restart.
GROUPS_SECONDS = 30.0
INDEX_PREFIX = "hue:"

# Application name and device name, both within the bridge's length limits.
DEVICE_TYPE = "refract#%s" % (socket.gethostname()[:19] or "omarchy")


def state_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "refract", "hue.json")


def mdns_discover(timeout=2.0):
    """Addresses of hosts answering an mDNS query for _hue._tcp.

    The bridge answers by multicast, so the socket joins the group and shares
    port 5353 with any mDNS daemon already listening.
    """
    labels = b"_hue._tcp.local".split(b".")
    name = b"".join(struct.pack("B", len(p)) + p for p in labels) + b"\x00"
    packet = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + name + struct.pack(">HH", 12, 1)
    found = []
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", 5353))
        group = struct.pack("4s4s", socket.inet_aton("224.0.0.251"), socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, group)
        sock.settimeout(0.5)
        sock.sendto(packet, ("224.0.0.251", 5353))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 12:
                continue
            is_answer = struct.unpack(">H", data[2:4])[0] & 0x8000
            if is_answer and b"_hue" in data and addr[0] not in found:
                found.append(addr[0])
    except OSError:
        pass
    finally:
        if sock:
            sock.close()
    return found


def cloud_discover(timeout=4.0):
    """Addresses Signify's discovery service lists for this public IP."""
    try:
        with urllib.request.urlopen("https://discovery.meethue.com/", timeout=timeout) as resp:
            entries = json.loads(resp.read().decode("utf-8", "replace"))
        return [e["internalipaddress"] for e in entries if isinstance(e, dict) and e.get("internalipaddress")]
    except (OSError, ValueError, KeyError):
        return []


class HueError(Exception):
    pass


class KeyRejected(HueError):
    """The bridge no longer accepts the saved application key."""


class Api:
    """HTTPS calls to one bridge.

    The bridge's certificate is issued by Signify's private root for the
    bridge id, not a public CA, so verification is off; every integration on
    the local network does the same.
    """

    def __init__(self, address, key=None):
        self.address = address
        self.key = key
        self.context = ssl.create_default_context()
        self.context.check_hostname = False
        self.context.verify_mode = ssl.CERT_NONE

    def request(self, method, path, body=None, timeout=4.0):
        headers = {"Accept": "application/json"}
        if self.key:
            headers["hue-application-key"] = self.key
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPSConnection(self.address, 443, timeout=timeout, context=self.context)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        finally:
            conn.close()
        try:
            parsed = json.loads(raw.decode("utf-8", "replace")) if raw else None
        except ValueError:
            parsed = None
        return resp.status, parsed

    def config(self):
        status, parsed = self.request("GET", "/api/0/config")
        if status != 200 or not isinstance(parsed, dict) or "bridgeid" not in parsed:
            raise HueError("no Hue bridge at %s" % self.address)
        return parsed

    def pair(self):
        """The application key, or None while the link button is unpressed."""
        status, parsed = self.request("POST", "/api", {"devicetype": DEVICE_TYPE, "generateclientkey": True})
        if not isinstance(parsed, list) or not parsed:
            raise HueError("unexpected pairing reply (HTTP %s)" % status)
        entry = parsed[0]
        if "success" in entry:
            return entry["success"].get("username")
        error = entry.get("error", {})
        if error.get("type") == 101:
            return None
        raise HueError(error.get("description") or "pairing failed")

    def resource(self, kind):
        status, parsed = self.request("GET", "/clip/v2/resource/" + kind)
        if status in (401, 403):
            raise KeyRejected("The bridge no longer accepts this key")
        if status != 200 or not isinstance(parsed, dict):
            raise HueError("HTTP %s reading %s" % (status, kind))
        return parsed.get("data", [])

    def put_light(self, light_id, body):
        status, parsed = self.request("PUT", "/clip/v2/resource/light/" + light_id, body)
        if status in (401, 403):
            raise KeyRejected("The bridge no longer accepts this key")
        errors = parsed.get("errors", []) if isinstance(parsed, dict) else []
        if status >= 400 or errors:
            detail = "; ".join(e.get("description", "") for e in errors if isinstance(e, dict))
            raise HueError(detail or "HTTP %s" % status)


class Hue(threading.Thread):
    """Owns the bridge connection; `notify` is called whenever state changes."""

    def __init__(self, notify, path=None):
        super().__init__(name="hue", daemon=True)
        self.notify = notify
        self.path = path or state_path()
        self.commands = queue.Queue()
        self.lock = threading.Lock()
        self.enabled = False
        self.configured = ""
        self.room = ""
        self.groups = []
        self.groups_at = 0.0
        self.api = None
        self.bridge_id = ""
        self.bridge_name = ""
        self.status = "disabled"
        self.message = ""
        self.lights = []
        self.products = {}
        self.failures = 0
        self.saved = self.load()
        self.next_discover = 0.0
        self.next_pair = 0.0
        self.next_poll = 0.0

    # ---- Calls from the main thread

    def configure(self, enabled, address, room=""):
        self.commands.put(("configure", bool(enabled), str(address or "").strip(), str(room or "").strip()))

    def refresh(self):
        self.commands.put(("refresh",))

    def apply(self, anchors, only=None, exclude=(), offsets=None):
        self.commands.put(("apply", list(anchors), only, set(exclude), dict(offsets or {})))

    def off(self, index):
        self.commands.put(("off", index))

    def stop(self):
        self.commands.put(("stop",))

    def snapshot(self):
        """(status dict, device list) as the state event reports them."""
        with self.lock:
            info = {"status": self.status, "message": self.message, "room": self.room,
                    "rooms": sorted({g["name"] for g in self.groups if g["name"]}, key=str.casefold)}
            if self.api:
                info["address"] = self.api.address
            if self.bridge_name:
                info["bridge"] = self.bridge_name
            return info, [self.public(l) for l in self.lights]

    @staticmethod
    def public(light):
        if light["points"] >= 2:
            detail = "Philips Hue · gradient, %d points" % light["points"]
        else:
            detail = "Philips Hue · " + (light["product"] or "color light")
        if not light["on"]:
            colors = ["000000"]
        elif light["gradient"]:
            colors = [xy_to_hex(x, y) for x, y in light["gradient"]]
        elif light["xy"]:
            colors = [xy_to_hex(*light["xy"])]
        else:
            colors = ["FFFFFF"]
        return {
            "index": INDEX_PREFIX + light["id"],
            "name": light["name"],
            "type": "Hue",
            "leds": max(1, light["points"]),
            "activeMode": "on" if light["on"] else "off",
            "detail": detail,
            "colors": colors,
        }

    # ---- Saved pairings

    def load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data.get("bridges", {}) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"bridges": self.saved}, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ---- State

    def set_status(self, status, message=""):
        with self.lock:
            changed = (status, message) != (self.status, self.message)
            self.status = status
            self.message = message
        if changed:
            self.notify()

    def set_lights(self, lights):
        with self.lock:
            self.lights = lights
        self.notify()

    def reset(self, status, message=""):
        self.api = None
        self.bridge_id = ""
        self.bridge_name = ""
        self.failures = 0
        with self.lock:
            self.groups = []
        self.groups_at = 0.0
        self.set_lights([])
        self.set_status(status, message)

    # ---- Thread loop

    def run(self):
        while True:
            try:
                cmd = self.commands.get(timeout=1.0)
            except queue.Empty:
                cmd = None
            if cmd:
                if cmd[0] == "stop":
                    return
                try:
                    self.handle(cmd)
                except KeyRejected as e:
                    self.forget_key(str(e))
                except (OSError, HueError, ValueError) as e:
                    self.set_status("unreachable", "Hue bridge at %s is not answering: %s" % (self.api.address if self.api else "?", e))
            if not self.enabled:
                continue
            now = time.monotonic()
            try:
                if not self.api:
                    if now >= self.next_discover:
                        self.next_discover = now + DISCOVER_SECONDS
                        self.discover()
                elif not self.api.key:
                    if now >= self.next_pair:
                        self.next_pair = now + PAIR_SECONDS
                        self.try_pair()
                elif now >= self.next_poll:
                    self.next_poll = now + POLL_SECONDS
                    self.poll()
            except KeyRejected as e:
                self.forget_key(str(e))
            except (OSError, HueError, ValueError) as e:
                self.lost(e)

    def handle(self, cmd):
        if cmd[0] == "configure":
            _, enabled, address, room = cmd
            if room != self.room:
                # A room change re-filters the same bridge's lights; the
                # connection and pairing stay as they are.
                self.room = room
                self.groups_at = 0.0
                self.next_poll = 0.0
                if self.api and self.api.key:
                    self.poll()
            if (enabled, address) == (self.enabled, self.configured):
                return
            self.enabled = enabled
            self.configured = address
            self.next_discover = 0.0
            if enabled:
                self.reset("searching", self.search_message())
            else:
                self.reset("disabled")
        elif cmd[0] == "refresh":
            if self.api and self.api.key:
                self.poll()
        elif cmd[0] == "apply":
            _, anchors, only, exclude, offsets = cmd
            if self.api and self.api.key:
                self.do_apply(anchors, only, exclude, offsets)
        elif cmd[0] == "off":
            if self.api and self.api.key:
                self.do_off(cmd[1])

    def search_message(self):
        if self.configured:
            return "Looking for a Hue bridge at %s…" % self.configured
        return "Searching for a Hue bridge…"

    # ---- Discovery and pairing

    def discover(self):
        candidates = []
        if self.configured:
            candidates.append(self.configured)
        else:
            candidates.extend(entry.get("address", "") for entry in self.saved.values())
            candidates.extend(mdns_discover())
            if not candidates:
                candidates.extend(cloud_discover())
        seen = set()
        for address in candidates:
            if not address or address in seen:
                continue
            seen.add(address)
            api = Api(address)
            try:
                config = api.config()
            except (OSError, HueError, ValueError):
                continue
            self.api = api
            self.bridge_id = str(config.get("bridgeid", "")).upper()
            self.bridge_name = str(config.get("name", "Hue Bridge"))
            saved = self.saved.get(self.bridge_id)
            if saved and saved.get("key"):
                api.key = saved["key"]
                if saved.get("address") != address:
                    saved["address"] = address
                    self.save()
                self.next_poll = 0.0
                self.set_status("connected")
            else:
                self.next_pair = 0.0
                self.set_status("unpaired", "%s found at %s. Press the link button on the bridge to pair." % (self.bridge_name, address))
            return
        self.set_status("searching", self.search_message())

    def try_pair(self):
        key = self.api.pair()
        if not key:
            return
        self.api.key = key
        self.saved[self.bridge_id] = {"address": self.api.address, "name": self.bridge_name, "key": key}
        self.save()
        self.next_poll = 0.0
        self.set_status("connected")

    def forget_key(self, reason):
        """The bridge rejected the key: the user removed this app in the Hue app."""
        if self.bridge_id in self.saved:
            del self.saved[self.bridge_id]
            self.save()
        if self.api:
            self.api.key = None
        self.set_lights([])
        self.next_pair = 0.0
        self.set_status("unpaired", "%s. Press the link button on the bridge to pair again." % reason)

    def lost(self, error):
        self.failures += 1
        address = self.api.address if self.api else self.configured
        self.set_status("unreachable", "Hue bridge at %s is not answering: %s" % (address, error))
        # A bridge can change address; after repeated failures at the saved
        # one, discovery runs again. A configured address is retried as is.
        if self.failures >= 3 and not self.configured:
            self.reset("searching", self.search_message())
            self.next_discover = 0.0

    # ---- Lights

    def poll(self):
        data = self.api.resource("light")
        lights = []
        owners = set()
        for entry in data:
            if not isinstance(entry, dict) or "color" not in entry:
                continue
            xy = entry.get("color", {}).get("xy")
            grad = entry.get("gradient") or {}
            points = int(grad.get("points_capable") or 0)
            owner = (entry.get("owner") or {}).get("rid", "")
            owners.add(owner)
            lights.append({
                "id": entry.get("id", ""),
                "order": self.order_of(entry.get("id_v1", "")),
                "name": (entry.get("metadata") or {}).get("name") or "Hue light",
                "on": bool((entry.get("on") or {}).get("on")),
                "xy": (xy["x"], xy["y"]) if isinstance(xy, dict) else None,
                "gradient": [(p["color"]["xy"]["x"], p["color"]["xy"]["y"])
                             for p in grad.get("points", []) if self.has_xy(p)],
                "points": points,
                "owner": owner,
                "product": "",
            })
        if owners - set(self.products):
            for dev in self.api.resource("device"):
                if isinstance(dev, dict):
                    self.products[dev.get("id", "")] = (dev.get("product_data") or {}).get("product_name", "")
        for light in lights:
            light["product"] = self.products.get(light["owner"], "")
        lights.sort(key=lambda l: (l["order"], l["name"]))
        self.failures = 0
        self.load_groups()
        if self.room:
            group = self.find_group()
            if group is None:
                self.set_lights([])
                names = ", ".join(sorted(g["name"] for g in self.groups)) or "none"
                self.set_status("connected", "No Hue room or zone named \u201c%s\u201d. The bridge has: %s." % (self.room, names))
                return
            lights = [l for l in lights if l["owner"] in group["devices"] or l["id"] in group["lights"]]
        self.set_lights(lights)
        if lights:
            self.set_status("connected")
        elif self.room:
            self.set_status("connected", "No color-capable lights in \u201c%s\u201d." % self.room)
        else:
            self.set_status("connected", "Paired with %s; it reports no color-capable lights." % (self.bridge_name or "the Hue bridge"))

    def load_groups(self):
        """Rooms and zones, re-read every GROUPS_SECONDS.

        A room's children are devices, which lights point at through
        `owner`; a zone's children are lights themselves. The names feed the
        panel's room picker.
        """
        now = time.monotonic()
        if now - self.groups_at < GROUPS_SECONDS:
            return
        groups = []
        for kind in ("room", "zone"):
            for entry in self.api.resource(kind):
                if not isinstance(entry, dict):
                    continue
                children = [c for c in entry.get("children", []) if isinstance(c, dict)]
                groups.append({
                    "name": (entry.get("metadata") or {}).get("name") or "",
                    "devices": {c.get("rid") for c in children if c.get("rtype") == "device"},
                    "lights": {c.get("rid") for c in children if c.get("rtype") == "light"},
                })
        with self.lock:
            self.groups = groups
        self.groups_at = now
        self.notify()

    def find_group(self):
        """The room or zone named by the `room` setting, or None. Names
        match case-insensitively."""
        wanted = self.room.casefold()
        for group in self.groups:
            if group["name"].casefold() == wanted:
                return group
        return None

    @staticmethod
    def has_xy(point):
        try:
            xy = point["color"]["xy"]
            return isinstance(xy.get("x"), (int, float)) and isinstance(xy.get("y"), (int, float))
        except (KeyError, TypeError, AttributeError):
            return False

    @staticmethod
    def order_of(id_v1):
        try:
            return int(str(id_v1).rsplit("/", 1)[-1])
        except ValueError:
            return 1 << 30

    def targets(self, anchors, offsets):
        """Colors per light id: gradient lights get the palette across their
        points; the rest share one gradient spread across them all. A
        light's saved offset rotates the palette before either."""
        with self.lock:
            lights = list(self.lights)
        out = {}
        plain = [l for l in lights if l["points"] < 2]
        for i, light in enumerate(plain):
            out[light["id"]] = [spread(anchors, len(plain), offsets.get(light["name"], 0))[i]]
        for light in lights:
            if light["points"] >= 2:
                out[light["id"]] = gradient(rotate(anchors, offsets.get(light["name"], 0)), light["points"])
        return out

    def do_apply(self, anchors, only, exclude, offsets):
        anchors = [a for a in anchors if isinstance(a, str) and len(a) == 6]
        if not anchors:
            return
        colors = self.targets(anchors, offsets)
        with self.lock:
            lights = list(self.lights)
        for light in lights:
            index = INDEX_PREFIX + light["id"]
            if only is not None and index != only:
                continue
            # A theme switch leaves a light that is off alone: switching
            # themes at night should not turn the bedroom on. An explicit
            # re-apply from the panel turns it on.
            if only is None and (light["name"] in exclude or not light["on"]):
                continue
            xys = [hex_to_xy(h) for h in colors.get(light["id"], [])]
            xys = [xy for xy in xys if xy]
            if not xys:
                continue
            body = {"on": {"on": True}}
            if light["points"] >= 2 and len(xys) >= 2:
                body["gradient"] = {"points": [{"color": {"xy": {"x": x, "y": y}}} for x, y in xys]}
            else:
                body["color"] = {"xy": {"x": xys[0][0], "y": xys[0][1]}}
            self.api.put_light(light["id"], body)
            time.sleep(0.1)
        self.poll()

    def do_off(self, index):
        with self.lock:
            lights = list(self.lights)
        for light in lights:
            if INDEX_PREFIX + light["id"] == index:
                self.api.put_light(light["id"], {"on": {"on": False}})
        self.poll()
