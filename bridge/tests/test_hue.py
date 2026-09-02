import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hue  # noqa: E402
from hue import Api, Hue, HueError, KeyRejected  # noqa: E402


def light(id_, number, name, on=True, xy=(0.3, 0.3), owner="dev", gradient_points=0, color=True):
    entry = {
        "id": id_, "id_v1": "/lights/%d" % number, "owner": {"rid": owner, "rtype": "device"},
        "metadata": {"name": name}, "on": {"on": on}, "type": "light",
    }
    if color:
        entry["color"] = {"xy": {"x": xy[0], "y": xy[1]}}
    if gradient_points:
        entry["gradient"] = {"points_capable": gradient_points,
                             "points": [{"color": {"xy": {"x": 0.7, "y": 0.3}}}, {"color": {"xy": {"x": 0.17, "y": 0.7}}}]}
    return entry


class FakeApi(Api):
    """Api with `request` replaced by a table of canned replies."""

    def __init__(self, address="192.168.1.15", key=None, replies=None):
        super().__init__(address, key)
        self.replies = replies or {}
        self.calls = []

    def request(self, method, path, body=None, timeout=4.0):
        self.calls.append((method, path, body))
        reply = self.replies.get((method, path))
        if reply is None:
            reply = self.replies.get(path)
        if reply is None and method == "PUT" and path.startswith("/clip/v2/resource/light/"):
            reply = (200, {"errors": [], "data": [{"rid": path.rsplit("/", 1)[-1], "rtype": "light"}]})
        if reply is None:
            raise OSError("no route to %s" % path)
        if isinstance(reply, Exception):
            raise reply
        return reply


def clip(data):
    return (200, {"errors": [], "data": data})


class ApiTest(unittest.TestCase):
    def test_config_needs_a_bridge_id(self):
        api = FakeApi(replies={"/api/0/config": (200, {"bridgeid": "ABC", "name": "Hue Bridge"})})
        self.assertEqual(api.config()["bridgeid"], "ABC")
        api = FakeApi(replies={"/api/0/config": (200, {"hello": "router"})})
        with self.assertRaises(HueError):
            api.config()

    def test_pair_outcomes(self):
        cases = {
            "success": ((200, [{"success": {"username": "KEY", "clientkey": "CK"}}]), "KEY"),
            "button": ((200, [{"error": {"type": 101, "description": "link button not pressed"}}]), None),
        }
        for name, (reply, expected) in cases.items():
            api = FakeApi(replies={("POST", "/api"): reply})
            self.assertEqual(api.pair(), expected, name)
            self.assertEqual(api.calls[-1][2]["devicetype"], hue.DEVICE_TYPE)
        for bad in ((200, [{"error": {"type": 7, "description": "invalid value"}}]), (200, []), (500, None),
                    (200, ["junk"]), (200, [{"success": {"username": ""}}])):
            with self.assertRaises(HueError):
                FakeApi(replies={("POST", "/api"): bad}).pair()

    def test_resource_status_handling(self):
        api = FakeApi(key="k", replies={"/clip/v2/resource/light": clip([{"id": "a"}])})
        self.assertEqual(api.resource("light"), [{"id": "a"}])
        with self.assertRaises(KeyRejected):
            FakeApi(key="k", replies={"/clip/v2/resource/light": (403, {"errors": [{"description": "unauthorized"}]})}).resource("light")
        with self.assertRaises(HueError):
            FakeApi(key="k", replies={"/clip/v2/resource/light": (503, None)}).resource("light")

    def test_put_light_raises_on_errors(self):
        api = FakeApi(key="k", replies={("PUT", "/clip/v2/resource/light/a"): (200, {"errors": [], "data": []})})
        api.put_light("a", {"on": {"on": True}})
        self.assertEqual(api.calls[-1][0], "PUT")
        with self.assertRaises(HueError) as ctx:
            FakeApi(key="k", replies={("PUT", "/clip/v2/resource/light/a"): (207, {"errors": [{"description": "too many points"}]})}).put_light("a", {})
        self.assertIn("too many points", str(ctx.exception))
        with self.assertRaises(KeyRejected):
            FakeApi(key="k", replies={("PUT", "/clip/v2/resource/light/a"): (401, None)}).put_light("a", {})

    def test_application_key_header(self):
        conn = mock.Mock()
        conn.getresponse.return_value.status = 200
        conn.getresponse.return_value.read.return_value = b"{}"
        with mock.patch("http.client.HTTPSConnection", return_value=conn):
            Api("1.2.3.4", key="secret").request("GET", "/x")
        headers = conn.request.call_args.kwargs["headers"]
        self.assertEqual(headers["hue-application-key"], "secret")

    def test_oversized_body_is_rejected(self):
        conn = mock.Mock()
        conn.getresponse.return_value.status = 200
        conn.getresponse.return_value.read.return_value = b"x" * (hue.MAX_BODY_BYTES + 1)
        with mock.patch("http.client.HTTPSConnection", return_value=conn):
            with self.assertRaises(HueError):
                Api("1.2.3.4").request("GET", "/x")


class HueTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "state", "hue.json")
        self.notifications = 0

    def tearDown(self):
        self.tmp.cleanup()

    def notify(self):
        self.notifications += 1

    def paired(self, lights, devices=(), rooms=(), zones=(), room=""):
        """A Hue backend already paired with a bridge serving these resources."""
        h = Hue(notify=self.notify, path=self.path)
        h.enabled = True
        h.room = room
        h.bridge_id = "ECB5"
        h.bridge_name = "Hue Bridge"
        h.api = FakeApi(key="k", replies={
            "/clip/v2/resource/light": clip(lights),
            "/clip/v2/resource/device": clip(list(devices)),
            "/clip/v2/resource/room": clip(list(rooms)),
            "/clip/v2/resource/zone": clip(list(zones)),
        })
        return h

    def puts(self, h):
        return [(path.rsplit("/", 1)[-1], body) for method, path, body in h.api.calls if method == "PUT"]


class PollTest(HueTestCase):
    def test_lights_are_listed_in_bridge_order_with_products(self):
        h = self.paired(
            [light("b", 3, "Desk", owner="d1"), light("a", 1, "Strip", owner="d2", gradient_points=5),
             light("w", 2, "Hall", color=False)],
            devices=[{"id": "d1", "product_data": {"product_name": "Hue color lamp"}},
                     {"id": "d2", "product_data": {"product_name": "Hue play gradient lightstrip"}}])
        h.poll()
        info, devices = h.snapshot()
        self.assertEqual(info["status"], "connected")
        self.assertEqual([d["name"] for d in devices], ["Strip", "Desk"])
        self.assertEqual(devices[0]["detail"], "Philips Hue · gradient, 5 points")
        self.assertEqual(devices[0]["leds"], 5)
        self.assertEqual(devices[1]["detail"], "Philips Hue · Hue color lamp")
        self.assertEqual(devices[1]["index"], "hue:b")

    def test_public_colors(self):
        h = self.paired([light("on", 1, "On", xy=(0.7006, 0.2993)), light("off", 2, "Off", on=False),
                         light("g", 3, "Grad", gradient_points=5)])
        h.poll()
        colors = {d["name"]: d["colors"] for d in h.snapshot()[1]}
        self.assertEqual(colors["On"], ["FF0000"])
        self.assertEqual(colors["Off"], ["000000"])
        self.assertEqual(len(colors["Grad"]), 2)
        modes = {d["name"]: d["activeMode"] for d in h.snapshot()[1]}
        self.assertEqual((modes["On"], modes["Off"]), ("on", "off"))

    def test_no_color_lights_gives_a_message(self):
        h = self.paired([light("w", 1, "Hall", color=False)])
        h.poll()
        info, devices = h.snapshot()
        self.assertEqual(devices, [])
        self.assertIn("no color-capable lights", info["message"])

    def test_hostile_json_is_bounded(self):
        # The malformed entries come first so the light cap does not drop them.
        entries = ["junk", {"id": None, "color": {}},
                   {"id": "bad", "color": {"xy": "nope"}, "metadata": {"name": 7}, "on": {"on": True}}]
        entries += [light("l%d" % i, i, "L%d" % i) for i in range(hue.MAX_LIGHTS + 20)]
        entries[3]["metadata"]["name"] = "n" * 1000
        entries[3]["gradient"] = {"points_capable": 10 ** 9, "points": [{"color": {"xy": {"x": 9, "y": 9}}}]}
        h = self.paired(entries, devices=["junk", {"id": "dev", "product_data": {"product_name": 5}}])
        h.poll()
        devices = h.snapshot()[1]
        self.assertEqual(len(devices), hue.MAX_LIGHTS)
        self.assertEqual(len(devices[0]["name"]), hue.MAX_NAME_CHARS)
        self.assertEqual(devices[0]["leds"], 1)  # the absurd points_capable was dropped
        bad = [d for d in devices if d["index"] == "hue:bad"]
        self.assertEqual(bad[0]["colors"], ["FFFFFF"])
        self.assertEqual(bad[0]["name"], "Hue light")

    def test_light_list_must_be_a_list(self):
        h = self.paired([])
        h.api.replies["/clip/v2/resource/light"] = (200, {"data": "nope"})
        with self.assertRaises(HueError):
            h.poll()

    def test_polling_notifies(self):
        h = self.paired([light("a", 1, "A")])
        h.poll()
        self.assertGreater(self.notifications, 0)


