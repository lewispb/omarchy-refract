import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Refract's bar presence: a row of dots sampled from the gradient the
// current theme produces, dimmed while OpenRGB is unreachable. Left click
// opens the device panel; right click re-applies the gradient.
BarWidget {
  id: root
  moduleName: "io.github.lewispb.refract"

  readonly property var service: bar && bar.shell && typeof bar.shell.serviceFor === "function"
    ? bar.shell.serviceFor(moduleName)
    : null

  // The shell injects settings into bar widgets, not services; push them
  // through so the service reads the same entry.
  onSettingsChanged: if (service && "settings" in service) service.settings = settings
  onServiceChanged: if (service && "settings" in service) service.settings = settings

  readonly property var anchors2: service ? service.themeAnchors : []
  readonly property var swatches: Model.gradient(anchors2, 5)
  readonly property bool connected: service ? service.connected : false

  readonly property string tooltip: {
    if (!service) return "Refract"
    if (!connected) return "OpenRGB unreachable — " + (service.lastError || "waiting for the server")
    return service.deviceCount + " devices on the theme gradient — click for the panel"
  }

  readonly property real dotSize: Math.max(5, Math.round(Style.bar.iconSlot * 0.28))
  readonly property real dotSpacing: Math.max(3, Math.round(dotSize * 0.55))
  readonly property int dotCount: Math.max(1, swatches.length)

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // ---- Panel popout. Shape contract for shell.summon/hide/toggle routing:
  //      Bar.findPanelWidget requires open/close/opened on the widget root.
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false

  function open() {
    if (panelLoader.item) panelLoader.item.open()
  }

  function close() {
    if (panelLoader.item) panelLoader.item.close()
  }

  function togglePanel() {
    if (panelLoader.item) panelLoader.item.toggle()
  }

  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
    if ("service" in target) target.service = root.service
  }

  onBarChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  IpcHandler {
    target: "io.github.lewispb.refract"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.togglePanel() }
    function apply(): void { if (root.service) root.service.applyTheme() }
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
      if (b === Qt.RightButton) {
        if (root.service) root.service.applyTheme()
      } else {
        root.togglePanel()
      }
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
          opacity: root.connected ? 1 : 0.35
        }
      }
    }
  }
}
