"""Pet widget — the always-on-top mascot."""
import random
import sys
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:                      # only for the Optional["ClawdApp"] hint
    from .app import ClawdApp

from PyQt5.QtCore import QElapsedTimer, QRect, QRectF, QSize, Qt, QTimer
from PyQt5.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPixmap, QRegion
from PyQt5.QtWidgets import QWidget

from . import macwindows
from .art import ArtState, ClawdArt, SpriteSet, _alpha_bbox
from .hats import anchor_for, hat_pixmap
from .config import (
    CELEBRATE_HOP_V,
    FETCH_SPEED_PX,
    MISCHIEF_CURSOR_STILL_S,
    MISCHIEF_MAX_INTERVAL_S,
    MISCHIEF_MIN_INTERVAL_S,
    MISCHIEF_PUSH_PX,
    CELEBRATE_MS,
    CHASE_RELEASE_PX,
    CHASE_SPEED_PX,
    CHASE_STOP_SHORT_PX,
    CHASE_TICK_MS,
    PET_HEIGHT,
    THROTTLE_IDLE_S,
    THROTTLE_TICK_MS,
    THROW_BOUNCE,
    THROW_FRICTION,
    THROW_GRAVITY,
    THROW_MIN_SPEED,
    THROW_STOP_SPEED,
    TYPING_BOB_PERIOD_MS,
    TYPING_BOB_PX,
    WANDER_PAUSE_RANGE_S,
    WANDER_SPEED_PX,
    WANDER_TICK_MS,
    WANDER_WALK_RANGE_S,
    WINDOW_SIT_POLL_MS,
)
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
    THROW_TICK_MS = 33         # throw-physics integration step (F12)
    THROW_TIMEOUT_S = 6.0      # safety cap on one flight
    THROW_SAMPLE_WINDOW_S = 0.12   # release speed is fit over this drag tail
    DRAG_SAMPLE_COUNT = 6      # recent (stamp, pos) drag samples kept

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

        # F2/F13: current sprite scaling; rebuild() swaps these at runtime
        self._height = PET_HEIGHT
        self._sprite_dir = None
        self._sprites = SpriteSet()
        self._apply_widget_size()

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
        self._drag_samples = []        # recent (monotonic stamp, global pos)

        # F5: autonomous wandering (opt-in, walk/pause state machine)
        self._wander_enabled = False
        self._wander_state = "pause"   # "walk" | "pause"
        self._wander_until = 0.0       # monotonic deadline of the current phase
        self._wander_dir = 1           # walking direction: +1 right, -1 left
        self._wander_facing = 1        # sprite facing (mirrored blit when -1)
        self._wander_carry = 0.0       # sub-pixel movement accumulator
        self._wander_timer = QTimer(self)
        self._wander_timer.setInterval(WANDER_TICK_MS)
        self._wander_timer.timeout.connect(self._wander_tick)

        # F12: throw physics (started from a fast drag release)
        self._throw_on = False
        self._throw_v = [0.0, 0.0]     # velocity in px/s
        self._throw_pos = [0.0, 0.0]   # float position (move() truncates)
        self._throw_deadline = 0.0     # monotonic safety timeout
        self._throw_timer = QTimer(self)
        self._throw_timer.setInterval(self.THROW_TICK_MS)
        self._throw_timer.timeout.connect(self._throw_tick)

        # F8: click-through around the sprite (opt-in)
        self._click_through = False

        # Y: idle throttling, cursor chase, typing-along, celebration
        self._last_active_mono = time.monotonic()
        self._chase_enabled = False
        self._chase_state = "wait"     # "wait" | "chase" | "caught"
        self._chase_next = 0.0         # monotonic time of the next chase attempt
        self._chase_carry = 0.0        # sub-pixel movement accumulator
        self._chase_test_target = None  # selftest injects a QPoint target here
        self._chase_timer = QTimer(self)
        self._chase_timer.setInterval(CHASE_TICK_MS)
        self._chase_timer.timeout.connect(self._chase_tick)
        self._generating = False       # Claude is generating -> typing-along bob
        self._celebrating = False
        self._hat = "none"             # H: current hat key (drawn in _blit)
        self._hat_bbox_cache = {}      # frame cacheKey -> visible-content bbox

        # ball fetch (inline wave): walk to a landed ball and collect it
        self._fetch_ball = None
        self.on_fetch_done = None      # app callback: ball retrieved
        self._fetch_carry = 0.0
        self._fetch_timer = QTimer(self)
        self._fetch_timer.setInterval(50)
        self._fetch_timer.timeout.connect(self._fetch_tick)

        # mischief (opt-in): rare cursor pinch, DesktopGoose-style but gentle
        self._mischief_enabled = False
        self._mischief_next = 0.0
        self._mischief_armed = False
        self._cursor_still_since = time.monotonic()
        self._last_cursor_pos = None

        # W: window sitting (opt-in, macOS) — perch on the frontmost window.
        # The provider is injectable so the selftest can feed fake frames.
        self._window_sit_enabled = False
        self._window_sitting = False   # currently anchored to a window top
        self._sit_frame = None         # (x, y, w, h) of the anchor window
        self._window_frame_provider = macwindows.frontmost_window_frame
        self._window_sit_timer = QTimer(self)
        self._window_sit_timer.setInterval(WINDOW_SIT_POLL_MS)
        self._window_sit_timer.timeout.connect(self._window_sit_tick)

        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick)
        self._anim_timer.start(self.ANIM_TICK_MS)

        self._idle_pool = [m for m in IDLE_FLOURISHES
                           if m in self._sprites.sprites]
        if self._idle_pool:
            self._idle_timer.start()

        self._apply_mood()
        self.setToolTip(tr("tooltip_wait"))

    # -------------------------------------------------- rebuild (F2/F13)

    def _apply_widget_size(self):
        """Fix the widget size to the sprite set (or the vector fallback)."""
        if self._sprites.sprites:
            self.setFixedSize(QSize(self._sprites.width, self._sprites.height))
        else:
            scale = self._height / ClawdArt.H
            self.setFixedSize(QSize(int(ClawdArt.W * scale + 0.5),
                                    self._height))

    def rebuild(self, height: int = PET_HEIGHT, sprite_dir=None):
        """Reload the sprites with a new size (F2) and/or pack folder (F13).

        The current values are kept in self._height / self._sprite_dir so a
        caller changing only one aspect can pass the other one back in.
        """
        self._height = height
        self._sprite_dir = sprite_dir
        self._sprites = SpriteSet(height=height, sprite_dir=sprite_dir)
        self._apply_widget_size()
        # old frames have the wrong scale — never cross-dissolve from them
        self._prev_pixmap = None
        # refresh the idle-flourish pool for the new pack, keep timer state
        self._idle_pool = [m for m in IDLE_FLOURISHES
                           if m in self._sprites.sprites]
        if self._idle_pool:
            if not self._idle_timer.isActive():
                self._idle_timer.start()
        else:
            self._idle_timer.stop()
            self._idle_variant = None
        if not self._react_active:
            self._update_mood()      # re-apply MOOD_FALLBACK for the new pack
        self._apply_mood()           # restarts the clock + click-through mask

    # -------------------------------------------------- state / painting

    def set_snapshot(self, snap: UsageSnapshot):
        idle = ((snap.source == "logs" and snap.entries == 0)
                or (snap.source == "api" and snap.pct <= 0))
        self.pct = snap.pct
        mood_pct = snap.pct
        if snap.source == "logs" and mood_pct >= 100.0:
            # an estimate may look alarmed, but only LIVE data may assert the
            # hard "over the limit" state — a drifted local window once showed
            # 228 % while the real usage was at 51 %
            mood_pct = 99.0
        self._quota_mood = ("sleep" if (not snap.error and idle)
                            else mood_for_pct(mood_pct))
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
            self._mark_active()
            self._update_mood()

    # ---------------------------------------------- idle throttle (Y)

    @property
    def throttled(self) -> bool:
        """True while the animation timer runs at the slow idle rate."""
        return self._anim_timer.interval() > self.ANIM_TICK_MS

    def _mark_active(self):
        """Something happened — full frame rate, restart the idle countdown."""
        self._last_active_mono = time.monotonic()
        if self._anim_timer.interval() != self.ANIM_TICK_MS:
            self._anim_timer.setInterval(self.ANIM_TICK_MS)

    def _idle_now(self) -> bool:
        return (self.mood in ("chill", "sleep")
                and not self._react_active
                and self._activity is None
                and not self._generating
                and self._wander_state != "walk"
                and not self._throw_on
                and self._chase_state != "chase"
                and not self.underMouse())

    def _maybe_throttle(self):
        """Idle pets must not keep a 30 fps timer alive (battery/CPU): after
        THROTTLE_IDLE_S of true idleness the frame interval drops; any state
        change goes through _mark_active() and restores it instantly."""
        if not self._idle_now():
            self._mark_active()
            return
        if (time.monotonic() - self._last_active_mono >= THROTTLE_IDLE_S
                and self._anim_timer.interval() != THROTTLE_TICK_MS):
            self._anim_timer.setInterval(THROTTLE_TICK_MS)

    # ---------------------------------------------- typing-along + jubilee (Y)

    def set_generating(self, on: bool):
        """Claude is generating -> Clawd 'types along' with a subtle bob."""
        on = bool(on)
        if on == self._generating:
            return
        self._generating = on
        if on:
            self._mark_active()
        self.update()

    def celebrate(self) -> bool:
        """One-shot ~3 s celebration (quota reset etc.): the liveliest sprite
        available plus a happy little hop through the throw physics. Returns
        True if it started; idempotent while one is already playing."""
        loaded = self._sprites.sprites
        if not loaded or self._react_active or self._celebrating:
            return False
        want = next((m for m in ("conduct", "juggle", "happy")
                     if m in loaded), None)
        if want is None:
            return False
        self._celebrating = True
        self._react_active = True
        self._mark_active()
        self._set_mood(want)
        self._react_timer.start(CELEBRATE_MS)
        if not self._throw_active:
            self._start_throw(0.0, -CELEBRATE_HOP_V)
        return True

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
        if self._chase_state == "caught":
            mood = "sleep"                         # napping on the caught cursor
        elif (((self._wander_enabled and self._wander_state == "walk")
                or self._chase_state == "chase"
                or self._fetch_ball is not None)
                and "carry" in self._sprites.sprites):
            # walking gait while wandering (F5) or chasing (Y) — at ANY quota
            # mood: a walking pet must show a walk cycle, the alarm look
            # returns the moment it stands still
            mood = "carry"
        elif mood in ("chill", "focus", "happy"):
            if mood == "chill" and self._idle_variant:
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
        self._celebrating = False
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
            self._mark_active()             # full frame rate for the transition
            self._apply_mood(prev)

    def _apply_mood(self, prev: Optional[QPixmap] = None):
        sprite = self._sprites.sprite(self.mood)
        if sprite is not None:
            self._prev_pixmap = prev
            if prev is not None:
                self._mood_clock.restart()
            self._clock.restart()
        if self._click_through:
            self._apply_input_mask()   # the frame box changes with the mood
        self.update()

    def _current_pixmap(self) -> Optional[QPixmap]:
        sprite = self._sprites.sprite(self.mood)
        if sprite is None or not sprite.pixmaps:
            return None
        return sprite.pixmaps[sprite.frame_at(self._clock.elapsed())]

    def _tick(self):
        self._maybe_throttle()
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

    def set_hat(self, key: str):
        """H: wear a hat (drawn as an overlay in _blit); '' / 'none' = bare."""
        key = key or "none"
        if key != self._hat:
            self._hat = key
            self.update()

    def _frame_bbox(self, pm: QPixmap):
        """Visible-content box of a frame, cached per frame.

        Hats must anchor to the BODY, not the frame rectangle — gif frames
        carry transparent padding that varies per mood and bobs during the
        animation, so a frame-anchored hat floats or slides into the face
        (user report: sunglasses mispositioned)."""
        key = pm.cacheKey()
        box = self._hat_bbox_cache.get(key)
        if box is None:
            box = _alpha_bbox(pm.toImage())
            if box.isNull():
                box = pm.rect()
            if len(self._hat_bbox_cache) > 256:
                self._hat_bbox_cache.clear()      # sprite pack swapped
            self._hat_bbox_cache[key] = box
        return box

    def _draw_hat(self, p: QPainter, x: int, y: int, pm: QPixmap):
        if self._hat in ("", "none"):
            return
        box = self._frame_bbox(pm)
        hat = hat_pixmap(self._hat, box.width())
        if hat is None:
            return
        hx = x + box.left() + (box.width() - hat.width()) // 2
        if anchor_for(self._hat) == "eyes":
            # eye line sits high in a compact body; poses with raised claws
            # (jump/juggle) stretch the bbox upward, so their eyes sit deeper
            frac = {"happy": 0.34, "pet": 0.34,
                    "juggle": 0.30, "conduct": 0.30}.get(self.mood, 0.22)
            hy = (y + box.top() + int(box.height() * frac)
                  - hat.height() // 2)
        else:
            hy = y + box.top() - hat.height() * 2 // 3   # brim overlaps head
        p.drawPixmap(hx, max(0, hy), hat)

    def _blit(self, p: QPainter, pm: QPixmap, opacity: float):
        if pm is None or pm.isNull() or opacity <= 0.001:
            return
        p.setOpacity(min(1.0, opacity))
        x = (self.width() - pm.width()) // 2
        y = self.height() - pm.height()          # feet on the ground
        if self._generating and not self._throw_on:
            # typing-along (Y): a subtle ~8 Hz bob while Claude generates
            y -= TYPING_BOB_PX * ((self._clock.elapsed()
                                   // TYPING_BOB_PERIOD_MS) % 2)
        if self._wander_facing < 0:
            # walking left: mirror the frame around its own vertical center —
            # the GIFs' native facing is kept for walking right
            p.save()
            cx = x + pm.width() / 2.0
            p.translate(cx, 0)
            p.scale(-1.0, 1.0)
            p.translate(-cx, 0)
            p.drawPixmap(x, y, pm)
            self._draw_hat(p, x, y, pm)          # mirrored with the body
            p.restore()
            return
        p.drawPixmap(x, y, pm)
        self._draw_hat(p, x, y, pm)

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
            self._mark_active()
            if self._throw_active:
                self._stop_throw()     # catch a flying Clawd mid-air
            self._press_global = event.globalPos()
            self._press_window = self.pos()
            self._dragging = False
            self._drag_samples = [(time.monotonic(), event.globalPos())]
            event.accept()

    def mouseMoveEvent(self, event):
        if self._press_global is None or not (event.buttons() & Qt.LeftButton):
            return
        self._drag_samples.append((time.monotonic(), event.globalPos()))
        del self._drag_samples[:-self.DRAG_SAMPLE_COUNT]
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
        if was_drag:
            # a fast release flings Clawd (F12); a calm one just parks him —
            # toggle_panel must never fire after either kind of drag
            vx, vy = self._release_velocity()
            if (vx * vx + vy * vy) ** 0.5 >= THROW_MIN_SPEED:
                self._start_throw(vx, vy)
            elif self.owner:
                self.owner.save_position()
        elif self.owner:
            self.owner.toggle_panel()
        self._drag_samples = []
        event.accept()

    def contextMenuEvent(self, event):
        if self.owner:
            menu = self.owner.build_menu(None)
            menu.exec_(event.globalPos())
            menu.deleteLater()

    # -------------------------------------------------- wandering (F5)

    def _screen_avail(self) -> QRect:
        """Available geometry of the screen Clawd currently stands on."""
        screen = (QGuiApplication.screenAt(self.frameGeometry().center())
                  or QGuiApplication.primaryScreen())
        return screen.availableGeometry()

    def enable_wander(self, on: bool):
        self._wander_enabled = bool(on)
        if self._wander_enabled:
            self._wander_state = "pause"     # ease in with a pause first
            self._wander_until = (time.monotonic()
                                  + random.uniform(*WANDER_PAUSE_RANGE_S))
            self._wander_timer.start()
        else:
            self._wander_timer.stop()
            self._wander_state = "pause"
            if self._wander_facing != 1:
                self._wander_facing = 1      # back to the GIFs' native facing
                self.update()
        self._update_mood()                  # enter/leave the walking gait

    def _wander_blocked(self) -> bool:
        """No autonomous movement while the user or Claude interacts."""
        return (self._press_global is not None          # mid-drag
                or self.underMouse()                    # cursor on Clawd
                or (self.owner is not None
                    and self.owner.panel.isVisible())   # panel open
                or self._react_active                   # petting reaction
                or self._throw_active                   # flying (F12)
                or self._chase_state != "wait"          # chasing/napping (Y)
                or self._fetch_ball is not None         # fetching the ball
                or self._activity is not None           # visibly working
                or self._quota_mood == "sleep")         # truly asleep stays put

    def _wander_tick(self):
        now = time.monotonic()
        if self._wander_blocked():
            if self._wander_state == "walk":
                self._wander_state = "pause"
                if self.owner:
                    self.owner.save_position()
                self._update_mood()          # drop the walking gait
            # keep pushing the deadline so a fresh pause starts once free
            self._wander_until = now + random.uniform(*WANDER_PAUSE_RANGE_S)
            return
        if now >= self._wander_until:
            if self._wander_state == "walk":
                self._wander_state = "pause"
                self._wander_until = now + random.uniform(*WANDER_PAUSE_RANGE_S)
                if self.owner:
                    self.owner.save_position()   # persist once per stretch
            else:
                self._wander_state = "walk"
                self._wander_dir = random.choice((-1, 1))
                self._wander_carry = 0.0
                self._wander_until = now + random.uniform(*WANDER_WALK_RANGE_S)
            self._update_mood()              # walking gait on/off (carry gif)
            return
        if self._wander_state != "walk":
            return
        avail = self._screen_avail()
        self._wander_carry += WANDER_SPEED_PX * self._wander_dir
        step = int(self._wander_carry)
        self._wander_carry -= step
        if step == 0:
            return
        x = self.x() + step
        left, right = avail.left(), avail.right() - self.width()
        if self._window_sitting and self._sit_frame is not None:
            # window sitting (W) cooperates with wander: while perched, the
            # walkable floor is the window's top edge, so turn at ITS borders
            fx, _fy, fw, _fh = self._sit_frame
            left = max(left, fx)
            right = min(right, fx + fw - self.width())
            if right < left:
                right = left
        if x <= left:                    # turn around at the screen edges
            x, self._wander_dir = left, 1
        elif x >= right:
            x, self._wander_dir = right, -1
        if self._wander_facing != self._wander_dir:
            self._wander_facing = self._wander_dir
            self.update()
        self.move(x, self.y())
        if self.owner:
            self.owner.pet_moved()       # a visible bubble follows along

    # -------------------------------------------------- cursor chase (Y)

    def enable_cursor_chase(self, on: bool):
        """Opt-in oneko mode: while idle, Clawd occasionally chases the mouse
        cursor along the floor, catches it, and naps on it until it escapes."""
        self._chase_enabled = bool(on)
        if self._chase_enabled:
            self._chase_state = "wait"
            # oneko chases CONTINUOUSLY — the earlier "occasional attempts"
            # design (30-90 s waits) plus blocking phases meant user and pet
            # basically never met. _chase_next now only debounces re-entry.
            self._chase_next = time.monotonic() + 1.0
            self._chase_timer.start()
        else:
            self._chase_timer.stop()
            self._chase_state = "wait"
            self._update_mood()

    def _chase_target_pos(self):
        if self._chase_test_target is not None:   # deterministic in tests
            return self._chase_test_target
        return QCursor.pos()

    def _chase_rearm(self, now: float):
        self._chase_state = "wait"
        self._chase_next = now + random.uniform(3.0, 8.0)   # short breather
        self._update_mood()

    def _chase_blocked(self) -> bool:
        """Chasing yields to everything else the pet might be doing."""
        return (self._press_global is not None          # mid-drag
                or self.underMouse()
                or (self.owner is not None
                    and self.owner.panel.isVisible())
                or self._react_active
                or self._throw_active
                # a WAITING Claude is the perfect chase moment (the user's
                # cursor is the one moving); only real work blocks
                or (self._activity is not None
                    and self._activity[0] != "waiting")
                or self._generating
                # NO quota-mood gate: an explicitly enabled fun feature must
                # not be overruled by mood politics — a 93 % day would
                # otherwise hide it entirely (user report, three rounds)
                or (self._wander_enabled and self._wander_state == "walk"))

    def _chase_tick(self):
        if self._fetch_ball is not None:
            return                       # the ball game has priority
        now = time.monotonic()
        if self._chase_state == "caught":
            target = self._chase_target_pos()
            cx = self.x() + self.width() // 2
            if abs(target.x() - cx) > CHASE_RELEASE_PX:
                self._chase_rearm(now)               # it escaped — wake up
            return
        if self._chase_blocked():
            if self._chase_state == "chase":
                self._chase_rearm(now)
            return
        if self._chase_state == "wait":
            # engage whenever the debounce passed AND the cursor is actually
            # away — a cat next to the mouse just sits there
            target = self._chase_target_pos()
            self._mischief_tick(now, target)
            cx = self.x() + self.width() // 2
            if self._mischief_armed and abs(target.x() - cx) <= CHASE_RELEASE_PX:
                self._mischief_pinch(now, target)      # already close enough
                return
            if (now >= self._chase_next
                    and abs(target.x() - cx) > CHASE_RELEASE_PX):
                self._chase_state = "chase"
                self._chase_carry = 0.0
                self._mark_active()
                self._update_mood()                  # walk cycle on
            return
        # state "chase": scuttle horizontally toward the cursor. The pet
        # walks across the whole VIRTUAL desktop (multi-monitor), catching
        # only once it shares a screen with the cursor.
        target = self._chase_target_pos()
        avail = self._screen_avail()
        virt = QRect()
        for scr in QGuiApplication.screens():
            virt = virt.united(scr.availableGeometry())
        if not virt.contains(target):                # cursor truly gone
            self._chase_rearm(now)
            return
        cx = self.x() + self.width() // 2
        dx = target.x() - cx
        catch_px = self.width() // 2 + CHASE_STOP_SHORT_PX
        if abs(dx) <= catch_px:
            if self._mischief_armed and avail.contains(target):
                self._chase_rearm(now)
                self._mischief_pinch(now, target)    # snuck up on it — pinch!
            elif avail.contains(target):
                self._chase_state = "caught"         # gotcha — nap on it
                self._update_mood()
            else:
                self._chase_rearm(now)               # right column, other screen
            return
        direction = 1 if dx > 0 else -1
        self._chase_carry += CHASE_SPEED_PX * direction
        step = int(self._chase_carry)
        self._chase_carry -= step
        if step == 0:
            return
        left, right = virt.left(), virt.right() - self.width()
        x = max(left, min(self.x() + step, right))
        if x == self.x():                            # pinned at a screen edge
            self._chase_rearm(now)
            return
        if self._wander_facing != direction:
            self._wander_facing = direction          # reuse the blit mirror
            self.update()
        self.move(x, self.y())
        if self.owner:
            self.owner.pet_moved()

    # -------------------------------------------------- window sitting (W)

    def enable_window_sitting(self, on: bool):
        """Opt-in Shimeji mode (macOS): Clawd perches on the top edge of the
        frontmost window, follows it around and falls (throw physics) when it
        disappears. The caller persists the setting and gates the menu entry
        on macwindows.window_tracking_available().

        Anchoring is a simple one-shot reposition: the poll tick snaps Clawd
        onto the window top the first time he is idle — deliberately chosen
        over walking there, because a robust cross-screen walk toward a
        moving target is far more failure-prone than a single hop."""
        self._window_sit_enabled = bool(on)
        if self._window_sit_enabled:
            self._window_sit_timer.start()
            self._window_sit_tick()          # anchor now, not one poll later
        else:
            self._window_sit_timer.stop()
            was_sitting = self._window_sitting
            self._window_sitting = False
            self._sit_frame = None
            if was_sitting and not self._throw_active:
                self._start_throw(0.0, 0.0)  # step off: gravity to the floor

    def _window_sit_blocked(self) -> bool:
        """Sitting yields to the same interactions wandering does — but NOT
        to wandering itself: while anchored, the wander walk strolls along
        the window's top edge (see _wander_tick). Chase is handled in the
        tick, which skips entirely while a chase is running."""
        return (self._press_global is not None          # mid-drag
                or self.underMouse()                    # cursor on Clawd
                or (self.owner is not None
                    and self.owner.panel.isVisible())   # panel open
                or self._react_active                   # petting reaction
                or self._throw_active)                  # flying (F12)
        # NOTE: activity/generating/quota mood do NOT suspend sitting — the
        # whole point is Clawd perching on your window WHILE Claude works

    def _window_sit_tick(self):
        if not self._window_sit_enabled:
            return
        if self._chase_state != "wait":
            return                       # never fight the cursor chase (Y)
        try:
            frame = self._window_frame_provider()
        except Exception:
            frame = None                 # a provider bug must not kill the tick
        if self._window_sit_blocked():
            # drag/throw/work suspends sitting; it re-anchors once idle again
            self._window_sitting = False
            self._sit_frame = None
            return
        if frame is None:                # perch is gone -> fall to the floor
            if self._window_sitting:
                self._window_sitting = False
                self._sit_frame = None
                self._start_throw(0.0, 0.0)
            return
        fx, fy, fw, _fh = frame
        avail = self._screen_avail()
        left = max(avail.left(), fx)
        right = min(avail.right() - self.width(), fx + fw - self.width())
        if right < left:                 # window shrank away under the pet
            if self._window_sitting:
                self._window_sitting = False
                self._sit_frame = None
                self._start_throw(0.0, 0.0)
            return
        # feet on the window top; clamped to the screen so a window whose
        # title bar hugs the menu bar never pushes the sprite off-screen
        y = max(avail.top(), fy - self.height())
        if self._window_sitting and self._sit_frame is not None:
            x = self.x() + (fx - self._sit_frame[0])   # follow a moving window
        else:
            x = self.x()                 # first anchor: hop straight onto it
        x = max(left, min(x, right))
        if (x, y) != (self.x(), self.y()):
            self._mark_active()
            self.move(x, y)
            if self.owner:
                self.owner.pet_moved()
        self._window_sitting = True
        self._sit_frame = frame

    # -------------------------------------------------- ball fetch (inline)

    def fetch(self, ball) -> None:
        """Chase a thrown ball once it lands and collect it. Fetch overrides
        the cursor chase while active; a second ball replaces the first."""
        if self._fetch_ball is not None:
            try:
                self._fetch_ball.remove()
            except RuntimeError:
                pass
        self._fetch_ball = ball
        self._fetch_carry = 0.0
        self._mark_active()
        self._fetch_timer.start()
        self._update_mood()

    @property
    def fetching(self) -> bool:
        return self._fetch_ball is not None

    def _fetch_tick(self):
        ball = self._fetch_ball
        if ball is None:
            self._fetch_timer.stop()
            return
        try:
            alive = ball.isVisible()
        except RuntimeError:            # widget already destroyed
            alive = False
        if not alive:
            self._fetch_ball = None
            self._fetch_timer.stop()
            self._update_mood()
            return
        if not ball.landed:
            return                      # still flying — watch it
        if self._press_global is not None or self._throw_active:
            return                      # the user has priority
        cx = self.x() + self.width() // 2
        bx = ball.x() + ball.width() // 2
        dx = bx - cx
        if abs(dx) <= self.width() // 2:       # got it!
            ball.remove()
            self._fetch_ball = None
            self._fetch_timer.stop()
            cb = self.on_fetch_done
            if cb is not None:
                cb()
            if not self._react_active:         # brief happy hop
                self.celebrate()
            self._update_mood()
            return
        direction = 1 if dx > 0 else -1
        self._fetch_carry += FETCH_SPEED_PX * direction
        step = int(self._fetch_carry)
        self._fetch_carry -= step
        if step == 0:
            return
        avail = self._screen_avail()
        left, right = avail.left(), avail.right() - self.width()
        x = max(left, min(self.x() + step, right))
        if self._wander_facing != direction:
            self._wander_facing = direction
            self.update()
        self.move(x, self.y())
        if self.owner:
            self.owner.pet_moved()

    # -------------------------------------------------- mischief (opt-in)

    def enable_mischief(self, on: bool) -> None:
        """Rare, gentle cursor pinch: Clawd sneaks to a RESTING cursor and
        nudges it away. Strictly opt-in; rides on the chase machinery."""
        self._mischief_enabled = bool(on)
        self._mischief_armed = False
        self._mischief_next = (time.monotonic()
                               + random.uniform(MISCHIEF_MIN_INTERVAL_S,
                                                MISCHIEF_MAX_INTERVAL_S))

    def _mischief_tick(self, now: float, target) -> None:
        """Called from the chase tick: track cursor rest, arm when due."""
        if self._last_cursor_pos is None or target != self._last_cursor_pos:
            self._last_cursor_pos = target
            self._cursor_still_since = now
            return
        if (self._mischief_enabled and not self._mischief_armed
                and now >= self._mischief_next
                and now - self._cursor_still_since >= MISCHIEF_CURSOR_STILL_S):
            self._mischief_armed = True

    def _mischief_pinch(self, now: float, target) -> None:
        """The pinch itself: nudge the cursor and act innocent."""
        self._mischief_armed = False
        self._mischief_next = now + random.uniform(MISCHIEF_MIN_INTERVAL_S,
                                                   MISCHIEF_MAX_INTERVAL_S)
        push = MISCHIEF_PUSH_PX * (1 if self._wander_facing >= 0 else -1)
        try:
            QCursor.setPos(target.x() + push, target.y())
        except Exception:
            return
        self._last_startle = None       # allow the startle hop right away
        self._startle()

    # -------------------------------------------------- throw physics (F12)

    @property
    def _throw_active(self) -> bool:
        return self._throw_on

    def _release_velocity(self):
        """Release speed in px/s, fitted over the freshest drag samples."""
        now = time.monotonic()
        pts = [(t, p) for t, p in self._drag_samples
               if now - t <= self.THROW_SAMPLE_WINDOW_S]
        if len(pts) < 2:
            return 0.0, 0.0
        (t0, p0), (t1, p1) = pts[0], pts[-1]
        dt = t1 - t0
        if dt <= 0.0:
            return 0.0, 0.0
        return (p1.x() - p0.x()) / dt, (p1.y() - p0.y()) / dt

    def _start_throw(self, vx: float, vy: float):
        self._mark_active()
        self._throw_on = True
        self._throw_v = [vx, vy]
        self._throw_pos = [float(self.x()), float(self.y())]
        self._throw_deadline = time.monotonic() + self.THROW_TIMEOUT_S
        self._throw_timer.start()

    def _stop_throw(self):
        self._throw_on = False
        self._throw_timer.stop()

    def _throw_step(self, dt: float, avail: QRect) -> bool:
        """One physics integration step; returns True while still flying.

        Kept free of timers and screen lookups so the trajectory is testable
        headless: velocity/position live in plain floats, bounces mirror at
        the given rect and the flight ends when Clawd rests on the floor.
        """
        vx, vy = self._throw_v
        vy += THROW_GRAVITY * dt
        x = self._throw_pos[0] + vx * dt
        y = self._throw_pos[1] + vy * dt
        left, right = avail.left(), avail.right() - self.width()
        top, bottom = avail.top(), avail.bottom() - self.height()
        if x < left:
            x, vx = left, -vx * THROW_BOUNCE
        elif x > right:
            x, vx = right, -vx * THROW_BOUNCE
        if y > bottom:                   # floor bounce
            y = bottom
            vy = -vy * THROW_BOUNCE
            vx *= THROW_FRICTION
        elif y < top:                    # ceiling bounce
            y = top
            vy = -vy * THROW_BOUNCE
            vx *= THROW_FRICTION
        self._throw_v = [vx, vy]
        self._throw_pos = [x, y]
        self.move(int(round(x)), int(round(y)))
        if self.owner:
            self.owner.pet_moved()
        speed = (vx * vx + vy * vy) ** 0.5
        grounded = y >= bottom - 0.5
        if ((speed < THROW_STOP_SPEED and grounded)
                or time.monotonic() >= self._throw_deadline):
            self._throw_on = False
            return False
        return True

    def _throw_tick(self):
        if not self._throw_on:
            self._throw_timer.stop()
            return
        if not self._throw_step(self.THROW_TICK_MS / 1000.0,
                                self._screen_avail()):
            self._throw_timer.stop()
            if self.owner:
                self.owner.save_position()

    # -------------------------------------------------- click-through (F8)

    def set_click_through(self, on: bool):
        """Let clicks pass through the widget area next to the sprite (F8).

        Simplification: the input mask is the BOUNDING BOX of the current
        frame, not a pixel-exact silhouette. setMask() also clips painting,
        so a pixel mask would visibly cut the mood cross-dissolve at the
        sprite edge; the box keeps the fade intact while clicks land on the
        window below wherever the (widest-mood-wide) widget is empty.
        Dragging and petting keep working anywhere INSIDE the box — that is
        intended. With the vector fallback (no sprites) this is a no-op.
        """
        self._click_through = bool(on)
        if self._click_through:
            self._apply_input_mask()
        else:
            self.clearMask()

    def _apply_input_mask(self):
        sprite = self._sprites.sprite(self.mood)
        if sprite is None or not sprite.pixmaps:
            return                       # vector fallback: keep full input
        pm = sprite.pixmaps[0]           # all frames of a mood share one size
        x = (self.width() - pm.width()) // 2
        y = self.height() - pm.height()  # positioned like _blit: feet down
        self.setMask(QRegion(x, y, pm.width(), pm.height()))

    # -------------------------------------------------- hover handling

    def enterEvent(self, event):
        self._mark_active()
        if self.owner:
            self.owner.hover_panel()
        self._startle()                # approaching a sleeping Clawd wakes him
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self.owner:
            self.owner.schedule_panel_hide()
        super().leaveEvent(event)
