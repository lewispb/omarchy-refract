import io
import json
import os
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openrgb_bridge as ob  # noqa: E402


def sdk_string(text):
    raw = text.encode("utf-8") + b"\x00"
    return struct.pack("<H", len(raw)) + raw


def controller_blob(protocol=3, name="Test Device", location="HID: /dev/hidraw9",
                    dev_type=5, modes=("Direct", "Static"), active_mode=0,
                    zones=1, leds=4, colors=None, num_modes=None, num_zones=None,
                    num_leds=None, num_colors=None, total_size=None, mode_colors=0):
    """A controller-data blob as the server lays it out.

    The `num_*` overrides write a count different from the items actually
    present, which is how a hostile or truncated blob looks.
    """
    colors = colors if colors is not None else [0x0000FF] * leds  # 0x00BBGGRR: red
    body = struct.pack("<i", dev_type) + sdk_string(name)
    if protocol >= 1:
        body += sdk_string("Vendor")
    body += sdk_string("Description") + sdk_string("1.0") + sdk_string("SERIAL") + sdk_string(location)
    body += struct.pack("<H", len(modes) if num_modes is None else num_modes)
    body += struct.pack("<i", active_mode)
    for i, mode_name in enumerate(modes):
        body += sdk_string(mode_name) + struct.pack("<iI", i, 0)
        body += struct.pack("<II", 0, 0)  # speed min/max
        if protocol >= 3:
            body += struct.pack("<II", 0, 100)  # brightness min/max
        body += struct.pack("<II", 0, 0)  # colors min/max
        body += struct.pack("<I", 0)  # speed
        if protocol >= 3:
            body += struct.pack("<I", 100)  # brightness
        body += struct.pack("<II", 0, 0)  # direction, color mode
        body += struct.pack("<H", mode_colors) + b"\x00" * (4 * mode_colors)
    body += struct.pack("<H", zones if num_zones is None else num_zones)
    for _ in range(zones):
        body += sdk_string("Zone") + struct.pack("<iIII", 0, 0, 0, leds) + struct.pack("<H", 0)
    body += struct.pack("<H", leds if num_leds is None else num_leds)
    for i in range(leds):
        body += sdk_string("LED %d" % i) + struct.pack("<I", 0)
    body += struct.pack("<H", len(colors) if num_colors is None else num_colors)
    for c in colors:
        body += struct.pack("<I", c)
    size = 4 + len(body) if total_size is None else total_size
    return struct.pack("<I", size) + body


class ReaderTest(unittest.TestCase):
    def test_reads_in_order(self):
        r = ob.Reader(struct.pack("<HIi", 7, 8, -9) + sdk_string("hi"))
        self.assertEqual((r.u16(), r.u32(), r.i32(), r.string()), (7, 8, -9, "hi"))

    def test_reading_past_the_end_fails_closed(self):
        r = ob.Reader(b"\x01")
        with self.assertRaises(ob.ProtocolError):
            r.u32()

    def test_oversized_string_is_rejected_before_it_is_read(self):
        r = ob.Reader(struct.pack("<H", ob.MAX_STRING_BYTES + 1) + b"x" * 10)
        with self.assertRaises(ob.ProtocolError):
            r.string()

    def test_long_names_are_truncated(self):
        r = ob.Reader(sdk_string("n" * 400))
        self.assertEqual(len(r.string()), ob.MAX_NAME_CHARS)


