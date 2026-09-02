"""Color helpers shared by the OpenRGB and Philips Hue backends."""

import colorsys


def vivid(hexc):
    """Lift saturation and value for hardware-bound colors.

    LEDs and lamps render mid-saturation screen colors as washed out. Screen
    surfaces (the panel, the bar) keep the exact theme colors.
    """
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
    if not anchors or count <= 0:
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


def rotate(anchors, offset):
    """`anchors` starting from the color `offset` places along.

    A device's saved offset picks which theme color its gradient starts
    from; for a one-LED device that is the whole choice.
    """
    if not anchors:
        return []
    k = int(offset or 0) % len(anchors)
    return anchors[k:] + anchors[:k]


def spread(anchors, count, offset=0):
    """The colors for a device with `count` LEDs.

    A device with few LEDs gets the first two anchors rather than the whole
    palette: four diodes showing four hues reads as noise, not a gradient.
    """
    anchors = rotate(anchors, offset)
    use = anchors if count >= 10 else anchors[:2]
    return gradient(use, count)


# ---- CIE xy, the color space Hue lights take.
#
# The matrices are the Wide RGB D65 conversion from Signify's developer
# documentation. The bridge clamps xy to each light's gamut, so no clamping
# is done here.

def _expand(c):
    return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92


def _compress(c):
    c = max(0.0, min(1.0, c))
    return 1.055 * (c ** (1 / 2.4)) - 0.055 if c > 0.0031308 else 12.92 * c


def hex_to_xy(hexc):
    """CIE xy for a color, or None for black, which has no chromaticity."""
    r = _expand(int(hexc[0:2], 16) / 255)
    g = _expand(int(hexc[2:4], 16) / 255)
    b = _expand(int(hexc[4:6], 16) / 255)
    x = r * 0.664511 + g * 0.154324 + b * 0.162028
    y = r * 0.283881 + g * 0.668433 + b * 0.047685
    z = r * 0.000088 + g * 0.072310 + b * 0.986039
    total = x + y + z
    if total <= 0:
        return None
    return (round(x / total, 4), round(y / total, 4))


def xy_to_hex(x, y):
    """The color at full brightness for a CIE xy pair, as RRGGBB."""
    if y <= 0:
        return "000000"
    X = x / y
    Y = 1.0
    Z = (1 - x - y) / y
    r = X * 1.656492 - Y * 0.354851 - Z * 0.255038
    g = -X * 0.707196 + Y * 1.655397 + Z * 0.036152
    b = X * 0.051713 - Y * 0.121364 + Z * 1.011530
    r, g, b = (max(0.0, c) for c in (r, g, b))
    peak = max(r, g, b)
    if peak <= 0:
        return "000000"
    r, g, b = (_compress(c / peak) for c in (r, g, b))
    return "%02X%02X%02X" % (round(r * 255), round(g * 255), round(b * 255))