class RoomTest(HueTestCase):
    ROOMS = [{"metadata": {"name": "Lewis Office"}, "children": [{"rid": "d1", "rtype": "device"}, {"rid": "d2", "rtype": "device"}]},
             {"metadata": {"name": "Bedroom"}, "children": [{"rid": "d3", "rtype": "device"}]}]
    ZONES = [{"metadata": {"name": "Desk zone"}, "children": [{"rid": "a", "rtype": "light"}]}]
    LIGHTS = [light("a", 3, "Desk", owner="d1"), light("b", 1, "Strip", owner="d2", gradient_points=5), light("c", 2, "Bed", owner="d3")]

    def with_room(self, room):
        h = self.paired(self.LIGHTS, rooms=self.ROOMS, zones=self.ZONES, room=room)
        h.poll()
        return h

    def test_rooms_are_reported_for_the_picker(self):
        info, _ = self.with_room("").snapshot()
        self.assertEqual(info["rooms"], ["Bedroom", "Desk zone", "Lewis Office"])
        self.assertEqual(info["room"], "")

    def test_room_filters_by_owner_device_case_insensitively(self):
        _, devices = self.with_room("lewis office").snapshot()
        self.assertEqual([d["name"] for d in devices], ["Strip", "Desk"])

    def test_zone_filters_by_light(self):
        _, devices = self.with_room("Desk zone").snapshot()
        self.assertEqual([d["name"] for d in devices], ["Desk"])

    def test_unknown_room_lists_what_exists(self):
        info, devices = self.with_room("Garage").snapshot()
        self.assertEqual(devices, [])
        self.assertIn("Garage", info["message"])
        self.assertIn("Bedroom, Desk zone, Lewis Office", info["message"])

    def test_room_with_no_color_lights(self):
        h = self.paired([light("w", 1, "Hall", owner="d3", color=False)], rooms=self.ROOMS, room="Bedroom")
        h.poll()
        self.assertIn("No color-capable lights in", h.snapshot()[0]["message"])

    def test_room_change_re_polls_without_reconnecting(self):
        h = self.with_room("Bedroom")
        api = h.api
        h.handle(("configure", True, "", "Lewis Office"))
        self.assertIs(h.api, api)
        self.assertEqual([d["name"] for d in h.snapshot()[1]], ["Strip", "Desk"])

    def test_groups_are_cached_between_polls(self):
        h = self.with_room("Bedroom")
        room_reads = lambda: sum(1 for _, path, _ in h.api.calls if path.endswith("/room"))
        before = room_reads()
        h.poll()
        self.assertEqual(room_reads(), before)
        h.groups_at = 0.0
        h.poll()
        self.assertEqual(room_reads(), before + 1)

    def test_hostile_groups_are_bounded(self):
        rooms = [{"metadata": {"name": "R%d" % i}, "children": [{"rid": "d1", "rtype": "device"}]} for i in range(hue.MAX_GROUPS + 5)]
        rooms.append("junk")
        rooms.append({"metadata": {"name": "X"}, "children": "nope"})
        h = self.paired(self.LIGHTS, rooms=rooms, room="R1")
        h.poll()
        self.assertEqual(len(h.groups), hue.MAX_GROUPS)


