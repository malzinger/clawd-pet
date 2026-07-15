"""Mini crabs (M): one small always-on-top crab per running subagent.

Module only — the app wiring (mapping SubagentStart/SubagentStop counts to
``MiniPetManager.set_count``) lives elsewhere. Each :class:`MiniCrab` is a
scaled-down copy of the main pet's idle look that scuttles around a fixed
anchor point near Clawd.
"""
import random
import sys
import time

from PyQt5.QtCore import QElapsedTimer, QPoint, QRect, QRectF, QSize, Qt, QTimer
from PyQt5.QtGui import QGuiApplication, QPainter
from PyQt5.QtWidgets import QWidget

from .art import ArtState, ClawdArt, SpriteSet
from .config import MINIPET_HEIGHT_FACTOR, MINIPET_MAX, PET_HEIGHT

# The idle-sprite frames are decoded and pre-scaled once per height and shared
# by every crab: Sprite is read-only after build() (frame_at + pixmaps), so up
# to MINIPET_MAX crabs must not each re-decode all the GIFs.
_SPRITE_CACHE = {}


def _sprites_for(height: int) -> SpriteSet:
    sprites = _SPRITE_CACHE.get(height)
    if sprites is None:
        sprites = SpriteSet(height=height)
        _SPRITE_CACHE[height] = sprites
    return sprites


