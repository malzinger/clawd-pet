"""Clawd artwork: programmatic vector fallback + animated GIF sprites."""
import functools
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QRect, QRectF, Qt
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QImage,
    QImageReader,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

from .config import PET_HEIGHT, SPRITE_DIR, SPRITE_FILES

# ======================================================================
#  Clawd artwork — programmatic vector/pixel rendering
# ======================================================================

@dataclass
class ArtState:
    mood: str = "chill"
    frame: int = 0
    blink: bool = False
    cursor_on: bool = True
    glitch_seed: int = 0
    sweat_t: float = 0.0     # 0..1, position of the sweat drop along its path


class ClawdArt:
    """Draws Clawd into any QPainter. Logical canvas: W x H units."""

    W, H = 144.0, 126.0

    BODY = QColor("#7fbcf4")
    BODY_SHADE = QColor("#5c9bd8")
    OUTLINE = QColor("#22334e")
    BEZEL = QColor("#16223b")
    SCREEN = QColor("#0b1220")
    TEXT = QColor("#63f5a6")
    LIMIT_TEXT = QColor("#ffd966")
    LIMIT_ACCENT = QColor("#ffb4a0")

    @classmethod
    def body_path(cls) -> QPainterPath:
        path = QPainterPath()
        path.setFillRule(Qt.WindingFill)
        path.addRoundedRect(QRectF(14, 54, 112, 54), 26, 26)   # base slab
        path.addEllipse(QRectF(18, 30, 46, 46))                # left bump
        path.addEllipse(QRectF(46, 16, 52, 54))                # middle bump
        path.addEllipse(QRectF(80, 30, 44, 44))                # right bump
        return path.simplified()

    @classmethod
    def draw(cls, p: QPainter, target: QRectF, st: ArtState):
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)

        # Fit the logical canvas into the target rect, centered.
        s = min(target.width() / cls.W, target.height() / cls.H)
        p.translate(
            target.x() + (target.width() - cls.W * s) / 2.0,
            target.y() + (target.height() - cls.H * s) / 2.0,
        )
        p.scale(s, s)

        outline = QPen(cls.OUTLINE, 4)
        outline.setJoinStyle(Qt.RoundJoin)
        outline.setCapStyle(Qt.RoundCap)

        # --- feet -----------------------------------------------------
        p.setPen(outline)
        p.setBrush(QBrush(cls.BODY_SHADE))
        p.drawRoundedRect(QRectF(38, 104, 20, 14), 5, 5)
        p.drawRoundedRect(QRectF(82, 104, 20, 14), 5, 5)

        # --- antenna ---------------------------------------------------
        p.drawRoundedRect(QRectF(67, 12, 6, 12), 2, 2)
        p.setBrush(QBrush(cls.BODY))
        p.drawEllipse(QRectF(62, 3, 16, 16))

        # --- cloud body ------------------------------------------------
        body = cls.body_path()
        p.setBrush(QBrush(cls.BODY))
        p.setPen(outline)
        p.drawPath(body)

        # soft bottom shade + top highlight, clipped to the body
        p.save()
        p.setClipPath(body)
        shade = QColor(cls.BODY_SHADE)
        shade.setAlpha(130)
        p.fillRect(QRectF(14, 90, 112, 20), shade)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 45))
        p.drawEllipse(QRectF(28, 26, 34, 20))
        p.restore()

        # --- terminal screen (face + chest) ----------------------------
        pulse = 0.5 + 0.5 * math.sin(st.frame * 0.35)
        p.setPen(outline)
        p.setBrush(QBrush(cls.BEZEL))
        p.drawRoundedRect(QRectF(27, 43, 86, 60), 12, 12)

        if st.mood == "limit":
            screen_col = QColor(min(126, 58 + int(68 * pulse)), 16, 12)
        else:
            screen_col = cls.SCREEN
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(screen_col))
        p.drawRoundedRect(QRectF(31, 47, 78, 52), 8, 8)

        # subtle CRT scanlines
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(31, 47, 78, 52), 8, 8)
        p.setClipPath(clip)
        for y in range(50, 98, 6):
            p.fillRect(QRectF(31, y, 78, 1.5), QColor(255, 255, 255, 10))
        p.restore()

        # limit mode: pulsing warning ring around the bezel
        if st.mood == "limit":
            ring = QPen(QColor(255, 90, 60, int(70 + 90 * pulse)), 5)
            p.setBrush(Qt.NoBrush)
            p.setPen(ring)
            p.drawRoundedRect(QRectF(27, 43, 86, 60), 12, 12)

        cls._draw_face(p, st)

        # --- sweat drop (panic) ----------------------------------------
        if st.mood == "panic":
            cls._draw_sweat(p, st)

        p.restore()

    # ------------------------------------------------------------------

    @classmethod
    def _glow_rect(cls, p: QPainter, rect: QRectF, color: QColor):
        glow = QColor(color)
        glow.setAlpha(55)
        p.fillRect(rect.adjusted(-2.5, -2.5, 2.5, 2.5), glow)
        p.fillRect(rect, color)

    @classmethod
    def _draw_chevron(cls, p: QPainter, x: float, y: float, color: QColor, px: float = 4.0):
        """Pixel-art '>' built from 5 stacked blocks."""
        steps = [(0, 0), (1, 1), (2, 2), (1, 3), (0, 4)]
        for cx, cy in steps:
            cls._glow_rect(p, QRectF(x + cx * px, y + cy * px, px, px), color)

    @classmethod
    def _draw_face(cls, p: QPainter, st: ArtState):
        rng = random.Random(st.glitch_seed)
        jx = jy = 0.0
        if st.mood == "panic":
            jx = rng.uniform(-1.2, 1.2)
            jy = rng.uniform(-0.8, 0.8)

        p.setPen(Qt.NoPen)

        # --- eyes ------------------------------------------------------
        if st.mood == "limit":
            # shocked wide white eyes with dark pupils
            for ex in (46, 82):
                p.setBrush(QColor(255, 244, 230))
                p.drawRoundedRect(QRectF(ex, 51, 12, 14), 3, 3)
                p.setBrush(QColor(40, 12, 10))
                p.drawRect(QRectF(ex + 4, 55, 4, 6))
        else:
            eye_col = cls.TEXT
            if st.blink:
                rects = [QRectF(48 + jx, 62 + jy, 10, 3), QRectF(82 + jx, 62 + jy, 10, 3)]
            elif st.mood == "focus":
                rects = [QRectF(48 + jx, 57 + jy, 10, 7), QRectF(82 + jx, 57 + jy, 10, 7)]
            else:
                rects = [QRectF(48 + jx, 54 + jy, 10, 12), QRectF(82 + jx, 54 + jy, 10, 12)]
            for r in rects:
                cls._glow_rect(p, r, eye_col)

        # --- prompt line -----------------------------------------------
        if st.mood == "limit":
            cls._draw_chevron(p, 44, 74, cls.LIMIT_ACCENT)
            # exclamation mark: bar + dot
            cls._glow_rect(p, QRectF(64, 74, 6, 13), cls.LIMIT_TEXT)
            cls._glow_rect(p, QRectF(64, 91, 6, 5), cls.LIMIT_TEXT)
        elif st.mood == "panic":
            # chromatic-aberration glitch copies, then the jittering prompt
            if rng.random() < 0.5:
                cls._draw_chevron(p, 44 - 2 + jx, 74 + jy, QColor(255, 70, 90, 150))
                cls._draw_chevron(p, 44 + 2 + jx, 74 + jy, QColor(80, 230, 255, 150))
            cls._draw_chevron(p, 44 + jx, 74 + jy, cls.TEXT)
            if st.cursor_on:
                cls._glow_rect(p, QRectF(62 + jx, 88 + jy, 12, 5), cls.TEXT)
            # random noise blocks flickering on the screen
            for _ in range(rng.randint(0, 4)):
                nx = rng.uniform(34, 100)
                ny = rng.uniform(50, 92)
                nc = rng.choice([QColor(255, 70, 90, 120), QColor(80, 230, 255, 120),
                                 QColor(255, 255, 255, 90)])
                p.fillRect(QRectF(nx, ny, rng.uniform(3, 9), 2.5), nc)
        else:
            cls._draw_chevron(p, 44, 74, cls.TEXT)
            if st.cursor_on:
                cls._glow_rect(p, QRectF(62, 88, 12, 5), cls.TEXT)

    @classmethod
    def _draw_sweat(cls, p: QPainter, st: ArtState):
        # slides down along the right bump, fading near the end
        t = st.sweat_t
        x = 106 + 10 * t
        y = 24 + 32 * t
        alpha = 255 if t < 0.7 else max(0, int(255 * (1.0 - (t - 0.7) / 0.3)))

        drop = QPainterPath()
        drop.moveTo(x + 5, y - 5)               # tip
        drop.cubicTo(x + 9, y + 2, x + 10, y + 6, x + 5, y + 9)
        drop.cubicTo(x, y + 6, x + 1, y + 2, x + 5, y - 5)

        fill = QColor("#a9ddf9")
        fill.setAlpha(alpha)
        edge = QColor("#4a90c2")
        edge.setAlpha(alpha)
        p.setPen(QPen(edge, 2))
        p.setBrush(QBrush(fill))
        p.drawPath(drop)
        hl = QColor(255, 255, 255, int(alpha * 0.8))
        p.setPen(Qt.NoPen)
        p.setBrush(hl)
        p.drawEllipse(QRectF(x + 2.5, y + 1, 2.5, 3.5))


