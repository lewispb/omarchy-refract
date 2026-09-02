import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "Model.js" as Model

// Headless owner of the OpenRGB connection and the theme watcher. The shell
// creates one of these per plugin; bar surfaces read it through
// `bar.shell.serviceFor(id)` instead of spawning their own bridge.
//
// Talks to the hardware through bridge/openrgb_bridge.py: JSON commands in
// on stdin, JSON events out on stdout. The bridge speaks the OpenRGB SDK
// binary protocol and the Philips Hue bridge's HTTPS API, sets one color per
// LED, and polls for changes made by other clients, so `devices` mirrors
// what the server and the bridge report.
//
// The theme watcher reads the active theme's colors.toml. When it changes —
// which is what `omarchy theme set` causes — the gradient is rebuilt from the
// configured anchor keys and re-applied to every device not switched off.
Item {
  id: root

  // Injected by the shell when the service is created.
  property var shell: null
  property var manifest: null

  // The bar widget's inline shell.json entry, pushed by the widget — the
  // shell injects settings into bar widgets, not services.
  property var settings: ({})

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  // ---- State read by the widget and panel
  property bool bridgeReady: false
  property bool openrgbConnected: false
  // disabled, searching, unpaired, connected, or unreachable; see hue.py.
  property string hueStatus: "disabled"
  property string hueMessage: ""
  // Room and zone names the bridge reports, for the panel's room picker.
  property var hueRooms: []
  readonly property bool hueConnected: hueStatus === "connected"
  // Something is there to receive colors.
  readonly property bool connected: openrgbConnected || hueConnected
  property var devices: []
  property string lastError: ""
  property string openrgbBinary: ""
  property bool serverStartAttempted: false

  // Which devices the server reports, ignoring their colors. The gradient is
  // re-applied when this changes: `openrgb --server` accepts clients before
  // device detection finishes, so a connection made at login sees an empty
  // list that fills in over the next few seconds, and devices plugged in
  // later arrive the same way.
  property string deviceSignature: ""

  readonly property string host: String(setting("host", "127.0.0.1") || "127.0.0.1")
  readonly property int port: Math.max(1, Math.min(65535, Number(setting("port", 6742)) || 6742))
  readonly property bool themeSync: setting("themeSync", true) !== false
  readonly property string style: String(setting("style", "gradient")) === "solid" ? "solid" : "gradient"
  readonly property string anchorKeys: String(setting("anchors", "accent magenta cyan blue"))
  readonly property bool vivid: setting("vivid", true) !== false
  readonly property bool hueEnabled: setting("hue", true) !== false
  readonly property string hueBridge: String(setting("hueBridge", "") || "")
  // A Hue room or zone name; blank syncs every color-capable light.
  readonly property string hueRoom: String(setting("hueRoom", "") || "")

  // Devices switched off from the panel, by name: { "<device name>": true }.
  // The panel owns writing this; theme sync leaves these dark.
  readonly property var offDevices: setting("off", ({})) || ({})

  // Which theme color each device's gradient starts from, by name:
  // { "<device name>": N }. Clicking a row's swatches in the panel advances
  // it; a one-LED lamp steps through the anchors one at a time.
  readonly property var offsets: setting("offsets", ({})) || ({})

  readonly property int deviceCount: devices.length

  // ---- Theme palette
  property string colorsToml: ""
  readonly property var themeAnchors: Model.anchorsFrom(colorsToml, anchorKeys)

  readonly property string bridgePath: Qt.resolvedUrl("bridge/openrgb_bridge.py").toString().replace(/^file:\/\//, "")

  FileView {
    id: colorsFile
    path: Quickshell.env("HOME") + "/.local/state/omarchy/current/theme/colors.toml"
    watchChanges: true
    printErrors: false
    onLoaded: root.colorsToml = text()
    onFileChanged: reload()
    onLoadFailed: root.colorsToml = ""
  }

  // The shell never re-reads colors.toml on a theme switch — omarchy-theme-set
  // pushes the new colors over shell IPC — and the switch swaps the staged
  // theme directory, which replaces the file's inode and silences the file
  // watch above after the first switch. The Color singleton's properties do
  // change on every switch, so they are the reload trigger; the file itself
  // still carries the full palette the gradient needs.
  readonly property string themeSignature: String(Color.accent) + String(Color.background) + String(Color.foreground)
  onThemeSignatureChanged: themeReloadTimer.restart()

  // Delayed so the reload reads the new theme's file after the staging swap
  // finishes, not the moment the first IPC-pushed color lands.
  Timer {
    id: themeReloadTimer
    interval: 500
    onTriggered: colorsFile.reload()
  }

  // ---- Bridge I/O

  function send(obj) {
    if (!bridge.running) return false
    bridge.write(JSON.stringify(obj) + "\n")
    return true
  }

  function handleLine(line) {
    var trimmed = String(line || "").trim()
    if (trimmed === "") return
    var msg
    try {
      msg = JSON.parse(trimmed)
    } catch (e) {
      console.warn("refract: unreadable bridge line: " + trimmed)
      return
    }
    switch (msg.event) {
    case "hello":
      bridgeReady = true
      openrgbBinary = msg.openrgbBinary ? String(msg.openrgbBinary) : ""
      connectNow()
      configureHue()
      break
    case "state":
      applyState(msg)
      break
    case "result":
      if (msg.ok === false) lastError = String(msg.error || "OpenRGB rejected the command")
      if (msg.op === "start_server") console.log("refract: " + (msg.started ? "started openrgb --server" : "did not start a server: " + msg.reason))
      break
    }
  }

  function applyState(msg) {
    openrgbConnected = msg.connected === true
    hueStatus = msg.hue && msg.hue.status ? String(msg.hue.status) : "disabled"
    hueMessage = msg.hue && msg.hue.message ? String(msg.hue.message) : ""
    hueRooms = msg.hue && Array.isArray(msg.hue.rooms) ? msg.hue.rooms : []
    // The bridge lists OpenRGB devices only while connected, so the list
    // holds Hue lights alone when the server is down.
    devices = Array.isArray(msg.devices) ? msg.devices : []
    var signature = devices.map(function(d) { return d.index + ":" + d.name + ":" + d.leds }).join("|")
    if (signature !== deviceSignature) {
      deviceSignature = signature
      if (devices.length > 0) scheduleApply()
    }
    if (openrgbConnected) {
      lastError = ""
    } else {
      lastError = String(msg.error || "")
      maybeStartServer()
      // Not restarted: Hue state changes arrive as state events too, and
      // each one would push the next OpenRGB attempt further out.
      if (!reconnectTimer.running) reconnectTimer.start()
    }
  }

  function connectNow() {
    send({ op: "connect", host: host, port: port })
  }

  function configureHue() {
    send({ op: "hue", enabled: hueEnabled, address: hueBridge, room: hueRoom })
  }

  onHueEnabledChanged: configureHue()
  onHueBridgeChanged: configureHue()
  onHueRoomChanged: configureHue()

  // At most once per shell session, and only when no openrgb process exists.
  // A systemd unit and the shell start in the same second at login, and the
  // server does not listen until it has finished detecting devices, so the
  // first connect fails while a server is already starting. Two servers
  // detect the same hardware at once and the second to bind the port exits;
  // if that is the unit's process, systemd records a failure. The bridge
  // checks the process table before spawning.
  function maybeStartServer() {
    if (!setting("autoStartServer", true)) return
    if (serverStartAttempted || openrgbBinary === "") return
    serverStartAttempted = true
    serverStartTimer.restart()
  }

  // The check runs a moment after the failed connect so a server that
  // systemd is in the middle of starting or restarting is in the process
  // table by the time the bridge looks.
  Timer {
    id: serverStartTimer
    interval: 2000
    onTriggered: root.send({ op: "start_server" })
  }

  Timer {
    id: reconnectTimer
    interval: 5000
    onTriggered: root.connectNow()
  }

  // ---- Theme sync

  function scheduleApply() {
    if (themeSync && connected && themeAnchors.length > 0) applyTimer.restart()
  }

  // Debounced: a theme switch rewrites colors.toml and the file watcher can
  // fire more than once for it, and a starting server reports its devices
  // one detector at a time.
  Timer {
    id: applyTimer
    interval: 600
    onTriggered: root.applyTheme()
  }

  onThemeAnchorsChanged: scheduleApply()
  onThemeSyncChanged: if (themeSync) scheduleApply()
  onStyleChanged: scheduleApply()
  onVividChanged: scheduleApply()

  function applyTheme() {
    if (!connected || themeAnchors.length === 0) return
    var exclude = []
    for (var name in offDevices) if (offDevices[name]) exclude.push(name)
    send({ op: "apply", anchors: themeAnchors, style: style, vivid: vivid, exclude: exclude, offsets: offsets })
  }

  function applyDevice(index) {
    if (!connected || themeAnchors.length === 0) return
    send({ op: "apply", anchors: themeAnchors, style: style, vivid: vivid, device: index, offsets: offsets })
  }

  function deviceOff(index) {
    send({ op: "off", device: index })
  }

  function refresh() {
    send({ op: "refresh" })
  }

  // /usr/bin/python3 rather than python3: version managers put their own
  // python3 on PATH, and the bridge needs the system interpreter that is
  // always present on Omarchy.
  Process {
    id: bridge
    command: ["/usr/bin/python3", root.bridgePath]
    running: true
    stdinEnabled: true

    stdout: SplitParser {
      onRead: function(segment) { root.handleLine(segment) }
    }

    onExited: {
      root.bridgeReady = false
      root.openrgbConnected = false
      root.hueStatus = "disabled"
      root.hueMessage = ""
      root.hueRooms = []
      root.devices = []
      root.deviceSignature = ""
      restartTimer.restart()
    }
  }

  Timer {
    id: restartTimer
    interval: 3000
    onTriggered: bridge.running = true
  }
}
