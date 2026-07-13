"""Pet widget — the always-on-top mascot."""
import random
import sys
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:                      # only for the Optional["ClawdApp"] hint
    from .app import ClawdApp

from PyQt5.QtCore import QElapsedTimer, QRectF, QSize, Qt, QTimer
from PyQt5.QtGui import QColor, QPainter, QPixmap
from PyQt5.QtWidgets import QWidget

from .art import ArtState, ClawdArt, SpriteSet
from .config import PET_HEIGHT
from .i18n import fmt_de, fmt_pct_de, tr
from .moods import (
    IDLE_FLOURISH_PROB,
    IDLE_FLOURISHES,
    IDLE_SWITCH_MS,
    MOOD_FALLBACK,
    PET_SPAM_COUNT,
    PET_SPAM_WINDOW_S,
    TOOL_MOODS,
    mood_for_pct,
)
from .usage import UsageSnapshot

# ======================================================================
#  Pet widget — the always-on-top mascot
# ======================================================================

_HEART_ROWS = ("0110110", "1111111", "1111111", "0111110", "0011100", "0001000")


class PetWidget(QWidget):
    ANIM_TICK_MS = 33          # ~30 fps; sprite timing comes from the GIF delays
    DRAG_THRESHOLD = 6
    MOOD_FADE_MS = 340         # cross-dissolve a mood change
    HEART_LIFE_MS = 1200       # petting hearts float up and fade this long
    REACT_MS = 1300            # how long the petting reaction animation plays
    STARTLE_COOLDOWN_S = 30.0  # min. seconds between hover-startles while asleep

    def __init__(self, owner: Optional["ClawdApp"] = None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.owner = owner
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if sys.platform == "darwin":   # Qt.Tool windows vanish on app deactivation
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)

        self.pct = 0.0
        self.mood = "chill"
        self._quota_mood = "chill"
        self._activity = None          # None | (kind, tool)
        self._hearts = []
        self._react_active = False     # a transient petting reaction is playing
        self._react_timer = QTimer(self)
        self._react_timer.setSingleShot(True)
        self._react_timer.timeout.connect(self._end_reaction)
        self._pet_times = []           # recent petting stamps (spam -> annoyed)
        self._last_startle = None      # monotonic stamp of the last hover-startle
        self._idle_variant = None      # current random idle flourish, or None
        self._idle_pool = []           # available idle flourishes (filled below)
        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(IDLE_SWITCH_MS)
        self._idle_timer.timeout.connect(self._tick_idle)

        self._sprites = SpriteSet()
        if self._sprites.sprites:
            self.setFixedSize(QSize(self._sprites.width, self._sprites.height))
        else:
            scale = PET_HEIGHT / ClawdArt.H
            self.setFixedSize(QSize(int(ClawdArt.W * scale + 0.5), PET_HEIGHT))

        # sprite playback / cross-dissolve state
        self._clock = QElapsedTimer()
        self._clock.start()
        self._mood_clock = QElapsedTimer()
        self._prev_pixmap = None

        # animation state
        self._frame = 0
        self._blink_left = 0
        self._next_blink = random.randint(25, 60)
        self._cursor_on = True
        self._glitch_seed = 0
        self._sweat_t = 0.0

        self._press_global = None
        self._press_window = None
        self._dragging = False

        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick)
        self._anim_timer.start(self.ANIM_TICK_MS)

        self._idle_pool = [m for m in IDLE_FLOURISHES
                           if m in self._sprites.sprites]
        if self._idle_pool:
            self._idle_timer.start()

        self._apply_mood()
        self.setToolTip(tr("tooltip_wait"))

    # -------------------------------------------------- state / painting

    def set_snapshot(self, snap: UsageSnapshot):
        idle = ((snap.source == "logs" and snap.entries == 0)
                or (snap.source == "api" and snap.pct <= 0))
        self.pct = snap.pct
        self._quota_mood = ("sleep" if (not snap.error and idle)
                            else mood_for_pct(snap.pct))
        self._update_mood()
        if snap.error:
            self.setToolTip(snap.error)
        elif snap.source == "api":
            self.setToolTip(tr("tooltip_api", p=fmt_pct_de(snap.pct)))
        else:
            self.setToolTip(tr("tooltip_est", p=fmt_pct_de(snap.pct),
                               n=fmt_de(snap.total)))

    def set_pct(self, pct: float):
        self.pct = pct
        self._quota_mood = mood_for_pct(pct)
        self._update_mood()

    def set_activity(self, activity):
        """activity: None or (kind, tool); kind in working/waiting/needs_input/error."""
        if activity != self._activity:
            self._activity = activity
            self._update_mood()

    def _update_mood(self):
        """Combine quota mood with live activity: quota alarms + reactions win.

        The running tool picks the animation (typing / reading / thinking /
        building), so Clawd visibly does what Claude is doing.
        """
        if self._react_active:
            return                       # let a petting reaction play out
        mood = self._quota_mood
        if mood not in ("panic", "limit") and self._activity:
            kind, tool = self._activity[0], self._activity[1]
            if kind == "working":
                mood = TOOL_MOODS.get(tool, "think" if tool is None else "focus")
            elif kind == "needs_input":
                mood = "notify"
            elif kind == "waiting":
                mood = "happy"
            elif kind == "error":
                mood = "panic"
        if mood == "chill":
            if self._idle_variant:
                mood = self._idle_variant          # play the random idle flourish
        else:
            self._idle_variant = None              # left idle -> don't resume a stale one
        if self._sprites.sprites and mood not in self._sprites.sprites:
            mood = MOOD_FALLBACK.get(mood, mood)   # older sprites/ without new gifs
        self._set_mood(mood)

    def _play_reaction(self):
        """Petting reaction: a happy double-jump, or annoyed if over-petted."""
        loaded = self._sprites.sprites
        if not loaded:
            return
        now = time.monotonic()
        self._pet_times = [t for t in self._pet_times
                           if now - t < PET_SPAM_WINDOW_S]
        self._pet_times.append(now)
        want = "annoyed" if len(self._pet_times) >= PET_SPAM_COUNT else "pet"
        if want not in loaded:
            want = "pet"
        if want not in loaded:
            return
        self._react_active = True
        self._set_mood(want)
        self._react_timer.start(self.REACT_MS)

    def _startle(self) -> bool:
        """A hovering cursor startles the sleeping Clawd: a short jump-up.

        Returns True if the reaction started. Reuses the petting reaction
        mechanics, so _end_reaction() drops him right back to sleep.
        """
        loaded = self._sprites.sprites
        if not loaded or self._react_active or self.mood != "sleep":
            return False
        now = time.monotonic()
        if (self._last_startle is not None
                and now - self._last_startle < self.STARTLE_COOLDOWN_S):
            return False                 # mouse traffic shouldn't keep him awake
        want = "pet" if "pet" in loaded else "happy"  # double-jump, else cheer
        if want not in loaded:
            return False
        self._last_startle = now
        self._react_active = True
        self._set_mood(want)
        self._react_timer.start(self.REACT_MS)
        return True

    def _end_reaction(self):
        self._react_active = False
        self._update_mood()

    def _tick_idle(self):
        """While Clawd is calm, occasionally play a random idle flourish."""
        calm = (not self._react_active and self._quota_mood == "chill"
                and self._activity is None)
        if not calm:
            if self._idle_variant is not None:
                self._idle_variant = None
                self._update_mood()
            return
        if self._idle_variant is not None:
            self._idle_variant = None                  # flourish over -> back to idle
        elif self._idle_pool and random.random() < IDLE_FLOURISH_PROB:
            self._idle_variant = random.choice(self._idle_pool)
        self._update_mood()

    def _set_mood(self, mood: str):
        if mood != self.mood:
            prev = self._current_pixmap()   # freeze the OLD mood before switching
            self.mood = mood
            self._apply_mood(prev)

    def _apply_mood(self, prev: Optional[QPixmap] = None):
        sprite = self._sprites.sprite(self.mood)
        if sprite is not None:
            self._prev_pixmap = prev
            if prev is not None:
                self._mood_clock.restart()
            self._clock.restart()
        self.update()

    def _current_pixmap(self) -> Optional[QPixmap]:
        sprite = self._sprites.sprite(self.mood)
        if sprite is None or not sprite.pixmaps:
            return None
        return sprite.pixmaps[sprite.frame_at(self._clock.elapsed())]

    def _tick(self):
        if self._sprites.sprites:
            self.update()      # sprite timing is derived from the clock
            return
        self._frame += 1

        # eye blink scheduling
        if self._blink_left > 0:
            self._blink_left -= 1
        elif self._frame >= self._next_blink and self.mood in ("chill", "focus"):
            self._blink_left = 2
            base = 40 if self.mood == "chill" else 26
            self._next_blink = self._frame + random.randint(base, base + 36)

        # cursor blink speed per mood
        period = {"chill": 9, "focus": 3, "panic": 2, "limit": 4}.get(self.mood, 9)
        self._cursor_on = (self._frame // period) % 2 == 0

        if self.mood == "panic":
            self._glitch_seed = random.randint(0, 1_000_000)
            self._sweat_t = (self._frame % 44) / 44.0

        self.update()

    def _art_state(self) -> ArtState:
        return ArtState(
            mood=self.mood,
            frame=self._frame,
            blink=self._blink_left > 0,
            cursor_on=self._cursor_on,
            glitch_seed=self._glitch_seed,
            sweat_t=self._sweat_t,
        )

    def _blit(self, p: QPainter, pm: QPixmap, opacity: float):
        if pm is None or pm.isNull() or opacity <= 0.001:
            return
        p.setOpacity(min(1.0, opacity))
        x = (self.width() - pm.width()) // 2
        y = self.height() - pm.height()          # feet on the ground
        p.drawPixmap(x, y, pm)

    def paintEvent(self, _event):
        p = QPainter(self)
        sprite = self._sprites.sprite(self.mood)
        if sprite is not None and sprite.pixmaps:
            # how far the incoming mood has dissolved in (1.0 = fully there)
            mood_in = 1.0
            if self._prev_pixmap is not None and self._mood_clock.isValid():
                elapsed = self._mood_clock.elapsed()
                if elapsed < self.MOOD_FADE_MS:
                    mood_in = elapsed / self.MOOD_FADE_MS
                else:
                    self._prev_pixmap = None
            self._blit(p, self._prev_pixmap, 1.0 - mood_in)

            frame = sprite.pixmaps[sprite.frame_at(self._clock.elapsed())]
            self._blit(p, frame, mood_in)
            p.setOpacity(1.0)
            self._draw_hearts(p)
            p.end()
            return
        ClawdArt.draw(p, QRectF(self.rect()), self._art_state())
        self._draw_hearts(p)
        p.end()

    def _draw_hearts(self, p: QPainter):
        if not self._hearts:
            return
        now = self._clock.elapsed()
        alive = []
        for h in self._hearts:
            age = now - h["born"]
            if age > self.HEART_LIFE_MS:
                continue
            alive.append(h)
            t = age / self.HEART_LIFE_MS
            col = QColor(232, 84, 120, int(235 * (1.0 - t)))
            x = h["x"] + h["vx"] * age * 0.05
            y = h["y"] - age * 0.045
            px = 2.0
            for ry, row in enumerate(_HEART_ROWS):
                for rx, ch in enumerate(row):
                    if ch == "1":
                        p.fillRect(QRectF(x + rx * px, y + ry * px, px, px), col)
        self._hearts = alive

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            for _ in range(5):
                self._hearts.append({
                    "x": self.width() / 2 + random.uniform(-30, 16),
                    "y": self.height() * 0.4 + random.uniform(-10, 10),
                    "vx": random.uniform(-0.5, 0.5),
                    "born": self._clock.elapsed(),
                })
            self._play_reaction()          # Clawd does a happy double-jump
            self.update()
            event.accept()

    # -------------------------------------------------- mouse handling

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
        if self.owner:
            self.owner.pet_moved()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        was_drag = self._dragging
        self._press_global = None
        self._dragging = False
        if self.owner:
            if was_drag:
                self.owner.save_position()
            else:
                self.owner.toggle_panel()
        event.accept()

    def contextMenuEvent(self, event):
        if self.owner:
            menu = self.owner.build_menu(None)
            menu.exec_(event.globalPos())
            menu.deleteLater()

    # -------------------------------------------------- hover handling

    def enterEvent(self, event):
        if self.owner:
            self.owner.hover_panel()
        self._startle()                # approaching a sleeping Clawd wakes him
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self.owner:
            self.owner.schedule_panel_hide()
        super().leaveEvent(event)
