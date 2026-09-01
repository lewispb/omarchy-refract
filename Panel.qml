import QtQuick
import Quickshell
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Refract's device panel: the theme gradient up top, then one row per device
// with its live colors as the server reports them. Each row has a re-apply
// and a power button; the header has the theme-follow toggle and a re-apply
// for everything. BarWidget.qml owns the bar dots and hands this panel the
// button to anchor against.
Panel {
  id: root
  moduleName: "io.github.lewispb.refract"
  ipcTarget: "io.github.lewispb.refract"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property var service: null
  readonly property var barIdentity: hostWidget || root

  readonly property bool connected: service ? service.connected : false
  readonly property var devices: service ? service.devices : []
  readonly property var themeAnchors: service ? service.themeAnchors : []
  readonly property bool themeSync: service ? service.themeSync : true
  readonly property var offDevices: service && service.offDevices ? service.offDevices : ({})
  readonly property string lastError: service ? service.lastError : ""

  readonly property var gradientStrip: Model.gradient(themeAnchors, 24)

  // Guarded so the widget renders before the bar is injected.
  readonly property color contentForeground: bar ? bar.foreground : Color.foreground
  readonly property string contentFontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property int panelWidth: Style.space(400)
  readonly property int rowHeight: Style.space(46)

  function open() {
    if (service) service.refresh()
    root.controller.show()
  }

  function close() {
    root.controller.hide()
  }

  function toggle() {
    if (root.opened) root.close()
    else root.open()
  }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  // Applied locally first so the panel redraws on the click itself; the
  // shell.json write comes back through the bar as the same value.
  function persistSettings(values) {
    var entry = { id: root.moduleName }
    for (var existing in root.settings) if (existing !== "id") entry[existing] = root.settings[existing]
    for (var key in values) entry[key] = values[key]

    root.settings = entry
    if (root.hostWidget && "settings" in root.hostWidget) root.hostWidget.settings = entry
    if (root.service && "settings" in root.service) root.service.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
  }

  function toggleThemeSync() {
    persistSettings({ themeSync: !root.themeSync })
  }

  function deviceIsOff(dev) {
    return offDevices[dev.name] === true || Model.allBlack(dev.colors)
  }

  function toggleDevicePower(dev) {
    if (!service) return
    var off = {}
    for (var name in offDevices) if (offDevices[name]) off[name] = true
    if (deviceIsOff(dev)) {
      delete off[dev.name]
      persistSettings({ off: off })
      service.applyDevice(dev.index)
    } else {
      off[dev.name] = true
      persistSettings({ off: off })
      service.deviceOff(dev.index)
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(root.panelWidth)
    contentHeight: panel.fittedContentHeight(content.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "a" || t === "A") { if (root.service) root.service.applyTheme() }
      }

      Column {
        id: content
        width: parent.width
        spacing: Style.space(10)

        // ---- Header: title, connection state, follow toggle, apply-all.
        Item {
          width: parent.width
          height: Style.space(24)

          Text {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: "REFRACT"
            color: Qt.darker(root.contentForeground, 1.5)
            font.family: root.contentFontFamily
            font.pixelSize: Style.font.bodySmall
            font.letterSpacing: 2
            font.bold: true
          }

          Row {
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            PanelActionButton {
              iconText: root.themeSync ? "󰄲" : "󰄱"
              tooltipText: root.themeSync ? "Following the theme — click to stop" : "Not following the theme — click to follow"
              foreground: root.themeSync ? Color.accent : root.contentForeground
              fontFamily: root.contentFontFamily
              onClicked: root.toggleThemeSync()
            }

            PanelActionButton {
              iconText: "󰑐"
              tooltipText: "Apply the gradient to every device (a)"
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              onClicked: if (root.service) root.service.applyTheme()
            }
          }
        }

        // ---- The gradient this theme produces, as one strip.
        Row {
          width: parent.width
          height: Style.space(14)

          Repeater {
            model: root.gradientStrip

            Rectangle {
              required property string modelData
              required property int index
              width: content.width / Math.max(1, root.gradientStrip.length)
              height: parent.height
              color: "#" + modelData
              // Only the strip's outer corners are rounded; radius on every
              // cell would cut notches into the run of color.
              radius: 0
            }
          }

          visible: root.gradientStrip.length > 0
        }

        // ---- Connection trouble, spelled out rather than a dead panel.
        Text {
          visible: !root.connected
          width: parent.width
          wrapMode: Text.Wrap
          text: root.lastError !== ""
            ? "OpenRGB unreachable: " + root.lastError
            : "Connecting to the OpenRGB server…"
          color: Qt.darker(root.contentForeground, 1.4)
          font.family: root.contentFontFamily
          font.pixelSize: Style.font.bodySmall
        }

        // ---- One row per device.
        Repeater {
          model: root.devices

          Item {
            id: row
            required property var modelData
            width: content.width
            height: root.rowHeight

            readonly property bool isOff: root.deviceIsOff(modelData)

            Column {
              anchors.left: parent.left
              anchors.right: rowButtons.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(4)

              Text {
                width: parent.width
                elide: Text.ElideRight
                text: row.modelData.name
                color: row.isOff ? Qt.darker(root.contentForeground, 1.8) : root.contentForeground
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.body
              }

              Row {
                spacing: Style.space(6)

                Row {
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(2)

                  Repeater {
                    model: row.modelData.colors

                    Rectangle {
                      required property string modelData
                      width: Style.space(10)
                      height: Style.space(10)
                      radius: Style.cornerRadius > 0 ? 2 : 0
                      color: "#" + modelData
                      border.width: 1
                      border.color: Qt.rgba(root.contentForeground.r, root.contentForeground.g, root.contentForeground.b, 0.15)
                    }
                  }
                }

                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  text: row.modelData.type + " · " + row.modelData.leds + " LEDs · " + row.modelData.activeMode
                  color: Qt.darker(root.contentForeground, 1.6)
                  font.family: root.contentFontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }

            Row {
              id: rowButtons
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: 0

              PanelActionButton {
                iconText: "󰑐"
                tooltipText: "Re-apply the gradient to this device"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                onClicked: if (root.service) root.service.applyDevice(row.modelData.index)
              }

              PanelActionButton {
                iconText: "󰐥"
                tooltipText: row.isOff ? "Switch this device back on" : "Switch this device off"
                foreground: row.isOff ? Qt.darker(root.contentForeground, 1.8) : root.contentForeground
                fontFamily: root.contentFontFamily
                onClicked: root.toggleDevicePower(row.modelData)
              }
            }
          }
        }
      }
    }
  }
}
