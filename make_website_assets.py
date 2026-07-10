#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Render marketing assets for the Clawd website into ./website/assets/.

    py make_website_assets.py

Produces (transparent where it makes sense, 2x for crisp retina display):
    * clawd-<mood>.png   — the pixel mascot in each of the five moods
    * panel.png          — the usage panel with realistic, calibrated numbers
    * hero.png           — pet + panel composed on a dark rounded card
Reuses the real application code so the assets always match the shipped app.
"""

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PyQt5.QtCore import Qt, QRectF
from PyQt5.QtGui import QColor, QLinearGradient, QPainter, QPixmap
from PyQt5.QtWidgets import QApplication

import clawd_pet as cp

OUT = Path(__file__).resolve().parent / "website" / "assets"
SCALE = 2                    # retina
MOODS = ["sleep", "chill", "focus", "panic", "limit"]


def sprite_frame(sprites: cp.SpriteSet, mood: str, upscale: int) -> QPixmap:
    """A crisp, content-cropped frame of one mood, nearest-neighbour upscaled."""
    sprite = sprites.sprite(mood)
    if sprite is None or not sprite.pixmaps:
        return cp.make_clawd_pixmap(128 * upscale, mood)
    base = sprite.pixmaps[len(sprite.pixmaps) // 3]     # a lively mid frame
    return base.scaled(base.width() * upscale, base.height() * upscale,
                       Qt.KeepAspectRatio, Qt.FastTransformation)


def build_panel(app: QApplication) -> cp.PanelWidget:
    """A panel showing realistic Fable-heavy numbers (calibrated budget)."""
    cp.set_max_tokens_override(280_000)
    snap = cp.UsageSnapshot(updated_at=datetime.now())
    snap.source = "logs"
    snap.input_tokens = 14_480
    snap.output_tokens = 176_400
    snap.total = snap.input_tokens + snap.output_tokens
    snap.entries = 512
    snap.oldest = datetime.now(timezone.utc) - timedelta(hours=1, minutes=8)
    snap.by_model = {"Fable": 156_900, "Opus": 33_980}
    snap.pct = snap.total / cp.effective_max_tokens() * 100.0

    panel = cp.PanelWidget()
    panel.update_snapshot(snap)
    panel.move(-4000, -4000)
    panel.show()
    app.processEvents()
    end = time.monotonic() + 1.0
    while time.monotonic() < end:      # let the bar animations settle
        app.processEvents()
    return panel


def rounded_card(w: int, h: int, radius: int) -> QPixmap:
    pm = QPixmap(w, h)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    grad = QLinearGradient(0, 0, 0, h)
    grad.setColorAt(0.0, QColor("#2b2926"))
    grad.setColorAt(1.0, QColor("#191817"))
    p.setPen(Qt.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(QRectF(0, 0, w, h), radius, radius)
    p.end()
    return pm


def main() -> int:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    OUT.mkdir(parents=True, exist_ok=True)

    sprites = cp.SpriteSet()

    # 1) individual mood sprites (transparent)
    for mood in MOODS:
        sprite_frame(sprites, mood, upscale=3).save(str(OUT / f"clawd-{mood}.png"))
    print(f"[assets] mood sprites: {MOODS}")

    # 2) the panel alone
    panel = build_panel(app)
    panel_pm = panel.grab()
    panel_pm.save(str(OUT / "panel.png"))
    print(f"[assets] panel.png {panel_pm.width()}x{panel_pm.height()}")

    # 3) hero composition: card + panel + pet
    hero_pet = sprite_frame(sprites, "focus", upscale=3)
    margin = 48 * SCALE
    gap = 40 * SCALE
    pw, ph = panel_pm.width(), panel_pm.height()
    petw, peth = hero_pet.width(), hero_pet.height()
    content_h = max(ph, peth)
    W = margin * 2 + pw + gap + petw
    H = margin * 2 + content_h
    card = rounded_card(W, H, 28 * SCALE)

    p = QPainter(card)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, False)
    p.drawPixmap(margin, margin + (content_h - ph) // 2, panel_pm)
    p.drawPixmap(margin + pw + gap, margin + content_h - peth, hero_pet)
    p.end()
    card.save(str(OUT / "hero.png"))
    print(f"[assets] hero.png {W}x{H}")

    panel.hide()
    print(f"[assets] written to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
