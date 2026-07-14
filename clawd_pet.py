#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clawd — Claude Code Desktop Pet & Usage Widget
==============================================

Entry point. The implementation lives in the clawdpet/ package:

    config     static configuration (plan constants, paths, weights, timings)
    i18n       DE/EN strings, number formats, tool phrases
    usage      log scanning, 5-h window replay, calibration (pure logic)
    api        read-only live sync with the Anthropic usage endpoint
    activity   session-log tail parsing (what Claude is doing right now)
    hooks      opt-in Claude Code hooks + datagram authentication
    moods      quota level + running tool -> animation mapping
    autostart  Windows Run key toggle
    update     GitHub release check
    history    local usage history + sparkline
    art        vector Clawd + GIF sprite rendering
    pet        the always-on-top mascot widget
    panel      the slide-out usage panel
    bubble     the transient speech bubble
    app        controller, tray, entry point
    selftest   headless smoke test

Setup
-----
    pip install PyQt5
    python clawd_pet.py

    # optional headless smoke test (scans logs, renders all moods offscreen):
    python clawd_pet.py --selftest

Usage
-----
    * Drag Clawd anywhere with the left mouse button.
    * Hover over Clawd to peek at the usage panel; left-click to pin it open.
    * Right-click Clawd (or the tray icon) for refresh / hide / quit.
    * The window position is remembered between runs.

Platform notes
--------------
    * Windows / macOS: transparency works out of the box.
    * Linux: a compositing window manager is required for the transparent
      background (KDE/GNOME default compositors are fine).
"""
import sys

# Re-export the public API so `import clawd_pet as cp` keeps working
# (make_website_assets.py and user scripts rely on these names).
from clawdpet.api import UsageBucket, collect_usage                    # noqa: F401
from clawdpet.app import ClawdApp, main                                # noqa: F401
from clawdpet.art import (                                             # noqa: F401
    SpriteSet,
    make_app_icon,
    make_clawd_icon,
    make_clawd_pixmap,
    sprite_pixmap,
)
from clawdpet.bubble import SpeechBubble                               # noqa: F401
from clawdpet.i18n import language, set_language, tr                  # noqa: F401
from clawdpet.panel import PanelWidget                                 # noqa: F401
from clawdpet.pet import PetWidget                                     # noqa: F401
from clawdpet.usage import (                                           # noqa: F401
    UsageSnapshot,
    effective_max_tokens,
    scan_usage,
    set_max_tokens_override,
)

if __name__ == "__main__":
    sys.exit(main())
