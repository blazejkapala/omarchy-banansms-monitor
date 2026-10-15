import QtQuick
import qs.Commons

Item {
  id: root
  property real iconSize: Style.font.icon
  property color color: Color.foreground
  implicitWidth: iconSize
  implicitHeight: iconSize
  width: iconSize
  height: iconSize
  onColorChanged: canvas.requestPaint()
  onIconSizeChanged: canvas.requestPaint()
  Canvas {
    id: canvas
    anchors.fill: parent
    antialiasing: true
    onPaint: {
      var c = getContext("2d"), s = width / 20
      c.reset(); c.clearRect(0, 0, width, height)
      c.fillStyle = root.color; c.strokeStyle = root.color
      c.lineJoin = "round"; c.lineCap = "round"; c.lineWidth = 1.4 * s
      c.beginPath(); c.moveTo(4*s, 3*s)
      c.bezierCurveTo(0*s, 10*s, 4*s, 17*s, 14*s, 18*s)
      c.bezierCurveTo(17*s, 18*s, 16*s, 16*s, 14*s, 16*s)
      c.bezierCurveTo(7*s, 14*s, 5*s, 9*s, 7*s, 4*s)
      c.closePath(); c.fill()
      c.beginPath(); c.moveTo(5*s, 4*s); c.lineTo(4*s, 1*s); c.stroke()
      c.strokeRect(10*s, 2*s, 9*s, 7*s)
      c.beginPath(); c.moveTo(11*s, 7*s); c.lineTo(13*s, 7*s); c.lineTo(14*s, 4*s); c.lineTo(16*s, 7*s); c.lineTo(18*s, 4*s); c.stroke()
    }
  }
}
