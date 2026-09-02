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
  readonly property var ringColors: Model.gradient(anchors2, 8)
  readonly property bool connected: service ? service.connected : false

  readonly property string tooltip: {
    if (!service) return "Refract"
    if (!connected) return "No OpenRGB server or Hue bridge — " + (service.lastError || "waiting")
    return service.deviceCount + (service.deviceCount === 1 ? " device" : " devices") + " on the theme gradient — click for the panel"
  }

  // The logo's mark at bar scale: eight dots on a ring, colored along the
  // gradient, with the first anchor at the center. Sized like the other
  // icon glyphs (see TailscaleIcon).
  readonly property real ringSize: Style.font.icon
  readonly property real ringDot: Math.max(2.5, ringSize * 0.26)
  readonly property real centerDot: Math.max(3, ringSize * 0.34)

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
    fixedWidth: root.vertical ? -1 : root.ringSize + 14
    fixedHeight: root.vertical ? Style.bar.iconSlot : -1

    onPressed: function(b) {
      if (b === Qt.RightButton) {
        if (root.service) root.service.applyTheme()
      } else {
        root.togglePanel()
      }
    }

    Item {
      id: ring
      width: root.ringSize
      height: root.ringSize
      anchors.centerIn: parent
      opacity: root.connected ? 1 : 0.35

      // No palette yet: the ring outline alone, in the bar foreground.
      Rectangle {
        visible: root.ringColors.length === 0
        anchors.fill: parent
        radius: width / 2
        color: "transparent"
        border.width: 1
        border.color: button.foreground
      }

      Repeater {
        model: root.ringColors

        Rectangle {
          required property string modelData
          required property int index
          readonly property real angle: index * Math.PI / 4 - Math.PI / 2
          readonly property real orbit: (ring.width - width) / 2
          x: orbit + Math.cos(angle) * orbit
          y: orbit + Math.sin(angle) * orbit
          width: root.ringDot
          height: root.ringDot
          radius: width / 2
          color: "#" + modelData
        }
      }

      Rectangle {
        visible: root.ringColors.length > 0
        anchors.centerIn: parent
        width: root.centerDot
        height: root.centerDot
        radius: width / 2
        color: root.ringColors.length > 0 ? "#" + root.ringColors[0] : "transparent"
      }
    }
  }
}