def make_clawd_pixmap(size: int, mood: str = "chill") -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    ClawdArt.draw(painter, QRectF(0, 0, size, size),
                  ArtState(mood=mood, cursor_on=True))
    painter.end()
    return pm


def make_clawd_icon(mood: str = "chill") -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128):
        icon.addPixmap(make_clawd_pixmap(size, mood))
    return icon


# ======================================================================
#  Sprite rendering — animated GIF frames of the pixel-art mascot
# ======================================================================

def _alpha_bbox(img: QImage) -> QRect:
    """Bounding box of the non-transparent pixels of one frame."""
    img = img.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    top = bottom = None
    left, right = w, -1
    for y in range(h):
        ptr = img.constScanLine(y)
        ptr.setsize(img.bytesPerLine())
        alphas = bytes(ptr)[3:w * 4:4]
        if not any(alphas):
            continue
        if top is None:
            top = y
        bottom = y
        first = next(i for i, a in enumerate(alphas) if a)
        if first < left:
            left = first
        last = len(alphas) - 1 - next(i for i, a in enumerate(reversed(alphas)) if a)
        if last > right:
            right = last
    if top is None:
        return QRect(0, 0, w, h)
    return QRect(left, top, right - left + 1, bottom - top + 1)


class Sprite:
    """One mood animation, decoded up front so we control frame timing.

    QMovie snaps from the last frame straight back to the first, which reads as
    a hard cut. Owning the frames lets us cross-dissolve the tail of the loop
    into its own first frame, so the seam disappears.
    """

    MAX_FRAMES = 120

    def __init__(self, path: Path):
        reader = QImageReader(str(path))
        self.images = []
        self.delays = []
        bbox = QRect()
        while len(self.images) < self.MAX_FRAMES:
            img = reader.read()
            if img.isNull():
                break
            img = img.convertToFormat(QImage.Format_ARGB32)
            box = _alpha_bbox(img)
            bbox = box if bbox.isNull() else bbox.united(box)
            self.images.append(img)
            self.delays.append(max(20, reader.nextImageDelay() or 80))

        if self.images and not bbox.isNull():
            bbox = bbox.adjusted(-2, -2, 2, 2).intersected(self.images[0].rect())
        self.bbox = bbox
        self.duration = sum(self.delays)
        self.starts = []
        acc = 0
        for d in self.delays:
            self.starts.append(acc)
            acc += d
        self.pixmaps = []

    def build(self, scale: float):
        """Crop to content and pre-scale every frame once."""
        w = max(1, int(round(self.bbox.width() * scale)))
        h = max(1, int(round(self.bbox.height() * scale)))
        self.pixmaps = [
            QPixmap.fromImage(img.copy(self.bbox)).scaled(
                w, h, Qt.IgnoreAspectRatio, Qt.FastTransformation)
            for img in self.images
        ]
        # the full-canvas source frames are never read again; drop them so a
        # 24/7 tray app does not hold ~200 MB of decoded QImages for its lifetime
        self.images = []

    def frame_index(self, pos_ms: int) -> int:
        idx = 0
        for i, start in enumerate(self.starts):
            if pos_ms >= start:
                idx = i
            else:
                break
        return idx

    def frame_at(self, elapsed_ms: int) -> int:
        """Ping-pong playback: forward, then backward. The animation never
        jumps back to frame 0, so there is no loop seam to hide."""
        if self.duration <= 0:
            return 0
        span = self.duration * 2
        pos = elapsed_ms % span
        if pos >= self.duration:
            pos = span - pos - 1
        return self.frame_index(pos)