class ParseControllerTest(unittest.TestCase):
    def parse(self, **kw):
        protocol = kw.get("protocol", 3)
        with mock.patch.object(ob, "hid_name", return_value=""):
            return ob.parse_controller(controller_blob(**kw), protocol)

    def test_fields(self):
        dev = self.parse(name="Mouse", leds=4, active_mode=1)
        self.assertEqual(dev["name"], "Mouse")
        self.assertEqual(dev["type"], 5)
        self.assertEqual(dev["leds"], 4)
        self.assertEqual([m["name"] for m in dev["modes"]], ["Direct", "Static"])
        self.assertEqual(dev["active_mode"], 1)

    def test_colors_are_decoded_from_bgr_words(self):
        dev = self.parse(colors=[0x0000FF, 0x00FF00, 0xFF0000])
        self.assertEqual(dev["colors"], ["FF0000", "00FF00", "0000FF"])

    def test_every_protocol_layout_parses(self):
        for protocol in (0, 1, 2, 3):
            dev = self.parse(protocol=protocol, leds=2)
            self.assertEqual(dev["leds"], 2, "protocol %d" % protocol)

    def test_mode_colors_are_skipped(self):
        self.assertEqual(self.parse(mode_colors=3)["leds"], 4)

    def test_size_past_the_data_is_rejected(self):
        with self.assertRaises(ob.ProtocolError):
            self.parse(total_size=1 << 30)

    def test_counts_past_the_data_are_rejected(self):
        for field in ("num_modes", "num_zones", "num_leds", "num_colors"):
            with self.assertRaises(ob.ProtocolError, msg=field):
                self.parse(**{field: 200})

    def test_counts_over_the_caps_are_rejected(self):
        for field in ("num_modes", "num_zones", "num_leds", "num_colors"):
            with self.assertRaises(ob.ProtocolError, msg=field):
                self.parse(**{field: 65535})

    def test_too_many_mode_colors_is_rejected(self):
        blob = controller_blob(mode_colors=0)
        # Overwrite the first mode's color count with a huge value.
        marker = sdk_string("Direct")
        at = blob.index(marker) + len(marker) + 4 + 4 + 8 + 8 + 8 + 4 + 4 + 8
        hostile = blob[:at] + struct.pack("<H", 60000) + blob[at + 2:]
        with self.assertRaises(ob.ProtocolError):
            ob.parse_controller(hostile, 3)


class DisplayNameTest(unittest.TestCase):
    def test_no_kernel_name_keeps_openrgb_name(self):
        with mock.patch.object(ob, "hid_name", return_value=""):
            self.assertEqual(ob.display_name("Keychron Gaming Keyboard 1", "HID: /dev/hidraw3"), "Keychron Gaming Keyboard 1")

    def test_shared_word_keeps_openrgb_name(self):
        with mock.patch.object(ob, "hid_name", return_value="Logitech G502 X PLUS"):
            self.assertEqual(ob.display_name("Logitech G502 X Plus", "HID: /dev/hidraw3"), "Logitech G502 X Plus")

    def test_vendor_prefix_counts_as_shared(self):
        with mock.patch.object(ob, "hid_name", return_value="AsusTek Computer Inc. ROG"):
            self.assertEqual(ob.display_name("ASUS ROG Keyboard", "HID: /dev/hidraw3"), "ASUS ROG Keyboard")

    def test_no_shared_word_uses_kernel_name(self):
        with mock.patch.object(ob, "hid_name", return_value="Compx Flow84@Lofree"):
            self.assertEqual(ob.display_name("Keychron Gaming Keyboard 1", "HID: /dev/hidraw14"), "Compx Flow84@Lofree")

    def test_hid_name_reads_sysfs(self):
        opened = mock.mock_open(read_data="HID_ID=0003\nHID_NAME=Vendor Widget\n")
        with mock.patch("builtins.open", opened):
            self.assertEqual(ob.hid_name("HID: /dev/hidraw7"), "Vendor Widget")
        opened.assert_called_with("/sys/class/hidraw/hidraw7/device/uevent")

    def test_hid_name_for_non_hid_location(self):
        self.assertEqual(ob.hid_name("I2C: bus 1"), "")


class FakeSocket:
    """A socket that serves scripted packets and records what was sent."""

    def __init__(self, packets=()):
        self.inbound = b"".join(packets)
        self.sent = b""

    def recv(self, n):
        chunk, self.inbound = self.inbound[:n], self.inbound[n:]
        return chunk

    def sendall(self, data):
        self.sent += data

    def close(self):
        pass


def packet(device_id, packet_id, payload=b""):
    return b"ORGB" + struct.pack("<III", device_id, packet_id, len(payload)) + payload