class MiniCrab(QWidget):
    """A small, draggable, idle-animated crab scuttling around an anchor.

    No panel, no menu, no petting — the only interaction is a simple drag
    (which moves the anchor). The window setup mirrors PetWidget exactly.
    CRITICAL (repo lesson): NEVER call raise_() on darwin — it can activate
    the whole app and steal keyboard focus.
    """

    TICK_MS = 60                 # one timer drives both the gif and the motion
    DRAG_THRESHOLD = 6           # px before a press turns into a drag
    WALK_STEP_PX = 2             # px moved per tick while scuttling
    WALK_RANGE_PX = (40, 120)    # random walk distance from the anchor
    PAUSE_RANGE_S = (1.0, 4.0)   # random pause between walks

    def __init__(self, anchor: QPoint):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if sys.platform == "darwin":   # Qt.Tool windows vanish on app deactivation
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)

        height = int(PET_HEIGHT * MINIPET_HEIGHT_FACTOR)
        self._sprite = _sprites_for(height).sprite("chill")   # the idle gif
        if self._sprite is not None and self._sprite.pixmaps:
            self.setFixedSize(QSize(self._sprite.pixmaps[0].width(), height))
        else:                          # vector fallback, like the main pet
            self._sprite = None
            scale = height / ClawdArt.H
            self.setFixedSize(QSize(int(ClawdArt.W * scale + 0.5), height))

        self._clock = QElapsedTimer()  # gif frame timing, like PetWidget
        self._clock.start()
        self._frame = 0
        self._facing = 1               # sprite facing: mirrored blit when -1
        self._state = "pause"          # "pause" | "walk"
        self._until = time.monotonic() + random.uniform(*self.PAUSE_RANGE_S)
        self._target_x = 0             # widget x the current walk heads for
        self._anchor = QPoint(0, 0)

        self._press_global = None
        self._press_window = None
        self._dragging = False

        self.set_anchor(anchor.x(), anchor.y())

        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # -------------------------------------------------- anchor / geometry

    def _screen_avail(self) -> QRect:
        screen = (QGuiApplication.screenAt(self._anchor)
                  or QGuiApplication.screenAt(self.frameGeometry().center())
                  or QGuiApplication.primaryScreen())
        return screen.availableGeometry()

    def set_anchor(self, x: int, y: int):
        """Base position of the scuttle: x is the crab's center, y its feet
        line. The widget snaps there (clamped onto the screen)."""
        self._anchor = QPoint(int(x), int(y))
        avail = self._screen_avail()
        wx = max(avail.left(),
                 min(int(x) - self.width() // 2, avail.right() - self.width()))
        wy = max(avail.top(),
                 min(int(y) - self.height(), avail.bottom() - self.height() + 1))
        self.move(wx, wy)
        self._state = "pause"
        self._until = time.monotonic() + random.uniform(*self.PAUSE_RANGE_S)

    def stop(self):
        """Stop the 60 ms anim/motion timer (despawn / test teardown)."""
        self._timer.stop()

    def hideEvent(self, event):
        self._timer.stop()     # battery: a hidden crab must not keep ticking
        super().hideEvent(event)

    # -------------------------------------------------- animation / motion

    def _start_walk(self):
        span = random.randint(*self.WALK_RANGE_PX) * random.choice((-1, 1))
        self._target_x = self._anchor.x() + span - self.width() // 2
        self._state = "walk"

    def _tick(self):
        self._frame += 1
        self.update()          # gif frame timing derives from the clock
        if self._press_global is not None:
            return             # no autonomous motion mid-drag
        now = time.monotonic()
        if self._state == "pause":
            if now >= self._until:
                self._start_walk()
            return
        avail = self._screen_avail()
        left = avail.left()
        right = max(left, avail.right() - self.width())
        tx = max(left, min(self._target_x, right))   # clamp onto the screen
        dx = tx - self.x()
        if dx == 0:                                  # arrived: take a break
            self._state = "pause"
            self._until = now + random.uniform(*self.PAUSE_RANGE_S)
            return
        face = -1 if dx < 0 else 1
        if face != self._facing:
            self._facing = face
            self.update()
        step = max(-self.WALK_STEP_PX, min(self.WALK_STEP_PX, dx))
        self.move(self.x() + step, self.y())

    def paintEvent(self, _event):
        p = QPainter(self)
        mirrored = self._facing < 0
        if mirrored:
            # walking left: mirror the frame around its own vertical center —
            # the artwork's native facing is kept for walking right (PetWidget
            # _blit behavior)
            cx = self.width() / 2.0
            p.save()
            p.translate(cx, 0)
            p.scale(-1.0, 1.0)
            p.translate(-cx, 0)
        if self._sprite is not None:
            pm = self._sprite.pixmaps[self._sprite.frame_at(self._clock.elapsed())]
            x = (self.width() - pm.width()) // 2
            y = self.height() - pm.height()          # feet on the ground
            p.drawPixmap(x, y, pm)
        else:
            ClawdArt.draw(p, QRectF(self.rect()),
                          ArtState(mood="chill", frame=self._frame))
        if mirrored:
            p.restore()
        p.end()

    # -------------------------------------------------- mouse (drag only)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_global = event.globalPos()
            self._press_window = self.pos()
            self._dragging = False
            event.accept()

    def mouseMoveEvent(self, event):
        if self._press_global is None or not (event.buttons() & Qt.LeftButton):
            return
        delta = event.globalPos() - self._press_global
        if not self._dragging and delta.manhattanLength() < self.DRAG_THRESHOLD:
            return
        self._dragging = True
        self.move(self._press_window + delta)
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        if self._dragging:     # dropping the crab re-anchors it there
            self.set_anchor(self.x() + self.width() // 2,
                            self.y() + self.height())
        self._press_global = None
        self._dragging = False
        event.accept()


class MiniPetManager:
    """Keeps exactly one MiniCrab per running subagent (capped)."""

    def __init__(self):
        self._crabs = []
        self._anchor = None    # last anchor point, reused when none is given

    def set_count(self, n: int, anchor_point: QPoint = None):
        """Spawn/despawn crabs to match n (capped at MINIPET_MAX), spreading
        their anchors alternating left/right of anchor_point."""
        if anchor_point is not None:
            self._anchor = QPoint(anchor_point)
        n = max(0, min(int(n), MINIPET_MAX))
        while len(self._crabs) > n:            # despawn most recent first
            crab = self._crabs.pop()
            crab.hide()                        # hideEvent stops the timer ...
            crab.stop()                        # ... belt and suspenders
            crab.deleteLater()
        if len(self._crabs) < n:
            base = self._anchor
            if base is None:                   # sane default: primary screen
                avail = QGuiApplication.primaryScreen().availableGeometry()
                base = QPoint(avail.center().x(), avail.bottom())
            while len(self._crabs) < n:
                i = len(self._crabs)
                offset = (60 + 50 * i) * (1 if i % 2 == 0 else -1)
                crab = MiniCrab(QPoint(base.x() + offset, base.y()))
                crab.show()
                self._crabs.append(crab)

    def count(self) -> int:
        return len(self._crabs)

    def clear(self):
        self.set_count(0)

    def positions(self):
        return [(crab.x(), crab.y()) for crab in self._crabs]
