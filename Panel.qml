import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

Panel {
  id: root
  moduleName: "io.github.f0tia.mesh-chat"
  ipcTarget: "io.github.f0tia.mesh-chat"
  manageIpc: false

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color accent: Commons.Color.accent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color barIconColor: mesh.connected ? barForeground : Qt.darker(barForeground, 1.55)
  readonly property int composeBytes: mesh.utf8Length(compose.text)

  function timeLabel(ts) {
    if (!ts) return ""
    var d = new Date(ts * 1000)
    var today = new Date()
    var hm = Qt.formatTime(d, "HH:mm")
    return d.toDateString() === today.toDateString() ? hm : Qt.formatDate(d, "MMM d") + " " + hm
  }

  function agoLabel(ts) {
    if (!ts) return ""
    var s = Math.max(0, Math.floor(Date.now() / 1000) - ts)
    if (s < 60) return "now"
    if (s < 3600) return Math.floor(s / 60) + "m"
    if (s < 86400) return Math.floor(s / 3600) + "h"
    return Math.floor(s / 86400) + "d"
  }

  function statusGlyph(m) {
    if (m.dir !== "out") return ""
    if (m.status === "delivered") return "✓✓"
    if (m.status === "sent") return "✓"
    if (m.status === "failed") return "✗"
    return "…"
  }

  function messageMeta(m) {
    var parts = [timeLabel(m.ts)]
    if (m.dir === "in") {
      if (m.snr !== undefined && m.snr !== null) parts.push("SNR " + m.snr)
      if (m.rssi !== undefined && m.rssi !== null) parts.push(m.rssi + " dBm")
      if (m.hops !== undefined && m.hops !== null) parts.push(m.hops === 0 ? "direct" : m.hops + " hop" + (m.hops === 1 ? "" : "s"))
    } else {
      parts.push(statusGlyph(m))
    }
    return parts.join(" · ")
  }

  function sendCompose() {
    if (mesh.send(compose.text)) compose.text = ""
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: mesh.viewing = opened

  MeshService {
    id: mesh
    settings: root.settings
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { mesh.refresh(); return "ok" }
    function status(): string { return mesh.statusText() }
    function send(conv: string, text: string): string {
      mesh.activeConv = conv
      return mesh.send(text) ? "ok" : "not sent"
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    tooltipText: "Mesh Chat · " + mesh.statusText()
    iconComponent: Component {
      Item {
        MeshIcon {
          anchors.centerIn: parent
          iconSize: Style.space(12)
          color: root.barIconColor
          badgeColor: root.urgent
          online: mesh.connected
          badge: mesh.unreadTotal > 0
        }
      }
    }
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.MiddleButton) mesh.refresh()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: compose
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(640))

    Column {
      id: column
      anchors.left: parent.left
      anchors.right: parent.right
      spacing: Style.space(10)

      PanelHero {
        width: parent.width
        title: mesh.selfName || "Mesh Chat"
        meta: mesh.statusText()
        foreground: root.foreground
        fontFamily: root.fontFamily
        iconOpacity: mesh.connected ? 1.0 : 0.5
        iconComponent: Component {
          MeshIcon {
            iconSize: Style.font.display
            color: root.foreground
            online: mesh.connected
          }
        }
      }

      // First run: libraries aren't installed yet.
      Column {
        visible: mesh.linkState === "missing-deps"
        width: parent.width
        spacing: Style.space(6)

        Text {
          width: parent.width
          wrapMode: Text.Wrap
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          text: "Mesh Chat needs the meshtastic and meshcore Python libraries. They install into a private venv in ~/.local/share/omarchy-mesh, without sudo."
        }

        Button {
          text: mesh.settingUp ? "Installing…" : "Install libraries"
          enabled: !mesh.settingUp
          bordered: true
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: mesh.runSetup()
        }

        Text {
          visible: mesh.setupLog.length > 0
          width: parent.width
          wrapMode: Text.WrapAnywhere
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          text: mesh.setupLog.slice(-8).join("\n")
        }
      }

      Dropdown {
        width: parent.width
        showLabel: false
        value: mesh.activeConv
        options: mesh.conversations
        foreground: root.foreground
        fontFamily: root.fontFamily
        onChanged: function(value) { mesh.activeConv = value }
      }

      Rectangle {
        width: parent.width
        height: Style.space(300)
        color: "transparent"
        radius: Style.space(4)
        border.width: Style.normalBorderWidth
        border.color: Qt.darker(root.foreground, 2.6)

        Text {
          anchors.centerIn: parent
          visible: mesh.activeMessages.length === 0
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          text: mesh.connected ? "No messages yet" : "Plug in or connect a radio"
        }

        ListView {
          id: messageList
          anchors.fill: parent
          anchors.margins: Style.space(8)
          clip: true
          spacing: Style.space(8)
          model: mesh.activeMessages
          boundsBehavior: Flickable.StopAtBounds
          ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
          onCountChanged: Qt.callLater(positionViewAtEnd)
          Component.onCompleted: positionViewAtEnd()

          delegate: Item {
            id: row
            required property var modelData
            readonly property bool mine: modelData.dir === "out"
            width: messageList.width
            implicitHeight: bubble.height

            Rectangle {
              id: bubble
              anchors.right: row.mine ? parent.right : undefined
              anchors.left: row.mine ? undefined : parent.left
              width: Math.min(row.width * 0.82, Math.max(body.implicitWidth, meta.implicitWidth, sender.implicitWidth) + Style.space(16))
              height: content.implicitHeight + Style.space(12)
              radius: Style.space(6)
              color: row.mine ? Qt.alpha(root.accent, 0.22) : Qt.alpha(root.foreground, 0.08)
              border.width: row.modelData.status === "failed" ? 1 : 0
              border.color: root.urgent

              Column {
                id: content
                x: Style.space(8)
                y: Style.space(6)
                width: bubble.width - Style.space(16)
                spacing: Style.space(2)

                Text {
                  id: sender
                  visible: !row.mine
                  text: row.modelData.fromName || row.modelData.from || ""
                  color: root.accent
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                }
                Text {
                  id: body
                  width: Math.min(implicitWidth, content.width)
                  text: row.modelData.text
                  wrapMode: Text.Wrap
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  textFormat: Text.PlainText
                }
                Text {
                  id: meta
                  text: root.messageMeta(row.modelData)
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }
          }
        }
      }

      RowLayout {
        width: parent.width
        spacing: Style.space(6)

        TextField {
          id: compose
          Layout.fillWidth: true
          foreground: root.foreground
          enabled: mesh.connected
          placeholderText: mesh.connected ? "Message " + mesh.convTitle(mesh.activeConv) : "Radio not connected"
          onAccepted: root.sendCompose()
          Keys.onEscapePressed: root.close()
        }

        Text {
          text: root.composeBytes + "/" + mesh.maxBytes
          color: root.composeBytes > mesh.maxBytes ? root.urgent : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
      }

      Text {
        visible: mesh.notice !== ""
        width: parent.width
        wrapMode: Text.Wrap
        text: mesh.notice
        color: root.urgent
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }

      PanelSectionHeader {
        visible: mesh.nodes.length > 0
        width: parent.width
        foreground: root.foreground
        fontFamily: root.fontFamily
        text: "Nodes heard (" + mesh.nodes.length + ") · click to message"
      }

      Repeater {
        model: mesh.nodes.slice(0, 8)

        delegate: Button {
          required property var modelData
          width: column.width
          leftAlign: true
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          selected: mesh.activeConv === "dm:" + modelData.id
          text: modelData.name
                + (modelData.short ? "  (" + modelData.short + ")" : "")
                + (modelData.lastHeard ? "  · " + root.agoLabel(modelData.lastHeard) : "")
                + (modelData.hops !== null && modelData.hops !== undefined ? "  · " + modelData.hops + " hop" + (modelData.hops === 1 ? "" : "s") : "")
          onClicked: {
            mesh.activeConv = "dm:" + modelData.id
            compose.forceActiveFocus()
          }
        }
      }

      RowLayout {
        width: parent.width
        spacing: Style.space(6)

        Button {
          text: "Refresh"
          bordered: true
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          enabled: mesh.connected
          onClicked: mesh.refresh()
        }
        Button {
          visible: mesh.protocol === "meshcore"
          text: "Send advert"
          bordered: true
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          enabled: mesh.connected
          onClicked: mesh.advert()
        }
        Item { Layout.fillWidth: true }
        Text {
          text: mesh.protocolLabel
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }
  }
}