class PacketTest(unittest.TestCase):
    def bridge(self, *packets):
        b = ob.Bridge()
        b.sock = FakeSocket(packets)
        return b

    def test_round_trip(self):
        b = self.bridge(packet(3, 1, b"abc"))
        self.assertEqual(b.recv_packet(), (3, 1, b"abc"))

    def test_bad_magic_is_rejected(self):
        b = self.bridge(b"XXXX" + struct.pack("<III", 0, 1, 0))
        with self.assertRaises(ob.ProtocolError):
            b.recv_packet()

    def test_oversized_payload_is_rejected_before_reading_it(self):
        b = self.bridge(b"ORGB" + struct.pack("<III", 0, 1, 0xFFFFFFFF))
        with self.assertRaises(ob.ProtocolError):
            b.recv_packet()

    def test_closed_connection_is_an_error(self):
        b = self.bridge(b"ORGB" + struct.pack("<III", 0, 1, 10) + b"short")
        with self.assertRaises(ConnectionError):
            b.recv_packet()

    def test_request_skips_device_list_notifications(self):
        b = self.bridge(packet(0, ob.DEVICE_LIST_UPDATED), packet(0, ob.REQUEST_CONTROLLER_COUNT, struct.pack("<I", 2)))
        _, payload = b.request(0, ob.REQUEST_CONTROLLER_COUNT)
        self.assertEqual(struct.unpack("<I", payload)[0], 2)
        self.assertTrue(b.pending_list_update)
        self.assertEqual(b.sock.sent, packet(0, ob.REQUEST_CONTROLLER_COUNT))

    def test_update_leds_sends_custom_mode_then_colors(self):
        b = self.bridge()
        b.update_leds(2, ["FF0000", "00FF00"])
        expected_body = struct.pack("<H", 2) + bytes([0xFF, 0, 0, 0, 0, 0xFF, 0, 0])
        expected = packet(2, ob.RGBCONTROLLER_SETCUSTOMMODE) + packet(
            2, ob.RGBCONTROLLER_UPDATELEDS, struct.pack("<I", 4 + len(expected_body)) + expected_body)
        self.assertEqual(b.sock.sent, expected)


class RefreshTest(unittest.TestCase):
    def test_too_many_controllers_disconnects(self):
        b = ob.Bridge()
        b.sock = FakeSocket([packet(0, ob.REQUEST_CONTROLLER_COUNT, struct.pack("<I", ob.MAX_CONTROLLERS + 1))])
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            b.refresh()
        self.assertIsNone(b.sock)
        state = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertFalse(state["connected"])
        self.assertIn("controllers", state["error"])

    def test_devices_are_parsed_and_emitted(self):
        blob = controller_blob(name="Strip", leds=20, colors=[0x0000FF] * 20)
        b = ob.Bridge()
        b.sock = FakeSocket([packet(0, ob.REQUEST_CONTROLLER_COUNT, struct.pack("<I", 1)),
                             packet(0, ob.REQUEST_CONTROLLER_DATA, blob)])
        b.protocol = 3
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(ob, "hid_name", return_value=""):
            b.refresh()
        state = json.loads(out.getvalue().strip())
        self.assertTrue(state["connected"])
        dev = state["devices"][0]
        self.assertEqual((dev["index"], dev["name"], dev["type"], dev["leds"], dev["activeMode"]),
                         (0, "Strip", "Keyboard", 20, "Direct"))
        # Swatches are sampled down so the event stays small.
        self.assertLessEqual(len(dev["colors"]), 12)


class EmitStateTest(unittest.TestCase):
    def test_identical_state_is_emitted_once(self):
        b = ob.Bridge()
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            b.emit_state(error="x")
            b.emit_state(error="x")
            b.emit_state(error="x", force=True)
        self.assertEqual(len(out.getvalue().strip().splitlines()), 2)

    def test_hue_devices_follow_openrgb_devices(self):
        b = ob.Bridge()
        b.devices = [{"index": 0, "name": "Mouse", "type": 6, "leds": 1, "modes": [], "active_mode": 0, "colors": ["FF0000"]}]
        with mock.patch.object(b.hue, "snapshot", return_value=({"status": "connected", "message": ""}, [{"index": "hue:a", "name": "Lamp"}])):
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                b.emit_state()
        state = json.loads(out.getvalue())
        self.assertEqual([d["name"] for d in state["devices"]], ["Mouse", "Lamp"])
        self.assertEqual(state["hue"]["status"], "connected")


