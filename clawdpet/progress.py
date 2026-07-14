"""Gamification progress (pure logic, no Qt) — the pet "eats" tokens into XP.

Every weighted token unit Clawd sees is food: 1000 units digest into 1 XP,
XP crosses a gently quadratic level curve, and level bands map to evolution
titles (AgentPet/Tamamon style). Deliberately NO decay/neglect mechanics —
the community finds punishing absence polarizing, so progress only grows.
"""
import json
from pathlib import Path
from typing import Optional

# 1 XP per 1000 weighted token units (Sonnet-input-token equivalents).
XP_PER_UNIT = 1.0 / 1000.0

# Evolution titles by level band, highest band first.
TITLE_BANDS = (
    (28, "Legend"),
    (21, "Kraken Whisperer"),
    (15, "Deep-Sea Dev"),
    (10, "Coder Crab"),
    (6, "Scuttler"),
    (3, "Crabling"),
    (0, "Hatchling"),
)

# Persistent pet state. Same discipline as usage.CALIBRATION_FILE: the file
# is shared between processes (the pet, a second instance, tests), so it is
# loaded lazily and mtime-aware, and every writer re-reads it first so a
# stale in-memory value never clobbers a fresher file.
STATE_FILE = Path.home() / ".clawd" / "pet_state.json"
_state_loaded = False
_state_mtime = None
_xp: float = 0.0


def _load_state() -> None:
    """Load (or re-load) the pet-state file.

    mtime-aware: several processes may share this file, and a process that
    cached its state once would otherwise keep a stale XP total forever.
    Any external write is picked up on the next access."""
    global _state_loaded, _state_mtime, _xp
    try:
        mtime = STATE_FILE.stat().st_mtime_ns
    except OSError:
        mtime = None
    if _state_loaded and mtime == _state_mtime:
        return
    _state_loaded = True
    _state_mtime = mtime
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    if isinstance(data.get("xp"), (int, float)) and data["xp"] >= 0:
        _xp = float(data["xp"])


def _reload_state() -> None:
    """Re-sync the module state from the file before writing.

    Without re-reading, a writer with stale in-memory values would clobber
    a fresher file and eat another process's earned XP."""
    global _state_loaded
    _state_loaded = False
    _load_state()


def _save_state() -> None:
    global _state_mtime
    data = {"xp": _xp}
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(STATE_FILE)
        _state_mtime = STATE_FILE.stat().st_mtime_ns
    except OSError:
        pass                     # unwritable home — in-memory XP still works


def xp_for_level(n: int) -> int:
    """Cumulative XP required to reach level n: 500 * n * (n + 1) / 2.

    Level 1 at 500 XP, level 2 at 1500, level 3 at 3000 — gently quadratic."""
    if n <= 0:
        return 0
    return 250 * n * (n + 1)     # == 500 * n * (n + 1) / 2, always integral


def level_for_xp(xp: float) -> int:
    level = 0
    while xp >= xp_for_level(level + 1):
        level += 1
    return level


def title_for_level(n: int) -> str:
    for threshold, title in TITLE_BANDS:
        if n >= threshold:
            return title
    return TITLE_BANDS[-1][1]


def add_usage(weighted_delta: float) -> Optional[dict]:
    """Feed weighted token units to the pet; XP grows by delta / 1000.

    Returns a level-up event {"level": n, "title": str} when one or more
    levels were crossed by this delta, else None. Non-positive deltas are
    ignored (a shrinking counter means a window reset, not negative food)."""
    global _xp
    if not isinstance(weighted_delta, (int, float)) or weighted_delta <= 0:
        return None
    _reload_state()              # never write on top of a stale in-memory state
    before = level_for_xp(_xp)
    _xp += float(weighted_delta) * XP_PER_UNIT
    after = level_for_xp(_xp)
    _save_state()
    if after > before:
        return {"level": after, "title": title_for_level(after)}
    return None


def current() -> dict:
    """The pet's current progress: xp, level, title, next_level_xp."""
    _load_state()
    level = level_for_xp(_xp)
    return {
        "xp": _xp,
        "level": level,
        "title": title_for_level(level),
        "next_level_xp": xp_for_level(level + 1),
    }
