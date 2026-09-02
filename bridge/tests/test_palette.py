import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from palette import gradient, hex_to_xy, lerp_hex, rotate, spread, vivid, xy_to_hex  # noqa: E402


class VividTest(unittest.TestCase):
    def test_lifts_saturation_of_a_muted_color(self):
        muted = "8090A0"
        lifted = vivid(muted)
        # The channel spread is what saturation is; lifting it widens the gap.
        def spread_of(h):
            chans = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
            return max(chans) - min(chans)
        self.assertGreater(spread_of(lifted), spread_of(muted))

    def test_saturated_primary_is_unchanged(self):
        self.assertEqual(vivid("FF0000"), "FF0000")

    def test_gray_stays_gray(self):
        self.assertEqual(vivid("808080"), "8D8D8D")  # value lifted, no hue introduced
        r, g, b = vivid("808080")[0:2], vivid("808080")[2:4], vivid("808080")[4:6]
        self.assertEqual(r, g)
        self.assertEqual(g, b)

    def test_white_is_clamped(self):
        self.assertEqual(vivid("FFFFFF"), "FFFFFF")


class LerpTest(unittest.TestCase):
    def test_endpoints(self):
        self.assertEqual(lerp_hex("000000", "FFFFFF", 0), "000000")
        self.assertEqual(lerp_hex("000000", "FFFFFF", 1), "FFFFFF")

    def test_midpoint_rounds(self):
        self.assertEqual(lerp_hex("000000", "FFFFFF", 0.5), "808080")
        self.assertEqual(lerp_hex("FF0000", "0000FF", 0.5), "800080")


class GradientTest(unittest.TestCase):
    def test_empty_inputs(self):
        self.assertEqual(gradient([], 5), [])
        self.assertEqual(gradient(["FF0000"], 0), [])

    def test_single_anchor_repeats(self):
        self.assertEqual(gradient(["FF0000"], 3), ["FF0000"] * 3)

    def test_count_one_takes_first_anchor(self):
        self.assertEqual(gradient(["FF0000", "00FF00"], 1), ["FF0000"])

    def test_endpoints_are_the_anchors(self):
        out = gradient(["FF0000", "00FF00", "0000FF"], 9)
        self.assertEqual(len(out), 9)
        self.assertEqual(out[0], "FF0000")
        self.assertEqual(out[4], "00FF00")
        self.assertEqual(out[-1], "0000FF")

    def test_two_leds_get_the_outer_anchors(self):
        self.assertEqual(gradient(["FF0000", "00FF00", "0000FF"], 2), ["FF0000", "0000FF"])


class RotateTest(unittest.TestCase):
    ANCHORS = ["A", "B", "C", "D"]

    def test_zero_and_full_turn_are_identity(self):
        self.assertEqual(rotate(self.ANCHORS, 0), self.ANCHORS)
        self.assertEqual(rotate(self.ANCHORS, 4), self.ANCHORS)

    def test_offset_moves_the_start(self):
        self.assertEqual(rotate(self.ANCHORS, 1), ["B", "C", "D", "A"])
        self.assertEqual(rotate(self.ANCHORS, 6), ["C", "D", "A", "B"])

    def test_bad_offsets_are_tolerated(self):
        self.assertEqual(rotate(self.ANCHORS, None), self.ANCHORS)
        self.assertEqual(rotate([], 3), [])


class SpreadTest(unittest.TestCase):
    ANCHORS = ["AA0000", "00AA00", "0000AA", "AAAAAA"]

    def test_small_device_blends_only_the_first_two_anchors(self):
        out = spread(self.ANCHORS, 4)
        self.assertEqual(out[0], "AA0000")
        self.assertEqual(out[-1], "00AA00")

    def test_large_device_gets_the_whole_palette(self):
        out = spread(self.ANCHORS, 10)
        self.assertEqual(out[0], "AA0000")
        self.assertEqual(out[-1], "AAAAAA")
        self.assertIn("0000AA", out)

    def test_one_led_steps_through_the_anchors_by_offset(self):
        self.assertEqual([spread(self.ANCHORS, 1, k)[0] for k in range(5)],
                         ["AA0000", "00AA00", "0000AA", "AAAAAA", "AA0000"])


class XyTest(unittest.TestCase):
    def test_black_has_no_chromaticity(self):
        self.assertIsNone(hex_to_xy("000000"))

    def test_known_values(self):
        self.assertEqual(hex_to_xy("FF0000"), (0.7006, 0.2993))
        self.assertEqual(hex_to_xy("FFFFFF"), (0.3227, 0.329))

    def test_primaries_round_trip(self):
        for h in ("FF0000", "00FF00", "0000FF", "FFFFFF"):
            self.assertEqual(xy_to_hex(*hex_to_xy(h)), h)

    def test_round_trip_keeps_the_hue_at_full_brightness(self):
        # xy carries no brightness, so a dark blue comes back as a bright one.
        self.assertEqual(xy_to_hex(*hex_to_xy("1E66F5")), "206AFF")

    def test_degenerate_xy_is_black(self):
        self.assertEqual(xy_to_hex(0.3, 0), "000000")
        self.assertEqual(xy_to_hex(0.3, -1), "000000")


if __name__ == "__main__":
    unittest.main()