class SpriteSet:
    """Loads the per-mood animations and scales them to one common size.

    height picks the on-screen pixel height (F2: size presets), sprite_dir
    an alternative sprite pack folder (F13); None means the bundled
    SPRITE_DIR. A folder without any known SPRITE_FILES gif loads nothing,
    so the vector fallback takes over exactly like with a missing folder.
    """

    def __init__(self, height: int = PET_HEIGHT,
                 sprite_dir: Optional[Path] = None):
        self.sprites = {}
        base = SPRITE_DIR if sprite_dir is None else Path(sprite_dir)
        if not base.is_dir():
            return
        for mood, fname in SPRITE_FILES.items():
            fp = base / fname
            if not fp.is_file():
                continue
            sprite = Sprite(fp)
            if sprite.images and not sprite.bbox.isNull():
                self.sprites[mood] = sprite
        if not self.sprites:
            return
        # One shared scale factor keeps Clawd the same size in every mood —
        # per-mood "fill the widget" scaling made him grow and shrink.
        tallest = max(s.bbox.height() for s in self.sprites.values())
        scale = height / tallest
        for sprite in self.sprites.values():
            sprite.build(scale)
        self.width = max(s.pixmaps[0].width() for s in self.sprites.values())
        self.height = height

    def sprite(self, mood: str) -> Optional[Sprite]:
        return self.sprites.get(mood)