class ApplyTest(HueTestCase):
    ANCHORS = ["FF0000", "00FF00", "0000FF", "FFFFFF"]

    def test_theme_apply_spreads_plain_lights_and_fills_gradient_lights(self):
        h = self.paired([light("a", 1, "First"), light("b", 2, "Second"), light("g", 3, "Strip", gradient_points=5)])
        h.poll()
        h.do_apply(self.ANCHORS, None, set(), {})
        puts = dict(self.puts(h))
        self.assertEqual(puts["a"]["color"]["xy"], {"x": 0.7006, "y": 0.2993})  # red
        self.assertEqual(puts["b"]["color"]["xy"], {"x": 0.1724, "y": 0.7468})  # green: two lights blend two anchors
        self.assertEqual(len(puts["g"]["gradient"]["points"]), 5)
        self.assertNotIn("color", puts["g"])
        for body in puts.values():
            self.assertEqual(body["on"], {"on": True})

    def test_theme_apply_skips_lights_that_are_off_and_excluded_names(self):
        h = self.paired([light("a", 1, "Dark", on=False), light("b", 2, "Skip"), light("c", 3, "Keep")])
        h.poll()
        h.do_apply(self.ANCHORS, None, {"Skip"}, {})
        self.assertEqual([lid for lid, _ in self.puts(h)], ["c"])

    def test_explicit_apply_turns_an_off_light_on(self):
        h = self.paired([light("a", 1, "Dark", on=False)])
        h.poll()
        h.do_apply(self.ANCHORS, "hue:a", set(), {})
        self.assertEqual(self.puts(h)[0][1]["on"], {"on": True})

    def test_offsets_step_a_single_light_through_the_anchors(self):
        h = self.paired([light("a", 1, "Iris")])
        h.poll()
        for k in range(3):
            h.do_apply(self.ANCHORS[:3], None, set(), {"Iris": k})
        xys = [body["color"]["xy"] for _, body in self.puts(h)]
        self.assertEqual(xys, [{"x": 0.7006, "y": 0.2993}, {"x": 0.1724, "y": 0.7468}, {"x": 0.1355, "y": 0.0399}])

    def test_bad_anchors_send_nothing(self):
        h = self.paired([light("a", 1, "A")])
        h.poll()
        h.do_apply(["nope", 12], None, set(), {})
        self.assertEqual(self.puts(h), [])

    def test_off(self):
        h = self.paired([light("a", 1, "A"), light("b", 2, "B")])
        h.poll()
        h.do_off("hue:b")
        self.assertEqual(self.puts(h), [("b", {"on": {"on": False}})])

    def test_commands_are_ignored_while_unpaired(self):
        h = self.paired([light("a", 1, "A")])
        h.api.key = None
        h.handle(("apply", self.ANCHORS, None, set(), {}))
        h.handle(("off", "hue:a"))
        h.handle(("refresh",))
        self.assertEqual(h.api.calls, [])


