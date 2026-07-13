"""Speech bubble — small transient callout above the pet."""
import sys

from PyQt5.QtCore import QRectF, Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QGuiApplication, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QWidget

# ======================================================================
#  Speech bubble — small transient callout above the pet
# ======================================================================

class SpeechBubble(QWidget):
    TAIL_H = 7
    PAD_X, PAD_Y = 12, 7

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if sys.platform == "darwin":
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self._text = ""
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self.setFont(QFont("Segoe UI", 9))
        self._on_click = None

    def show_text(self, text: str, pet: QWidget, duration_ms: int = 4200,
                  on_click=None):
        self._text = text
        self._on_click = on_click
        fm = self.fontMetrics()
        self.setFixedSize(max(46, fm.horizontalAdvance(text) + self.PAD_X * 2),
                          fm.height() + self.PAD_Y * 2 + self.TAIL_H)
        self.follow(pet)
        self.show()
        self.raise_()
        self.update()
        self._hide_timer.start(duration_ms)

    def follow(self, pet: QWidget):
        geo = pet.frameGeometry()
        screen = (QGuiApplication.screenAt(geo.center())
                  or QGuiApplication.primaryScreen())
        avail = screen.availableGeometry()
        x = geo.center().x() - self.width() // 2
        x = max(avail.left() + 4, min(x, avail.right() - self.width() - 4))
        y = max(avail.top() + 4, geo.top() - self.height() - 2)
        self.move(x, y)

    def mousePressEvent(self, _event):
        cb = self._on_click
        self.hide()
        if cb:
            cb()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        body = QRectF(0, 0, self.width(), self.height() - self.TAIL_H)
        p.setPen(QPen(QColor("#3d3b38"), 1))
        p.setBrush(QColor(38, 37, 35, 250))
        p.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), 9, 9)
        cx = self.width() / 2
        tail = QPainterPath()
        tail.moveTo(cx - 6, body.bottom() - 1)
        tail.lineTo(cx, self.height() - 1)
        tail.lineTo(cx + 6, body.bottom() - 1)
        tail.closeSubpath()
        p.fillPath(tail, QColor(38, 37, 35, 250))
        p.setPen(QColor("#eceae6"))
        p.drawText(body, Qt.AlignCenter, self._text)
        p.end()