@functools.lru_cache(maxsize=32)
def sprite_pixmap(mood: str, size: int) -> Optional[QPixmap]:
    """First frame of a mood GIF, cropped to content, crisply scaled."""
    fp = SPRITE_DIR / SPRITE_FILES.get(mood, "")
    if not fp.is_file():
        return None
    img = QImageReader(str(fp)).read()
    if img.isNull():
        return None
    box = _alpha_bbox(img).adjusted(-2, -2, 2, 2).intersected(img.rect())
    pm = QPixmap.fromImage(img).copy(box)
    return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.FastTransformation)


def make_app_icon(mood: str = "chill") -> QIcon:
    pm = sprite_pixmap(mood, 128)
    if pm is not None:
        return QIcon(pm)
    return make_clawd_icon(mood)


# ======================================================================
#  Sprite-pack import (Y) — petdex / "Codex pet" community packs
# ======================================================================

# Community packs (petdex.dev, clawd-on-desk "Codex pet" zips, hand-made
# folders) name their animations by STATE, not by our gif filenames. This
# maps the state names seen in the wild onto our mood keys; the first file
# matching a mood wins. Substring match, longest names first, so "sleeping"
# beats "sleep" beats "s".
PACK_STATE_MOODS = {
    "idle": "chill", "default": "chill", "stand": "chill",
    "walk": "carry", "walking": "carry", "move": "carry", "run": "carry",
    "carry": "carry", "carrying": "carry",
    "sleep": "sleep", "sleeping": "sleep", "rest": "sleep",
    "celebrate": "happy", "dance": "happy", "happy": "happy", "cheer": "happy",
    "work": "focus", "working": "focus", "build": "focus", "building": "focus",
    "think": "think", "thinking": "think",
    "type": "type", "typing": "type",
    "read": "read", "reading": "read",
    "error": "panic", "panic": "panic", "debug": "panic", "sad": "panic",
    "notify": "notify", "alert": "notify", "wave": "notify",
    "pet": "pet", "jump": "pet",
    "annoyed": "annoyed", "angry": "annoyed", "grumpy": "annoyed",
    "juggle": "juggle", "juggling": "juggle",
    "conduct": "conduct", "conducting": "conduct",
    "sweep": "sweep", "sweeping": "sweep", "clean": "sweep",
    "limit": "limit", "dead": "limit",
}
_PACK_EXTS = (".gif", ".png", ".webp", ".apng")


