"""Shell drops (Tiny-Pasture loop): collectible bonus XP while agents work.

During long working stretches the pet occasionally drops a shell nearby;
clicking it collects bonus XP. One at a time, fades away unclaimed after
a while — pure reward, never a nag. The spawn cadence lives in the app;
this module is just the widget + a pure scheduler.
"""
import random
import sys
import time
from typing import Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QWidget

from .config import (
    SHELL_LIFETIME_S,
    SHELL_MAX_INTERVAL_S,
    SHELL_MIN_INTERVAL_S,
    SHELL_SIZE,
    SHELL_XP_RANGE,
)


class ShellWidget(QWidget):
    """A clickable pixel shell; on_collect(xp) fires exactly once."""

    def __init__(self, on_collect):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if sys.platform == "darwin":
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setFixedSize(SHELL_SIZE, SHELL_SIZE)
        self.setCursor(Qt.PointingHandCursor)
        self._on_collect = on_collect
        self.xp = random.randint(*SHELL_XP_RANGE)
        self.collected = False
        self._expire = QTimer(self)
        self._expire.setSingleShot(True)
        self._expire.setInterval(int(SHELL_LIFETIME_S * 1000))
        self._expire.timeout.connect(self.disappear)

    def appear(self, x: int, y: int):
        self.move(int(x), int(y))
        self.show()                      # never raise_() — focus-steal lesson
        self._expire.start()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self.collected:
            self.collected = True
            cb, self._on_collect = self._on_collect, None
            self.disappear()
            if cb is not None:
                cb(self.xp)
            event.accept()

    def disappear(self):
        self._expire.stop()
        self.hide()
        self.deleteLater()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        px = max(2, SHELL_SIZE // 10)
        shell = QColor("#e8b465")
        ridge = QColor("#b9803a")
        rows = SHELL_SIZE // px
        for ry in range(rows):
            width_frac = 1.0 - abs(ry / rows - 0.55) * 1.4
            for rx in range(rows):
                cx = abs(rx / rows - 0.5)
                if cx <= width_frac / 2 and ry >= rows // 4:
                    p.fillRect(rx * px, ry * px, px, px,
                               ridge if rx % 3 == 0 else shell)
        p.end()


class ShellScheduler:
    """Pure spawn-cadence state machine (the app owns the QTimer).

    should_spawn() is asked periodically while an agent is WORKING; it fires
    at most once per randomized interval and only while nothing is pending."""

    def __init__(self, rng: Optional[random.Random] = None):
        self._rng = rng or random.Random()
        self._next = 0.0
        self.arm(time.monotonic())

    def arm(self, now_mono: float):
        self._next = now_mono + self._rng.uniform(SHELL_MIN_INTERVAL_S,
                                                  SHELL_MAX_INTERVAL_S)

    def should_spawn(self, now_mono: float, pending: bool,
                     working: bool) -> bool:
        if pending or not working or now_mono < self._next:
            return False
        self.arm(now_mono)
        return True
