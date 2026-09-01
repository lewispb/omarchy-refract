import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "Model.js" as Model

// Headless owner of the OpenRGB connection and the theme watcher. The shell
// creates one of these per plugin; bar surfaces read it through
// `bar.shell.serviceFor(id)` instead of spawning their own bridge.
//
// Talks to OpenRGB through bridge/openrgb_bridge.py: JSON commands in on
// stdin, JSON events out on stdout. The bridge speaks the SDK binary
// protocol, sets one color per LED, and polls for changes made by other
// clients, so `devices` mirrors what the server reports.
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
  property bool connected: false
  property var devices: []
  property string lastError: ""
  property string openrgbBinary: ""
  property bool serverStartAttempted: false

  readonly property string host: String(setting("host", "127.0.0.1") || "127.0.0.1")
  readonly property int port: Math.max(1, Math.min(65535, Number(setting("port", 6742)) || 6742))
  readonly property bool themeSync: setting("themeSync", true) !== false
  readonly property string style: String(setting("style", "gradient")) === "solid" ? "solid" : "gradient"
  readonly property string anchorKeys: String(setting("anchors", "accent magenta cyan blue"))

  // Devices switched off from the panel, by name: { "<device name>": true }.
  // The panel owns writing this; theme sync leaves these dark.
  readonly property var offDevices: setting("off", ({})) || ({})

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
      break
    case "state":
      applyState(msg)
      break
    case "result":
      if (msg.ok === false) lastError = String(msg.error || "OpenRGB rejected the command")
      break
    }
  }

  function applyState(msg) {
    var wasConnected = connected
    if (msg.connected) {
      connected = true
      lastError = ""
      devices = Array.isArray(msg.devices) ? msg.devices : []
      if (!wasConnected) scheduleApply()
    } else {
      connected = false
      devices = []
      lastError = String(msg.error || "")
      maybeStartServer()
      reconnectTimer.restart()
    }
  }

  function connectNow() {
    send({ op: "connect", host: host, port: port })
  }

  // Once per shell session: spawning openrgb on every failed connect would
  // fight a server the user is starting by hand or through systemd.
  function maybeStartServer() {
    if (!setting("autoStartServer", true)) return
    if (serverStartAttempted || openrgbBinary === "") return
    serverStartAttempted = true
    Quickshell.execDetached([openrgbBinary, "--server"])
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
  // fire more than once for it.
  Timer {
    id: applyTimer
    interval: 600
    onTriggered: root.applyTheme()
  }

  onThemeAnchorsChanged: scheduleApply()
  onThemeSyncChanged: if (themeSync) scheduleApply()
  onStyleChanged: scheduleApply()

  function applyTheme() {
    if (!connected || themeAnchors.length === 0) return
    var exclude = []
    for (var name in offDevices) if (offDevices[name]) exclude.push(name)
    send({ op: "apply", anchors: themeAnchors, style: style, exclude: exclude })
  }

  function applyDevice(index) {
    if (!connected || themeAnchors.length === 0) return
    send({ op: "apply", anchors: themeAnchors, style: style, device: index })
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
      root.connected = false
      root.devices = []
      restartTimer.restart()
    }
  }

  Timer {
    id: restartTimer
    interval: 3000
    onTriggered: bridge.running = true
  }
}
