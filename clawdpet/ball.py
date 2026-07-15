"""A throwable ball for fetch games (vscode-pets' most-loved feature).

Tiny always-on-top window with the same gravity/bounce/friction physics
as the pet's own throw (F12). The app launches it from the tray; the pet
notices when it lands and fetches it. Deliberately dumb: no interaction
beyond flying, resting and being removed by the fetching pet.
"""
import sys
import time

from PyQt5.QtCore import QRect, Qt, QTimer
from PyQt5.QtGui import QColor, QGuiApplication, QPainter

from PyQt5.QtWidgets import QWidget

from .config import (
    BALL_SIZE,
    THROW_BOUNCE,
    THROW_FRICTION,
    THROW_GRAVITY,
    THROW_STOP_SPEED,
)

BALL_TIMEOUT_S = 20.0        # hard cap on one flight (mirrors the pet's cap)


class BallWidget(QWidget):
    TICK_MS = 33

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if sys.platform == "darwin":
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setFixedSize(BALL_SIZE, BALL_SIZE)
        self.landed = False
        self._v = [0.0, 0.0]
        self._pos = [0.0, 0.0]
        self._deadline = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)

    # -- physics ---------------------------------------------------------

    def launch(self, x: float, y: float, vx: float, vy: float):
        """Start a flight from (x, y) with velocity in px/s."""
        self.landed = False
        self._pos = [float(x), float(y)]
        self._v = [float(vx), float(vy)]
        self.move(int(x), int(y))
        self._deadline = time.monotonic() + BALL_TIMEOUT_S
        self.show()                      # never raise_() — focus-steal lesson
        self._timer.start()

    def _avail(self) -> QRect:
        screen = (QGuiApplication.screenAt(self.frameGeometry().center())
                  or QGuiApplication.primaryScreen())
        return screen.availableGeometry()

    def step(self, dt: float, avail: QRect) -> bool:
        """One integration step; returns True while still flying. Pure-ish
        (no timers) so the selftest can drive a full trajectory headless."""
        vx, vy = self._v
        vy += THROW_GRAVITY * dt
        x = self._pos[0] + vx * dt
        y = self._pos[1] + vy * dt
        left, right = avail.left(), avail.right() - self.width()
        top, bottom = avail.top(), avail.bottom() - self.height()
        if x < left:
            x, vx = left, -vx * THROW_BOUNCE
        elif x > right:
            x, vx = right, -vx * THROW_BOUNCE
        if y > bottom:
            y = bottom
            vy = -vy * THROW_BOUNCE
            vx *= THROW_FRICTION
        elif y < top:
            y, vy = top, -vy * THROW_BOUNCE
        self._v = [vx, vy]
        self._pos = [x, y]
        self.move(int(round(x)), int(round(y)))
        speed = (vx * vx + vy * vy) ** 0.5
        if ((speed < THROW_STOP_SPEED and y >= bottom - 0.5)
                or time.monotonic() >= self._deadline):
            self.landed = True
            return False
        return True

    def _tick(self):
        if not self.step(self.TICK_MS / 1000.0, self._avail()):
            self._timer.stop()

    def remove(self):
        """Fetched (or cancelled): stop and dispose."""
        self._timer.stop()
        self.hide()
        self.deleteLater()

    # -- looks -----------------------------------------------------------

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)   # keep it pixelated
        px = max(2, BALL_SIZE // 9)
        # blocky ball: red body, white patch, dark outline feel
        body = QColor("#d9482f")
        patch = QColor("#f4ede4")
        for ry in range(BALL_SIZE // px):
            for rx in range(BALL_SIZE // px):
                cx = rx * px + px / 2 - BALL_SIZE / 2
                cy = ry * px + px / 2 - BALL_SIZE / 2
                if cx * cx + cy * cy <= (BALL_SIZE / 2 - 1) ** 2:
                    # light patch top-left so it reads as a ball, not a dot
                    lx = cx + BALL_SIZE * 0.18
                    ly = cy + BALL_SIZE * 0.18
                    p.fillRect(rx * px, ry * px, px, px,
                               patch if lx * lx + ly * ly
                               <= (BALL_SIZE * 0.22) ** 2 else body)
        p.end()
