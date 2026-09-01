// Pure functions shared by the service, the bar widget, and the panel.
.pragma library

function isHex(value) {
  return typeof value === "string" && /^[0-9a-fA-F]{6}$/.test(value)
}

// Parses `key = "#rrggbb"` lines from a theme's colors.toml into a map.
function paletteFrom(toml) {
  var map = {}
  String(toml || "").split("\n").forEach(function(line) {
    var m = line.match(/^\s*([A-Za-z_][\w-]*)\s*=\s*"#?([0-9a-fA-F]{6})"/)
    if (m) map[m[1]] = m[2].toUpperCase()
  })
  return map
}

// The anchor colors the gradient passes through: the palette values of the
// configured keys, in order, skipping keys the theme does not define.
function anchorsFrom(toml, keys) {
  var palette = paletteFrom(toml)
  var out = []
  String(keys || "").split(/[\s,]+/).forEach(function(key) {
    if (palette[key] !== undefined) out.push(palette[key])
  })
  if (out.length === 0 && palette["accent"] !== undefined) out.push(palette["accent"])
  return out
}

function lerpHex(a, b, t) {
  function chan(i) {
    var av = parseInt(a.substr(i, 2), 16)
    var bv = parseInt(b.substr(i, 2), 16)
    var v = Math.round(av + (bv - av) * t)
    return (v < 16 ? "0" : "") + v.toString(16).toUpperCase()
  }
  return chan(0) + chan(2) + chan(4)
}

// `count` colors interpolated through `anchors`.
function gradient(anchors, count) {
  if (!anchors || anchors.length === 0 || count <= 0) return []
  if (anchors.length === 1 || count === 1) {
    var solid = []
    for (var j = 0; j < count; j++) solid.push(anchors[0])
    return solid
  }
  var segs = anchors.length - 1
  var out = []
  for (var i = 0; i < count; i++) {
    var pos = i * segs / (count - 1)
    var seg = Math.min(Math.floor(pos), segs - 1)
    out.push(lerpHex(anchors[seg], anchors[seg + 1], pos - seg))
  }
  return out
}

// Up to `count` items sampled evenly across `arr`.
function sample(arr, count) {
  if (!arr || arr.length === 0 || count <= 0) return []
  if (arr.length <= count) return arr.slice()
  var out = []
  for (var i = 0; i < count; i++)
    out.push(arr[Math.round(i * (arr.length - 1) / (count - 1))])
  return out
}

// True when every color in the list is black — the device reads as off.
function allBlack(colors) {
  if (!colors || colors.length === 0) return false
  for (var i = 0; i < colors.length; i++)
    if (colors[i] !== "000000") return false
  return true
}
