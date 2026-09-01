import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Bar widget for omarchy-rgb-sync. Shows a row of dots sampled from the
// gradient the last sync applied, so the bar carries the same colors as the
// hardware. The tooltip reports the theme, device count, and time of the
// last run. Left click re-applies the sync.
BarWidget {
  id: root
  moduleName: "io.github.lewispb.rgb-sync"

  property var status: ({})

  readonly property var gradient: status && status.gradient ? status.gradient : []
  readonly property int deviceCount: status && status.devices ? status.devices.length : 0
  readonly property bool ok: status && status.result === "ok"

  // Up to five dots, sampled evenly across the gradient.
  readonly property var swatches: {
    var g = root.gradient
    if (!g || g.length === 0) return []
    var count = Math.min(5, g.length)
    var out = []
    for (var i = 0; i < count; i++)
      out.push(g[Math.round(i * (g.length - 1) / Math.max(1, count - 1))])
    return out
  }

  readonly property string tooltip: {
    if (!status || !status.time) return "rgb-sync has not run yet"
    var when = String(status.time).replace("T", " ").slice(0, 16)
    var head = ok
      ? (status.theme + " on " + deviceCount + " devices at " + when)
      : (String(status.result) + " at " + when)
    return head + " — click to re-apply"
  }

  readonly property real dotSize: Math.max(5, Math.round(Style.bar.iconSlot * 0.28))
  readonly property real dotSpacing: Math.max(3, Math.round(dotSize * 0.55))
  readonly property int dotCount: Math.max(1, swatches.length)

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function parse(raw) {
    try { root.status = JSON.parse(raw) } catch (e) { root.status = {} }
  }

  FileView {
    id: statusFile
    path: Quickshell.env("HOME") + "/.local/state/omarchy-rgb-sync/status.json"
    watchChanges: true
    printErrors: false
    onLoaded: root.parse(text())
    onFileChanged: reload()
    onLoadFailed: root.status = {}
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    hasVisualContent: true
    tooltipText: root.tooltip
    fixedWidth: root.vertical
      ? -1
      : root.dotCount * root.dotSize + (root.dotCount - 1) * root.dotSpacing + 17.5
    fixedHeight: root.vertical
      ? root.dotCount * root.dotSize + (root.dotCount - 1) * root.dotSpacing + 17.5
      : -1

    onPressed: function(b) {
      if (b === Qt.LeftButton)
        root.bar.run(Quickshell.env("HOME") + "/.config/omarchy/hooks/theme-set.d/rgb-sync")
    }

    Grid {
      anchors.centerIn: parent
      columns: root.vertical ? 1 : root.dotCount
      spacing: root.dotSpacing

      Repeater {
        model: root.swatches.length > 0 ? root.swatches : [""]

        Rectangle {
          required property string modelData
          width: root.dotSize
          height: root.dotSize
          radius: root.dotSize / 2
          color: modelData !== "" ? "#" + modelData : "transparent"
          border.width: modelData !== "" ? 0 : 1
          border.color: button.foreground
          opacity: root.ok || root.swatches.length === 0 ? 1 : 0.4
        }
      }
    }
  }
}