class ApplyTest(unittest.TestCase):
    def bridge(self):
        b = ob.Bridge()
        b.sock = FakeSocket()
        b.devices = [
            {"index": 0, "name": "Mouse", "type": 6, "leds": 1, "modes": [], "active_mode": 0, "colors": []},
            {"index": 1, "name": "Board", "type": 0, "leds": 0, "modes": [], "active_mode": 0, "colors": []},
            {"index": 2, "name": "Strip", "type": 4, "leds": 12, "modes": [], "active_mode": 0, "colors": []},
        ]
        b.refresh = mock.Mock()
        b.update_leds = mock.Mock()
        b.hue.apply = mock.Mock()
        return b

    def run_apply(self, b, msg):
        with mock.patch.object(sys, "stdout", io.StringIO()):
            b.apply(msg)

    def test_theme_apply_reaches_every_device_with_leds_and_the_hue_backend(self):
        b = self.bridge()
        self.run_apply(b, {"anchors": ["FF0000", "00FF00"], "vivid": False})
        self.assertEqual([c.args[0] for c in b.update_leds.call_args_list], [0, 2])
        b.hue.apply.assert_called_once_with(["FF0000", "00FF00"], None, set(), {})

    def test_exclude_skips_by_name(self):
        b = self.bridge()
        self.run_apply(b, {"anchors": ["FF0000", "00FF00"], "vivid": False, "exclude": ["Mouse"]})
        self.assertEqual([c.args[0] for c in b.update_leds.call_args_list], [2])

    def test_offsets_rotate_per_device(self):
        b = self.bridge()
        self.run_apply(b, {"anchors": ["FF0000", "00FF00"], "vivid": False, "offsets": {"Mouse": 1}})
        self.assertEqual(b.update_leds.call_args_list[0].args, (0, ["00FF00"]))

    def test_solid_style_uses_the_first_anchor_only(self):
        b = self.bridge()
        self.run_apply(b, {"anchors": ["FF0000", "00FF00"], "vivid": False, "style": "solid"})
        self.assertEqual(b.update_leds.call_args_list[1].args, (2, ["FF0000"] * 12))

    def test_vivid_is_applied_before_sending(self):
        b = self.bridge()
        self.run_apply(b, {"anchors": ["8090A0"], "vivid": True})
        self.assertNotEqual(b.update_leds.call_args_list[0].args[1], ["8090A0"])

    def test_hue_only_apply_does_not_touch_openrgb(self):
        b = self.bridge()
        self.run_apply(b, {"anchors": ["FF0000"], "vivid": False, "device": "hue:abc"})
        b.update_leds.assert_not_called()
        b.hue.apply.assert_called_once_with(["FF0000"], "hue:abc", set(), {})

    def test_single_openrgb_device(self):
        b = self.bridge()
        self.run_apply(b, {"anchors": ["FF0000"], "vivid": False, "device": 2})
        self.assertEqual([c.args[0] for c in b.update_leds.call_args_list], [2])
        b.hue.apply.assert_not_called()

    def test_no_colors_is_an_error_result(self):
        b = self.bridge()
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            b.apply({"anchors": ["bad"]})
        self.assertEqual(json.loads(out.getvalue())["ok"], False)


class CommandTest(unittest.TestCase):
    def test_off_routes_by_index_type(self):
        b = ob.Bridge()
        b.off = mock.Mock()
        b.handle_command(b'{"op":"off","device":3}')
        b.handle_command(b'{"op":"off","device":"hue:x"}')
        self.assertEqual([c.args[0] for c in b.off.call_args_list], [3, "hue:x"])

    def test_hue_op_configures_the_backend(self):
        b = ob.Bridge()
        b.hue.configure = mock.Mock()
        b.handle_command(b'{"op":"hue","enabled":false,"address":"1.2.3.4","room":"Office"}')
        b.hue.configure.assert_called_once_with(False, "1.2.3.4", "Office")

    def test_quit_stops_the_loop(self):
        b = ob.Bridge()
        b.hue.stop = mock.Mock()
        with self.assertRaises(SystemExit):
            b.handle_command(b'{"op":"quit"}')
        b.hue.stop.assert_called_once()

    def test_garbage_is_ignored(self):
        b = ob.Bridge()
        b.handle_command(b"not json")
        b.handle_command(b"[1,2]")
        b.handle_command(b'{"op":"nothing"}')


if __name__ == "__main__":
    unittest.main()