def _pack_mood_for(name: str) -> Optional[str]:
    stem = Path(name).stem.lower().replace("_", "-")
    for prefix in ("clawd-", "pet-", "codex-"):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
    for state in sorted(PACK_STATE_MOODS, key=len, reverse=True):
        if state in stem:
            return PACK_STATE_MOODS[state]
    return None


def _pack_sources(src: Path) -> dict:
    """mood -> source file, from a manifest.json when present, else by name."""
    out = {}
    manifest = src / "manifest.json"
    if manifest.is_file():
        import json
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        states = None
        if isinstance(data, dict):
            for key in ("states", "animations", "sprites"):
                if isinstance(data.get(key), dict):
                    states = data[key]
                    break
        if states:
            for state, fname in states.items():
                if not isinstance(fname, str):
                    continue
                mood = (PACK_STATE_MOODS.get(str(state).lower())
                        or _pack_mood_for(str(state)))
                fp = src / fname
                if mood and mood not in out and fp.is_file():
                    out[mood] = fp
            if out:
                return out
    for fp in sorted(src.rglob("*")):
        if not fp.is_file() or fp.suffix.lower() not in _PACK_EXTS:
            continue
        mood = _pack_mood_for(fp.name)
        if mood and mood not in out:
            out[mood] = fp
    return out


def import_sprite_pack(path, dest_root: Optional[Path] = None) -> Optional[Path]:
    """Convert a community sprite pack (folder or .zip) into a folder our
    SpriteSet can load, under ~/.clawd/sprite_packs/<name>/ (or dest_root).

    Best effort: unknown states are skipped (SpriteSet falls back to idle),
    but a pack without an idle animation is rejected. Returns the resulting
    sprite_dir, or None when nothing usable was found. Pure logic, no Qt
    dialogs — QImageReader sniffs content, so .png/.webp sources may keep
    their bytes under our .gif target names.
    """
    import shutil
    import tempfile
    import zipfile
    src = Path(path)
    if dest_root is None:
        dest_root = Path.home() / ".clawd" / "sprite_packs"
    tmp = None
    try:
        if src.is_file() and src.suffix.lower() == ".zip":
            tmp = tempfile.TemporaryDirectory(prefix="clawd-pack-")
            try:
                with zipfile.ZipFile(src) as zf:
                    zf.extractall(tmp.name)   # noqa: S202 — local user archive
            except (OSError, zipfile.BadZipFile):
                return None
            root = Path(tmp.name)
            entries = [e for e in root.iterdir() if not e.name.startswith(".")]
            src_dir = entries[0] if len(entries) == 1 and entries[0].is_dir() else root
        elif src.is_dir():
            src_dir = src
        else:
            return None
        sources = _pack_sources(src_dir)
        if "chill" not in sources:        # no idle animation -> unusable pack
            return None
        name = "".join(c if c.isalnum() or c in "-_" else "-"
                       for c in src.stem).strip("-") or "pack"
        dest = dest_root / name
        try:
            if dest.is_dir():
                shutil.rmtree(dest)       # replace an older import of this pack
            dest.mkdir(parents=True, exist_ok=True)
            for mood, fp in sources.items():
                shutil.copy2(fp, dest / SPRITE_FILES[mood])
        except OSError:
            return None
        return dest
    finally:
        if tmp is not None:
            tmp.cleanup()