class PairingTest(HueTestCase):
    def test_pairing_saves_the_key_privately(self):
        h = Hue(notify=self.notify, path=self.path)
        h.bridge_id = "ECB5"
        h.bridge_name = "Hue Bridge"
        h.api = FakeApi(replies={("POST", "/api"): (200, [{"success": {"username": "KEY"}}])})
        h.try_pair()
        self.assertEqual(h.api.key, "KEY")
        self.assertEqual(h.snapshot()[0]["status"], "connected")
        with open(self.path) as f:
            saved = json.load(f)
        self.assertEqual(saved["bridges"]["ECB5"], {"address": "192.168.1.15", "name": "Hue Bridge", "key": "KEY"})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_unpressed_button_keeps_waiting(self):
        h = Hue(notify=self.notify, path=self.path)
        h.api = FakeApi(replies={("POST", "/api"): (200, [{"error": {"type": 101, "description": "link button not pressed"}}])})
        h.try_pair()
        self.assertIsNone(h.api.key)
        self.assertFalse(os.path.exists(self.path))

    def test_rejected_key_is_forgotten(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump({"bridges": {"ECB5": {"address": "192.168.1.15", "key": "OLD"}}}, f)
        h = Hue(notify=self.notify, path=self.path)
        h.bridge_id = "ECB5"
        h.api = FakeApi(key="OLD", replies={"/clip/v2/resource/light": (403, None)})
        h.enabled = True
        try:
            h.poll()
        except KeyRejected as e:
            h.forget_key(str(e))
        self.assertIsNone(h.api.key)
        self.assertEqual(h.snapshot()[0]["status"], "unpaired")
        with open(self.path) as f:
            self.assertEqual(json.load(f)["bridges"], {})

    def test_corrupt_state_file_is_ignored(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(Hue(notify=self.notify, path=self.path).saved, {})


class DiscoveryTest(HueTestCase):
    def fake_network(self, bridges, mdns=(), cloud=()):
        """Patch Api so each address answers /api/0/config as `bridges` says."""
        def factory(address, key=None):
            config = bridges.get(address)
            replies = {"/api/0/config": (200, config)} if config else {}
            return FakeApi(address, key, replies)
        patches = [mock.patch.object(hue, "Api", side_effect=factory),
                   mock.patch.object(hue, "mdns_discover", return_value=list(mdns)),
                   mock.patch.object(hue, "cloud_discover", return_value=list(cloud))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_mdns_result_is_used_and_unpaired_bridge_prompts_for_the_button(self):
        self.fake_network({"192.168.1.15": {"bridgeid": "ecb5", "name": "Hue Bridge"}}, mdns=["192.168.1.15"])
        h = Hue(notify=self.notify, path=self.path)
        h.enabled = True
        h.discover()
        info = h.snapshot()[0]
        self.assertEqual(info["status"], "unpaired")
        self.assertIn("Press the link button", info["message"])
        self.assertEqual(info["address"], "192.168.1.15")
        self.assertEqual(h.bridge_id, "ECB5")

    def test_cloud_is_only_asked_when_mdns_finds_nothing(self):
        self.fake_network({"10.0.0.2": {"bridgeid": "b"}}, mdns=[], cloud=["10.0.0.2"])
        h = Hue(notify=self.notify, path=self.path)
        h.discover()
        self.assertEqual(h.api.address, "10.0.0.2")
        hue.cloud_discover.assert_called_once()

    def test_configured_address_skips_discovery(self):
        self.fake_network({"1.2.3.4": {"bridgeid": "b"}}, mdns=["9.9.9.9"])
        h = Hue(notify=self.notify, path=self.path)
        h.configured = "1.2.3.4"
        h.discover()
        self.assertEqual(h.api.address, "1.2.3.4")
        hue.mdns_discover.assert_not_called()

    def test_saved_pairing_is_reused_and_a_moved_bridge_updates_its_address(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump({"bridges": {"B": {"address": "192.168.1.15", "key": "KEY"}}}, f)
        self.fake_network({"192.168.1.99": {"bridgeid": "b"}}, mdns=["192.168.1.99"])
        h = Hue(notify=self.notify, path=self.path)
        h.discover()
        self.assertEqual(h.api.key, "KEY")
        self.assertEqual(h.snapshot()[0]["status"], "connected")
        with open(self.path) as f:
            self.assertEqual(json.load(f)["bridges"]["B"]["address"], "192.168.1.99")

    def test_non_bridges_are_skipped(self):
        self.fake_network({"10.0.0.5": {"hello": "printer"}}, mdns=["10.0.0.5", "10.0.0.6"])
        h = Hue(notify=self.notify, path=self.path)
        h.discover()
        self.assertIsNone(h.api)
        self.assertEqual(h.snapshot()[0]["status"], "searching")


class LifecycleTest(HueTestCase):
    def test_disable_resets_everything(self):
        h = self.paired([light("a", 1, "A")])
        h.poll()
        h.handle(("configure", False, "", ""))
        info, devices = h.snapshot()
        self.assertEqual((info["status"], devices, h.api), ("disabled", [], None))

    def test_enable_starts_searching(self):
        h = Hue(notify=self.notify, path=self.path)
        h.handle(("configure", True, "", ""))
        self.assertEqual(h.snapshot()[0]["message"], "Searching for a Hue bridge…")
        h.handle(("configure", True, "1.2.3.4", ""))
        self.assertEqual(h.snapshot()[0]["message"], "Looking for a Hue bridge at 1.2.3.4…")

    def test_repeated_failures_return_to_discovery_unless_an_address_is_configured(self):
        h = self.paired([])
        for _ in range(3):
            h.lost(OSError("timed out"))
        self.assertIsNone(h.api)
        self.assertEqual(h.snapshot()[0]["status"], "searching")

        h = self.paired([])
        h.configured = "192.168.1.15"
        for _ in range(5):
            h.lost(OSError("timed out"))
        self.assertIsNotNone(h.api)
        self.assertEqual(h.snapshot()[0]["status"], "unreachable")
        self.assertIn("192.168.1.15", h.snapshot()[0]["message"])

    def test_status_change_notifies_once_per_change(self):
        h = Hue(notify=self.notify, path=self.path)
        h.set_status("searching", "a")
        h.set_status("searching", "a")
        self.assertEqual(self.notifications, 1)

    def test_run_loop_processes_commands_and_stops(self):
        # Discovery is stubbed so the thread touches no network.
        with mock.patch.object(hue, "mdns_discover", return_value=[]), \
                mock.patch.object(hue, "cloud_discover", return_value=[]):
            h = Hue(notify=self.notify, path=self.path)
            h.start()
            h.configure(True, "", "Office")
            h.stop()
            h.join(timeout=5)
            self.assertFalse(h.is_alive())
        self.assertEqual((h.enabled, h.room), (True, "Office"))
        self.assertEqual(h.snapshot()[0]["status"], "searching")


class MdnsPacketTest(unittest.TestCase):
    def test_query_is_sent_to_the_multicast_group(self):
        sock = mock.Mock()
        sock.recvfrom.side_effect = OSError("done")
        with mock.patch("socket.socket", return_value=sock):
            self.assertEqual(hue.mdns_discover(timeout=0.01), [])
        (packet, target), _ = sock.sendto.call_args
        self.assertEqual(target, ("224.0.0.251", 5353))
        self.assertIn(b"\x04_hue\x04_tcp\x05local\x00", packet)
        self.assertTrue(packet.endswith(b"\x00\x0c\x00\x01"))  # PTR, IN

    def test_only_answers_mentioning_hue_count(self):
        sock = mock.Mock()
        answer = b"\x00\x00\x84\x00" + b"\x00" * 8 + b"_hue._tcp.local"
        question = b"\x00\x00\x00\x00" + b"\x00" * 8 + b"_hue._tcp.local"
        other = b"\x00\x00\x84\x00" + b"\x00" * 8 + b"_ipp._tcp.local"
        sock.recvfrom.side_effect = [(answer, ("192.168.1.15", 5353)), (question, ("192.168.1.2", 5353)),
                                     (other, ("192.168.1.3", 5353)), (answer, ("192.168.1.15", 5353)), OSError("done")]
        with mock.patch("socket.socket", return_value=sock):
            self.assertEqual(hue.mdns_discover(timeout=5), ["192.168.1.15"])


if __name__ == "__main__":
    unittest.main()
