import QtQuick
import qs.Commons
import qs.Commons as Commons

// Radio mast with two signal arcs either side. Arcs fade when the radio is
// offline; a badge dot marks unread messages.
Item {
  id: root

  property real iconSize: Style.font.icon
  property color color: Commons.Color.foreground
  property color badgeColor: Commons.Color.urgent
  property bool online: true
  property bool badge: false

  width: iconSize
  height: iconSize
  implicitWidth: iconSize
  implicitHeight: iconSize

  onColorChanged: canvas.requestPaint()
  onOnlineChanged: canvas.requestPaint()
  onIconSizeChanged: canvas.requestPaint()

  Canvas {
    id: canvas
    anchors.fill: parent
    onPaint: {
      var ctx = getContext("2d")
      var s = width
      ctx.reset()
      ctx.strokeStyle = root.color
      ctx.fillStyle = root.color
      ctx.lineCap = "round"
      ctx.lineWidth = Math.max(1.2, s * 0.09)

      var cx = s / 2
      var cy = s * 0.36

      // Mast and its tip.
      ctx.beginPath()
      ctx.moveTo(cx, cy)
      ctx.lineTo(cx, s * 0.95)
      ctx.stroke()
      ctx.beginPath()
      ctx.arc(cx, cy, s * 0.09, 0, Math.PI * 2)
      ctx.fill()

      // Signal arcs.
      ctx.globalAlpha = root.online ? 1.0 : 0.3
      var radii = [s * 0.24, s * 0.42]
      for (var i = 0; i < radii.length; i++) {
        ctx.beginPath()
        ctx.arc(cx, cy, radii[i], -Math.PI * 0.25, Math.PI * 0.25)
        ctx.stroke()
        ctx.beginPath()
        ctx.arc(cx, cy, radii[i], Math.PI * 0.75, Math.PI * 1.25)
        ctx.stroke()
      }
    }
  }

  Rectangle {
    visible: root.badge
    width: Math.max(4, root.iconSize * 0.34)
    height: width
    radius: width / 2
    color: root.badgeColor
    anchors.right: parent.right
    anchors.top: parent.top
    anchors.rightMargin: -width * 0.25
    anchors.topMargin: -width * 0.15
  }
}
