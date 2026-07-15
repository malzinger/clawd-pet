"""Hats — level-unlocked cosmetics + seasonal defaults.

Pure logic plus QPainter pixel-art generation: no widgets, no app coupling.
Regular hats are level rewards and sit on top of the sprite ("anchor": "top");
the two seasonal ones (santa, sunglasses) are never listed as level rewards —
they are only surfaced by season_default() when the user has not picked a hat.
The sunglasses overlay the eye region instead ("anchor": "eyes").
"""
import functools
from datetime import date
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QPainter, QPixmap

# Ordered registry: key -> definition. Non-seasonal min_levels are monotonic
# in registry order (the menu shows them in exactly this order).
HATS = {
    "none":       {"label_key": "hat_none",       "min_level": 0,  "anchor": "top"},
    "party":      {"label_key": "hat_party",      "min_level": 2,  "anchor": "top"},
    "hardhat":    {"label_key": "hat_hardhat",    "min_level": 4,  "anchor": "top"},
    "beret":      {"label_key": "hat_beret",      "min_level": 6,  "anchor": "top"},
    "pirate":     {"label_key": "hat_pirate",     "min_level": 8,  "anchor": "top"},
    "wizard":     {"label_key": "hat_wizard",     "min_level": 12, "anchor": "top"},
    "crown":      {"label_key": "hat_crown",      "min_level": 18, "anchor": "top"},
    # seasonal defaults — excluded from unlocked_hats(), min_level 0 because
    # season_default() may hand them to a brand-new pet
    "santa":      {"label_key": "hat_santa",      "min_level": 0,  "anchor": "top",
                   "seasonal": True},
    "sunglasses": {"label_key": "hat_sunglasses", "min_level": 0,  "anchor": "eyes",
                   "seasonal": True},
}

# hat width relative to the sprite width (target band: ~55-70 %)
_HAT_WIDTH_FRACTION = 0.62

_PALETTE = {
    "R": QColor("#c0392b"),   # deep red (beret, party stripe)
    "r": QColor("#e74c3c"),   # bright red (santa cone)
    "Y": QColor("#f1c40f"),   # gold / safety yellow
    "y": QColor("#f9e79f"),   # pale yellow highlight
    "D": QColor("#c9980c"),   # darker gold (hardhat brim shadow)
    "G": QColor("#2ecc71"),   # green
    "B": QColor("#2980b9"),   # wizard blue
    "s": QColor("#f6d743"),   # star yellow
    "K": QColor("#1b1b1b"),   # black
    "k": QColor("#3d3d3d"),   # dark-gray lens glint
    "w": QColor("#f5f5f5"),   # white
    "o": QColor("#ff8fb1"),   # pink pompom
    "c": QColor("#3dd6d0"),   # cyan gem
    "m": QColor("#c86bd9"),   # magenta gem
}

# Each hat is a grid of logical pixels ("." = transparent). Every grid cell is
# painted as one blocky square via QPainter, matching the sprite aesthetic.
_PIXEL_ART = {
    # colorful cone + pompom
    "party": (
        "....ooo....",
        "....ooo....",
        ".....R.....",
        "....RRR....",
        "....YYY....",
        "...GGGGG...",
        "...BBBBB...",
        "..RRRRRRR..",
        ".YYYYYYYYY.",
        "GGGGGGGGGGG",
    ),
    # yellow dome + brim
    "hardhat": (
        "....YYYYY....",
        "...YYyyyYY...",
        "..YYYyyyYYY..",
        "..YYYYYYYYY..",
        "..YYYYYYYYY..",
        "YYYYYYYYYYYYY",
        ".DDDDDDDDDDD.",
    ),
    # tilted red disc with a stem
    "beret": (
        ".......K....",
        "...RRRRR....",
        ".RRRRRRRRR..",
        "RRRRRRRRRRR.",
        ".RRRRRRRRRR.",
        "...RRRRRR...",
    ),
    # black tricorn + white skull dot
    "pirate": (
        ".....KKK.....",
        "...KKKKKKK...",
        ".KKKKKKKKKKK.",
        "KKKKKKwKKKKKK",
        "KKKKKwwwKKKKK",
        "KK.........KK",
    ),
    # blue cone + stars + brim
    "wizard": (
        "......B......",
        ".....BBB.....",
        ".....BBB.....",
        "....BBsBB....",
        "....BBBBB....",
        "...BBBBBBB...",
        "...BBsBBBB...",
        "..BBBBBBBBB..",
        "..BBBBBsBBB..",
        "BBBBBBBBBBBBB",
        ".BBBBBBBBBBB.",
    ),
    # gold band + three spikes + gem dots
    "crown": (
        "Y....Y....Y",
        "Y....Y....Y",
        "YY...Y...YY",
        "YYY.YYY.YYY",
        "YYYYYYYYYYY",
        "YcYYYmYYYcY",
        "YYYYYYYYYYY",
    ),
    # red cone flopping right + white brim and pompom (December default)
    "santa": (
        "........ww",
        "......rrww",
        ".....rrrr.",
        "....rrrrr.",
        "...rrrrrr.",
        "..rrrrrrr.",
        ".rrrrrrrr.",
        "wwwwwwwwww",
        "wwwwwwwwww",
    ),
    # black shades bar over the eyes (summer default)
    "sunglasses": (
        "KKKKKKKKKKKKK",
        ".KKKK.K.KKKK.",
        ".KkkK...KkkK.",
        ".KKKK...KKKK.",
    ),
}


@functools.lru_cache(maxsize=64)
def hat_pixmap(key: str, width: int) -> Optional[QPixmap]:
    """Blocky pixel-art hat scaled for a sprite `width` px wide (None for
    "none"/unknown). Cached per (key, width): identical args, identical object.
    """
    rows = _PIXEL_ART.get(key)
    if rows is None:
        return None
    grid_w, grid_h = len(rows[0]), len(rows)
    px = max(1, int(round(width * _HAT_WIDTH_FRACTION / grid_w)))
    pm = QPixmap(grid_w * px, grid_h * px)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, False)
    for gy, row in enumerate(rows):
        for gx, ch in enumerate(row):
            if ch != ".":
                painter.fillRect(gx * px, gy * px, px, px, _PALETTE[ch])
    painter.end()
    return pm


def unlocked_hats(level: int) -> list:
    """Level-reward hats unlocked at `level`, in menu order. Always contains
    "none"; seasonal hats are never level rewards, so they never appear here.
    """
    return [k for k, d in HATS.items()
            if not d.get("seasonal") and level >= d["min_level"]]


def hat_available(key: str, level: int) -> bool:
    """True when `key` exists and its level requirement is met (seasonal hats
    have min_level 0, so an active season default is always wearable)."""
    d = HATS.get(key)
    return d is not None and level >= d["min_level"]


def season_default(today: date) -> str:
    """Date-driven default hat when the user has not picked one:
    Dec 1-31 -> "santa", Jun 1 - Aug 31 -> "sunglasses", else "none"."""
    if today.month == 12:
        return "santa"
    if 6 <= today.month <= 8:
        return "sunglasses"
    return "none"


def anchor_for(key: str) -> str:
    """Where the hat attaches on the sprite: "top" (head) or "eyes"."""
    d = HATS.get(key)
    return d["anchor"] if d else "top"
