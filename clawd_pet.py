#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clawd — Claude Code Desktop Pet & Usage Widget
==============================================

A frameless, always-on-top desktop pet that renders "Clawd", the pixel-art
cloud mascot of Claude Code, and live-tracks your Claude Code token usage by
scanning the local session logs (~/.claude/projects/**/*.jsonl) over the
rolling 5-hour quota window.

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

import bisect
import functools
import json
import math
import os
import random
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QImageReader,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt5.QtNetwork import QHostAddress, QUdpSocket
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

# ======================================================================
#  CONFIG — edit these to match your plan
# ======================================================================

# Rolling-window quota. Anthropic does not publish the real token budget of any
# plan, so this is only a starting guess. Use the tray menu → "Limit
# kalibrieren …" once and the app derives your real budget from the percentage
# Claude itself displays; the result is stored in QSettings and wins over this.
MAX_TOKENS = 88_000                # placeholder default (Max 5x plan)
PLAN_NAME = "Max 5x"               # shown in the panel header
WINDOW_HOURS = 5                   # length of Anthropic's fixed session window
REPLAY_HOURS = 48                  # look-back to reconstruct the window chain
SCAN_INTERVAL_MS = 20_000          # how often the logs are rescanned
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Cache reads are ~100x larger than input/output on agent workloads and barely
# count against the real quota — excluded by default (else the meter pegs at
# thousands of percent).
COUNT_CACHE_READ = False           # include cache_read_input_tokens in the total
COUNT_CACHE_CREATION = False       # include cache_creation_input_tokens in the total

PET_HEIGHT = 132                   # on-screen pixel height of Clawd
PANEL_WIDTH = 392                  # width of the slide-out panel

# Animated GIF sprites (community pixel-art recreation of the official mascot,
# MIT-licensed: https://github.com/KebeliSamet0/clawd). If a file is missing,
# the built-in vector Clawd is drawn instead.
SPRITE_DIR = Path(__file__).resolve().parent / "sprites"
SPRITE_FILES = {
    "sleep": "clawd-sleeping.gif",   # no activity in the rolling window
    "chill": "clawd-idle.gif",
    "focus": "clawd-building.gif",
    "happy": "clawd-happy.gif",      # turn finished / waiting for your input
    "panic": "clawd-debugger.gif",
    "limit": "clawd-error.gif",
}

# --- Real-time activity (Stufe 1: log watcher, Stufe 2: opt-in hooks) -------
ACTIVITY_POLL_MS = 1500     # how often the newest session log is checked
ACTIVITY_IDLE_S = 240       # log untouched this long -> no activity
HOOK_UDP_PORT = 52741       # clawd_hook.py sends Claude Code events here
CLAUDE_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"
HOOK_EVENTS = ["PreToolUse", "Notification", "Stop", "SessionStart"]

# --- Optional live sync with the Anthropic usage endpoint -------------------
# Read-only, best effort: if ~/.claude/.credentials.json holds a *currently
# valid* OAuth token, the exact utilization percentages Claude itself shows are
# fetched. The app never writes to the credential store and never refreshes a
# token (the refresh endpoint is bot-protected and returns 403). On Windows the
# desktop app keeps its live token in the Credential Manager, so the file is
# often stale — then the local log estimate below is used instead.
USE_API_USAGE = True
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

ORG_NAME = "ClawdPet"
APP_NAME = "Clawd"


# ======================================================================
#  Usage scanning (pure logic, no Qt) — runs on a worker thread
# ======================================================================

@dataclass
class UsageSnapshot:
    """Aggregated token usage over the rolling window."""
    total: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    pct: float = 0.0
    oldest: Optional[datetime] = None       # oldest counted entry (UTC)
    updated_at: Optional[datetime] = None   # when this scan finished (local)
    files_scanned: int = 0
    entries: int = 0
    error: str = ""
    source: str = "logs"                          # "api" (live) or "logs" (estimate)
    buckets: list = field(default_factory=list)   # list[UsageBucket] in api mode
    by_model: dict = field(default_factory=dict)  # model id -> input+output tokens
    newest_file: str = ""                         # most recently written session log


_MAX_TOKENS_OVERRIDE: Optional[int] = None      # set once calibrated


def effective_max_tokens() -> int:
    """The calibrated budget if the user measured one, else the placeholder."""
    return _MAX_TOKENS_OVERRIDE or MAX_TOKENS


def set_max_tokens_override(value: Optional[int]) -> None:
    global _MAX_TOKENS_OVERRIDE
    _MAX_TOKENS_OVERRIDE = int(value) if value else None


def is_calibrated() -> bool:
    return _MAX_TOKENS_OVERRIDE is not None


def _parse_iso_ts(raw) -> Optional[datetime]:
    """Parse Claude Code log timestamps like '2026-07-09T23:23:38.864Z'."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _usage_entry(usage: dict, ts: datetime):
    def num(*keys) -> int:
        for k in keys:
            v = usage.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    return [
        num("input_tokens"),
        num("output_tokens"),
        num("cache_read_input_tokens", "cache_read_tokens"),
        num("cache_creation_input_tokens"),
        ts,
        "",          # model id, filled in by the scanner
    ]


def _entry_weight(entry) -> int:
    return entry[0] + entry[1] + entry[2] + entry[3]


def pretty_model(model_id: str) -> str:
    """'claude-fable-5' -> 'Fable', 'claude-opus-4-8' -> 'Opus', …"""
    m = (model_id or "").lower()
    for name in ("fable", "opus", "sonnet", "haiku"):
        if name in m:
            return name.capitalize()
    if m in ("", "<synthetic>"):
        return "System"
    return model_id


def _current_window_start(timestamps, now: datetime) -> Optional[datetime]:
    """Replay Anthropic's chained fixed 5-hour windows over activity times.

    A window opens with the first message while no window is active and lasts
    exactly WINDOW_HOURS; under continuous use the next window opens with the
    first message after the previous one expired. Any silence of >= one window
    length guarantees a fresh start, so the replay is anchored there.
    """
    if not timestamps:
        return None
    window = timedelta(hours=WINDOW_HOURS)
    anchor = timestamps[0]
    prev = timestamps[0]
    for ts in timestamps[1:]:
        if ts - prev >= window:
            anchor = ts
        prev = ts
    start = anchor
    while now >= start + window:
        idx = bisect.bisect_left(timestamps, start + window)
        if idx >= len(timestamps):
            return None            # window expired and nothing started a new one
        start = timestamps[idx]
    return start


def scan_usage(now: Optional[datetime] = None, should_stop=None) -> UsageSnapshot:
    """Scan ~/.claude/projects/**/*.jsonl and sum the current session window.

    Anthropic's 5-hour limit is a FIXED window (starts with the first message,
    resets completely after 5 h — chained under continuous use), not a rolling
    one. Entries of the last REPLAY_HOURS are collected to reconstruct the
    window chain; only tokens since the current window start are counted.
    Streaming writes the same assistant message on several lines with an
    identical message id, so entries are deduplicated per id (keeping the
    line with the largest token count).
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=REPLAY_HOURS)
    snap = UsageSnapshot(updated_at=datetime.now())

    if not CLAUDE_PROJECTS_DIR.is_dir():
        snap.error = f"Log-Verzeichnis nicht gefunden: {CLAUDE_PROJECTS_DIR}"
        return snap

    by_msg_id = {}
    anonymous = []
    newest_mtime = datetime.min.replace(tzinfo=timezone.utc)

    try:
        files = list(CLAUDE_PROJECTS_DIR.rglob("*.jsonl"))
    except OSError as exc:
        snap.error = f"Logs nicht lesbar: {exc}"
        return snap

    for fp in files:
        if should_stop is not None and should_stop():
            break   # app is quitting — a partial snapshot is fine
        try:
            mtime = datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            continue  # file untouched since before the window — nothing new inside
        if mtime > newest_mtime:
            newest_mtime = mtime
            snap.newest_file = str(fp)
        snap.files_scanned += 1
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(rec, dict):
                        continue
                    ts = _parse_iso_ts(rec.get("timestamp"))
                    if ts is None or ts < cutoff or ts > now + timedelta(minutes=5):
                        continue
                    msg = rec.get("message")
                    msg = msg if isinstance(msg, dict) else {}
                    usage = msg.get("usage") or rec.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    entry = _usage_entry(usage, ts)
                    if _entry_weight(entry) <= 0:
                        continue
                    model = msg.get("model")
                    if isinstance(model, str):
                        entry[5] = model
                    mid = msg.get("id")
                    if isinstance(mid, str) and mid:
                        prev = by_msg_id.get(mid)
                        if prev is None or _entry_weight(entry) > _entry_weight(prev):
                            by_msg_id[mid] = entry
                    else:
                        anonymous.append(entry)
        except OSError:
            continue

    all_entries = list(by_msg_id.values()) + anonymous
    window_start = _current_window_start(sorted(e[4] for e in all_entries), now)
    snap.oldest = window_start          # countdown target: window_start + 5 h

    for inp, out, cr, cc, ts, model in all_entries:
        if window_start is None or ts < window_start:
            continue                    # previous, already reset window
        snap.entries += 1
        snap.input_tokens += inp
        snap.output_tokens += out
        snap.cache_read += cr
        snap.cache_creation += cc
        name = pretty_model(model)
        snap.by_model[name] = snap.by_model.get(name, 0) + inp + out

    snap.total = snap.input_tokens + snap.output_tokens
    if COUNT_CACHE_READ:
        snap.total += snap.cache_read
    if COUNT_CACHE_CREATION:
        snap.total += snap.cache_creation
    budget = effective_max_tokens()
    snap.pct = (snap.total / budget * 100.0) if budget > 0 else 0.0
    return snap


class ScanThread(QThread):
    """Runs scan_usage() off the GUI thread."""
    result = pyqtSignal(object)

    def run(self):
        try:
            snap = collect_usage(should_stop=self.isInterruptionRequested)
        except Exception as exc:  # never let the worker die silently
            snap = UsageSnapshot(error=f"Scan-Fehler: {exc}", updated_at=datetime.now())
        self.result.emit(snap)


# ======================================================================
#  Live usage via the Anthropic OAuth API — the same numbers the Claude
#  UI shows (5-hour window + weekly limits), incl. token auto-refresh.
# ======================================================================

@dataclass
class UsageBucket:
    key: str
    label: str
    pct: float
    resets_at: Optional[datetime]


BUCKET_LABELS = {
    "five_hour": "5-Stunden-Limit",
    "seven_day": "Wöchentlich · alle Modelle",
    "seven_day_opus": "Wöchentlich · Opus",
    "seven_day_sonnet": "Wöchentlich · Sonnet",
    "seven_day_fable": "Wöchentlich · Fable",
    "seven_day_oauth_apps": "Wöchentlich · Apps",
}


def _get_access_token() -> Optional[str]:
    """The stored OAuth token, but only while it is still valid. Read-only."""
    if os.environ.get("CLAWD_NO_API"):
        return None
    try:
        creds = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    oauth = creds.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    expires_ms = oauth.get("expiresAt") or 0
    if token and time.time() * 1000 < expires_ms - 60_000:
        return token
    return None                   # expired — fall back to the log estimate


def fetch_api_usage() -> Optional[list]:
    """Real utilization buckets straight from Anthropic, or None on failure."""
    token = _get_access_token()
    if not token:
        return None
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "ClawdPet/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    buckets = []

    # Preferred source: the "limits" array — it is what Claude's own usage
    # popup renders, incl. model-scoped weekly limits ("Wöchentlich · Fable").
    limits = data.get("limits")
    if isinstance(limits, list):
        for lim in limits:
            if not isinstance(lim, dict):
                continue
            pct = lim.get("percent")
            if not isinstance(pct, (int, float)):
                continue
            kind = str(lim.get("kind") or "")
            scope = lim.get("scope") if isinstance(lim.get("scope"), dict) else {}
            model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
            display = model.get("display_name")
            if kind == "session":
                key, label = "five_hour", "5-Stunden-Limit"
            elif kind == "weekly_all":
                key, label = "seven_day", "Wöchentlich · alle Modelle"
            elif isinstance(display, str) and display:
                key, label = f"weekly_{display.lower()}", f"Wöchentlich · {display}"
            else:
                key = kind or "unknown"
                label = (kind or "Limit").replace("_", " ").title()
            buckets.append(UsageBucket(
                key=key, label=label, pct=float(pct),
                resets_at=_parse_iso_ts(lim.get("resets_at"))))

    # Fallback: older top-level bucket format {name: {utilization, resets_at}}
    if not buckets:
        for key, val in data.items():
            if not isinstance(val, dict):
                continue
            util = val.get("utilization")
            if not isinstance(util, (int, float)):
                continue
            pct = float(util)
            if isinstance(util, float) and 0.0 <= util <= 1.0:
                pct *= 100.0      # some deployments report a 0..1 fraction
            resets = val.get("resets_at")
            if isinstance(resets, (int, float)):
                resets_at = datetime.fromtimestamp(resets, tz=timezone.utc)
            else:
                resets_at = _parse_iso_ts(resets)
            label = BUCKET_LABELS.get(
                key, key.replace("seven_day_", "Wöchentlich · ").replace("_", " ").title())
            buckets.append(UsageBucket(key=key, label=label, pct=pct, resets_at=resets_at))

    order = {"five_hour": 0, "seven_day": 1}
    buckets.sort(key=lambda b: order.get(b.key, 2))
    return buckets or None


def collect_usage(should_stop=None) -> UsageSnapshot:
    """API first (exact numbers), local log estimate as fallback."""
    if USE_API_USAGE:
        buckets = fetch_api_usage()
        if buckets:
            # keep the local per-model token counts as extra detail
            snap = scan_usage(should_stop=should_stop)
            if snap.error:
                snap = UsageSnapshot(updated_at=datetime.now())
            snap.error = ""
            snap.source = "api"
            snap.buckets = buckets
            five = next((b for b in buckets if b.key == "five_hour"), buckets[0])
            snap.pct = five.pct
            return snap
    return scan_usage(should_stop=should_stop)


def _fmt_reset(resets_at: Optional[datetime]) -> str:
    if resets_at is None:
        return ""
    secs = int((resets_at - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "Zurücksetzung läuft …"
    if secs < 24 * 3600:
        h, m = divmod(secs // 60, 60)
        if h:
            return f"Zurücksetzung in {h} Std. {m:02d} Min."
        return f"Zurücksetzung in {m} Min."
    local = resets_at.astimezone()
    wd = ("Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So.")[local.weekday()]
    return f"Zurücksetzung {wd}, {local.strftime('%H:%M')}"


# ======================================================================
#  Real-time activity — Stufe 1: watch the newest session log's tail
# ======================================================================

TOOL_BUBBLES = {
    "Read": "liest Dateien …",
    "Edit": "schreibt Code …",
    "Write": "schreibt Code …",
    "MultiEdit": "schreibt Code …",
    "NotebookEdit": "schreibt Code …",
    "Bash": "führt Befehle aus …",
    "PowerShell": "führt Befehle aus …",
    "Grep": "durchsucht den Code …",
    "Glob": "durchsucht den Code …",
    "Task": "delegiert an Agenten …",
    "Agent": "delegiert an Agenten …",
    "WebFetch": "surft im Web …",
    "WebSearch": "surft im Web …",
}


def read_last_activity(path: Path, now: Optional[datetime] = None):
    """Inspect the tail of a session log.

    Returns ("working", tool_name_or_None), ("waiting", None) — Claude has
    finished its turn — or None when the log has gone quiet.
    """
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    now_ts = (now or datetime.now(timezone.utc)).timestamp()
    if now_ts - mtime > ACTIVITY_IDLE_S:
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 32768))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue                      # first line may be cut by the seek
        if not isinstance(rec, dict):
            continue
        rtype = rec.get("type")
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else None
        if rtype == "assistant" and msg:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        return ("working", block.get("name"))
            return ("waiting", None)      # spoke without tools -> turn is over
        if rtype == "user":
            return ("working", None)      # tool result arrived, Claude thinks
    return None


# ======================================================================
#  Real-time activity — Stufe 2: opt-in Claude Code hooks
#  (clawd_hook.py sends events via UDP; registration lives in
#   ~/.claude/settings.json and is only touched from the tray menu.)
# ======================================================================

def hook_command() -> Optional[str]:
    """Command line for the Claude Code hook, or None if unavailable."""
    if getattr(sys, "frozen", False):
        src = Path(getattr(sys, "_MEIPASS", "")) / "clawd_hook.py"
        dst = Path.home() / ".claude" / "clawd_hook.py"
        try:
            shutil.copy2(src, dst)
        except OSError:
            return None
        hook_py = dst
    else:
        hook_py = Path(__file__).resolve().parent / "clawd_hook.py"
        if not hook_py.is_file():
            return None
    runner = (shutil.which("pythonw") or shutil.which("pyw")
              or shutil.which("python") or shutil.which("py"))
    if not runner:
        return None
    return f'"{runner}" "{hook_py}"'


def _load_settings(settings_path: Path):
    try:
        if settings_path.exists():
            return json.loads(settings_path.read_text(encoding="utf-8"))
        return {}
    except (OSError, ValueError):
        return None


def _write_settings(settings_path: Path, data: dict) -> bool:
    try:
        if settings_path.exists():
            shutil.copy2(settings_path,
                         settings_path.with_suffix(".json.clawd-bak"))
        tmp = settings_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(settings_path)
        return True
    except OSError:
        return False


def hooks_registered(settings_path: Path) -> bool:
    data = _load_settings(settings_path)
    if not isinstance(data, dict):
        return False
    return "clawd_hook.py" in json.dumps(data.get("hooks", {}))


def register_hooks(settings_path: Path, command: str) -> bool:
    data = _load_settings(settings_path)
    if not isinstance(data, dict):
        return False
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in HOOK_EVENTS:
        arr = hooks.setdefault(event, [])
        if not isinstance(arr, list):
            continue
        if any("clawd_hook.py" in json.dumps(entry) for entry in arr):
            continue
        arr.append({"matcher": "",
                    "hooks": [{"type": "command", "command": command}]})
        changed = True
    return changed and _write_settings(settings_path, data)


def unregister_hooks(settings_path: Path) -> bool:
    data = _load_settings(settings_path)
    if not isinstance(data, dict):
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event, arr in hooks.items():
        if not isinstance(arr, list):
            continue
        kept = [e for e in arr if "clawd_hook.py" not in json.dumps(e)]
        if len(kept) != len(arr):
            hooks[event] = kept
            changed = True
    return changed and _write_settings(settings_path, data)


def mood_for_pct(pct: float) -> str:
    if pct >= 100.0:
        return "limit"
    if pct >= 80.0:
        return "panic"
    if pct >= 50.0:
        return "focus"
    return "chill"


MOOD_COLORS = {
    "sleep": "#3fb950",
    "chill": "#3fb950",
    "focus": "#d29922",
    "panic": "#f0883e",
    "limit": "#f85149",
}


def fmt_de(n: int) -> str:
    """1234567 -> '1.234.567' (German thousands separator)."""
    return f"{n:,}".replace(",", ".")


def fmt_pct_de(pct: float) -> str:
    return f"{pct:.1f}".replace(".", ",") + " %"


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
    """Loads the per-mood animations and scales them to one common size."""

    def __init__(self):
        self.sprites = {}
        if not SPRITE_DIR.is_dir():
            return
        for mood, fname in SPRITE_FILES.items():
            fp = SPRITE_DIR / fname
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
        scale = PET_HEIGHT / tallest
        for sprite in self.sprites.values():
            sprite.build(scale)
        self.width = max(s.pixmaps[0].width() for s in self.sprites.values())
        self.height = PET_HEIGHT

    def sprite(self, mood: str) -> Optional[Sprite]:
        return self.sprites.get(mood)


@functools.lru_cache(maxsize=16)
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
#  Pet widget — the always-on-top mascot
# ======================================================================

_HEART_ROWS = ("0110110", "1111111", "1111111", "0111110", "0011100", "0001000")


class PetWidget(QWidget):
    ANIM_TICK_MS = 33          # ~30 fps; sprite timing comes from the GIF delays
    DRAG_THRESHOLD = 6
    MOOD_FADE_MS = 340         # cross-dissolve a mood change
    HEART_LIFE_MS = 1200       # petting hearts float up and fade this long

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

        self._apply_mood()
        self.setToolTip("Clawd wartet auf die ersten Daten …")

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
            self.setToolTip(f"Claude-Nutzung (5-h-Fenster): {fmt_pct_de(snap.pct)}")
        else:
            self.setToolTip(
                f"Claude-Nutzung (Schätzung): {fmt_pct_de(snap.pct)}  "
                f"({fmt_de(snap.total)} Tokens verbraucht)"
            )

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
        """Combine quota mood with live activity: quota alarms always win."""
        mood = self._quota_mood
        if mood not in ("panic", "limit") and self._activity:
            kind = self._activity[0]
            mood = {"working": "focus", "waiting": "happy",
                    "needs_input": "happy", "error": "panic"}.get(kind, mood)
        self._set_mood(mood)

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
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self.owner:
            self.owner.schedule_panel_hide()
        super().leaveEvent(event)


# ======================================================================
#  Slide-out usage panel
# ======================================================================

PANEL_QSS = """
QFrame#card {
    background-color: rgba(38, 37, 35, 250);
    border: 1px solid #3d3b38;
    border-radius: 12px;
}
QLabel {
    color: #eceae6;
    background: transparent;
    border: none;
    font-family: 'Segoe UI', 'Helvetica Neue', sans-serif;
}
QLabel#h1       { font-size: 13px; font-weight: 600; }
QLabel#rowlabel { font-size: 12px; font-weight: 600; }
QLabel#reset    { font-size: 11px; color: #9b9892; }
QLabel#pct      { font-size: 12px; font-weight: 700; }
QLabel#sub      { font-size: 11px; color: #9b9892; }
QLabel#note     { font-size: 10px; color: #7d7a74; font-style: italic; }
QProgressBar {
    background: #3a3833;
    border: none;
    border-radius: 2px;
}
QProgressBar::chunk { border-radius: 2px; background: #6879f8; }
QFrame#divider { background: #3d3b38; border: none; }
"""


class PanelWidget(QWidget):
    SLIDE_PX = 16

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if sys.platform == "darwin":   # Qt.Tool windows vanish on app deactivation
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setFixedWidth(PANEL_WIDTH)
        self.setStyleSheet(PANEL_QSS)

        self.pinned = False
        self.on_leave = None            # callback set by ClawdApp
        self._snap: Optional[UsageSnapshot] = None
        self._anim: Optional[QParallelAnimationGroup] = None
        self._hiding = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        card = QFrame(self)
        card.setObjectName("card")
        outer.addWidget(card)

        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 160))
        card.setGraphicsEffect(shadow)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(8)
        self._lay = lay

        # ---- header (Claude style) -------------------------------------
        header = QHBoxLayout()
        header.setSpacing(8)
        avatar = QLabel()
        avatar.setPixmap(sprite_pixmap("chill", 24) or make_clawd_pixmap(24))
        header.addWidget(avatar)
        title = QLabel(f"Plan-Nutzungslimits · {PLAN_NAME}")
        title.setObjectName("h1")
        header.addWidget(title, 1)
        lay.addLayout(header)
        lay.addWidget(self._divider())

        # ---- usage rows, created on demand from the live buckets --------
        self._rows = {}

        # ---- footer ------------------------------------------------------
        self._footer_div = self._divider()
        lay.addWidget(self._footer_div)
        self.detail_label = QLabel("")
        self.detail_label.setObjectName("sub")
        self.detail_label.setWordWrap(True)
        lay.addWidget(self.detail_label)
        self.updated_label = QLabel("Zuletzt aktualisiert: –")
        self.updated_label.setObjectName("note")
        lay.addWidget(self.updated_label)

        # countdown refresher (only while visible)
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._refresh_countdown)

        self.adjustSize()

    # -------------------------------------------------- small builders

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setObjectName("divider")
        line.setFixedHeight(1)
        return line

    def _ensure_row(self, key: str, label: str) -> dict:
        """Create (or fetch) a Claude-style usage row: label | reset | pct + bar."""
        row = self._rows.get(key)
        if row is not None:
            row["name"].setText(label)
            return row
        idx = self._lay.indexOf(self._footer_div)
        holder = QVBoxLayout()
        holder.setSpacing(3)

        # top line: name .......... percentage
        top = QHBoxLayout()
        top.setSpacing(8)
        name = QLabel(label)
        name.setObjectName("rowlabel")
        pct = QLabel("–")
        pct.setObjectName("pct")
        top.addWidget(name, 1)
        top.addWidget(pct, 0, Qt.AlignRight)

        # second line: the reset hint gets its own full-width row, so long
        # German strings ("Zurücksetzung in 3 Std. 53 Min.") are never clipped
        reset = QLabel("")
        reset.setObjectName("reset")
        reset.setWordWrap(True)

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(False)
        bar.setFixedHeight(6)

        holder.addLayout(top)
        holder.addWidget(reset)
        holder.addWidget(bar)
        holder.addSpacing(6)
        self._lay.insertLayout(idx, holder)
        anim = QPropertyAnimation(bar, b"value", self)
        anim.setDuration(600)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        row = {"name": name, "reset": reset, "pct": pct, "bar": bar, "anim": anim}
        self._rows[key] = row
        return row

    def _show_only(self, keys):
        """Hide rows that the current snapshot does not provide, so switching
        between API and log mode never leaves a stale duplicate row behind."""
        for key, row in self._rows.items():
            visible = key in keys
            for part in ("name", "reset", "pct", "bar"):
                row[part].setVisible(visible)

    @staticmethod
    def _animate_row(row: dict, pct: float):
        row["pct"].setText(f"{pct:.0f} %")
        color = MOOD_COLORS[mood_for_pct(pct)] if pct >= 80 else "#6879f8"
        row["pct"].setStyleSheet(f"color: {color};")
        row["bar"].setStyleSheet(
            "QProgressBar { background: #3a3833; border: none; border-radius: 2px; }"
            f"QProgressBar::chunk {{ border-radius: 2px; background: {color}; }}")
        target = max(0, min(100, int(round(pct))))
        anim = row["anim"]
        anim.stop()
        anim.setStartValue(row["bar"].value())
        anim.setEndValue(target)
        anim.start()

    # -------------------------------------------------- data updates

    def update_snapshot(self, snap: UsageSnapshot):
        self._snap = snap

        if snap.error:
            self.detail_label.setText(snap.error)
        elif snap.source == "api":
            for b in snap.buckets:
                self._animate_row(self._ensure_row(b.key, b.label), b.pct)
            self._show_only({b.key for b in snap.buckets})
            models = sorted(
                ((n, t) for n, t in snap.by_model.items() if n != "System" and t > 0),
                key=lambda kv: -kv[1])
            if models:
                parts = " · ".join(f"{n} {fmt_de(t)}" for n, t in models)
                self.detail_label.setText(f"Lokal gezählt (5-h-Fenster): {parts} Tokens")
            else:
                self.detail_label.setText("")
        else:
            row = self._ensure_row("estimate", "5-Stunden-Limit")
            self._animate_row(row, snap.pct)
            row["pct"].setText(fmt_pct_de(snap.pct))
            keys = {"estimate"}

            # per-model breakdown of the same 5-hour window (Fable, Opus, …)
            budget = effective_max_tokens()
            models = sorted(
                ((n, t) for n, t in snap.by_model.items() if n != "System" and t > 0),
                key=lambda kv: -kv[1])
            for name, tok in models:
                mkey = f"model:{name}"
                mrow = self._ensure_row(mkey, name)
                share = (tok / budget * 100.0) if budget > 0 else 0.0
                self._animate_row(mrow, share)
                mrow["reset"].setText(f"{fmt_de(tok)} Tokens (In + Out)")
                keys.add(mkey)
            self._show_only(keys)

            hint = ("Limit kalibriert" if is_calibrated() else
                    "Platzhalter-Limit – im Tray-Menü kalibrieren")
            self.detail_label.setText(
                f"{fmt_de(snap.total)} Tokens verbraucht (Input + Output) · {hint}")
        self.detail_label.setVisible(bool(self.detail_label.text()))
        if snap.updated_at:
            src = "live" if snap.source == "api" else "lokal"
            self.updated_label.setText(
                f"Zuletzt aktualisiert: {snap.updated_at.strftime('%H:%M:%S')} ({src})")
        self._refresh_countdown()
        # rows are inserted lazily — force a full re-layout before resizing,
        # otherwise sizeHint() is stale and the card gets squashed
        self._lay.invalidate()
        self._lay.activate()
        self.layout().invalidate()
        self.layout().activate()
        self.adjustSize()

    def _refresh_countdown(self):
        snap = self._snap
        if snap is None:
            return
        if snap.source == "api":
            for b in snap.buckets:
                row = self._rows.get(b.key)
                if row is not None:
                    row["reset"].setText(_fmt_reset(b.resets_at))
        else:
            row = self._rows.get("estimate")
            if row is None:
                return
            if snap.oldest is None:
                row["reset"].setText("")
            else:
                row["reset"].setText(
                    _fmt_reset(snap.oldest + timedelta(hours=WINDOW_HOURS)))

    # -------------------------------------------------- show / hide

    def target_geometry(self, pet: QWidget):
        """Position next to the pet: right side preferred, left as fallback."""
        self.adjustSize()
        screen = QGuiApplication.screenAt(pet.frameGeometry().center()) \
            or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        pet_geo = pet.frameGeometry()

        x = pet_geo.right() + 6
        side = 1
        if x + self.width() > avail.right():
            x = pet_geo.left() - 6 - self.width()
            side = -1
        y = pet_geo.center().y() - self.height() // 2
        y = max(avail.top() + 8, min(y, avail.bottom() - self.height() - 8))
        return QPoint(x, y), side

    def show_for(self, pet: QWidget, pinned: bool):
        self.pinned = pinned or self.pinned
        target, side = self.target_geometry(pet)
        if self._anim is not None:
            self._anim.stop()   # a running fade-out would leave opacity at 0
            self._anim.deleteLater()
            self._anim = None
        self._hiding = False
        if self.isVisible():
            self.setWindowOpacity(1.0)
            self.move(target)
            return
        start = QPoint(target.x() - side * self.SLIDE_PX, target.y())
        self.move(start)
        self.setWindowOpacity(0.0)
        self.show()

        pos_anim = QPropertyAnimation(self, b"pos", self)
        pos_anim.setDuration(240)
        pos_anim.setStartValue(start)
        pos_anim.setEndValue(target)
        pos_anim.setEasingCurve(QEasingCurve.OutCubic)

        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(240)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)

        self._anim = QParallelAnimationGroup(self)
        self._anim.addAnimation(pos_anim)
        self._anim.addAnimation(fade)
        self._anim.start()

    def hide_animated(self):
        if not self.isVisible() or self._hiding:
            return
        if self._anim is not None:
            self._anim.stop()   # don't fight a still-running slide-in
            self._anim.deleteLater()
            self._anim = None
        self._hiding = True
        self.pinned = False
        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(180)
        fade.setStartValue(self.windowOpacity())
        fade.setEndValue(0.0)
        fade.finished.connect(self._finish_hide)
        self._anim = QParallelAnimationGroup(self)
        self._anim.addAnimation(fade)
        self._anim.start()

    def _finish_hide(self):
        if self._hiding:
            self.hide()
            self.setWindowOpacity(1.0)
            self._hiding = False

    def reposition(self, pet: QWidget):
        if self.isVisible():
            target, _ = self.target_geometry(pet)
            self.move(target)

    # -------------------------------------------------- events

    def showEvent(self, event):
        self._countdown_timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._countdown_timer.stop()
        super().hideEvent(event)

    def leaveEvent(self, event):
        if self.on_leave:
            self.on_leave()
        super().leaveEvent(event)


# ======================================================================
#  Speech bubble — small transient callout above the pet
# ======================================================================

class SpeechBubble(QWidget):
    TAIL_H = 7
    PAD_X, PAD_Y = 12, 7

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if sys.platform == "darwin":
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self._text = ""
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self.setFont(QFont("Segoe UI", 9))

    def show_text(self, text: str, pet: QWidget, duration_ms: int = 4200):
        self._text = text
        fm = self.fontMetrics()
        self.setFixedSize(max(46, fm.horizontalAdvance(text) + self.PAD_X * 2),
                          fm.height() + self.PAD_Y * 2 + self.TAIL_H)
        self.follow(pet)
        self.show()
        self.raise_()
        self.update()
        self._hide_timer.start(duration_ms)

    def follow(self, pet: QWidget):
        geo = pet.frameGeometry()
        screen = (QGuiApplication.screenAt(geo.center())
                  or QGuiApplication.primaryScreen())
        avail = screen.availableGeometry()
        x = geo.center().x() - self.width() // 2
        x = max(avail.left() + 4, min(x, avail.right() - self.width() - 4))
        y = max(avail.top() + 4, geo.top() - self.height() - 2)
        self.move(x, y)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        body = QRectF(0, 0, self.width(), self.height() - self.TAIL_H)
        p.setPen(QPen(QColor("#3d3b38"), 1))
        p.setBrush(QColor(38, 37, 35, 250))
        p.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), 9, 9)
        cx = self.width() / 2
        tail = QPainterPath()
        tail.moveTo(cx - 6, body.bottom() - 1)
        tail.lineTo(cx, self.height() - 1)
        tail.lineTo(cx + 6, body.bottom() - 1)
        tail.closeSubpath()
        p.fillPath(tail, QColor(38, 37, 35, 250))
        p.setPen(QColor("#eceae6"))
        p.drawText(body, Qt.AlignCenter, self._text)
        p.end()


# ======================================================================
#  Application controller — wires pet, panel, tray and scanner together
# ======================================================================

class ClawdApp:
    def __init__(self, app: QApplication, with_tray: bool = True):
        self.app = app
        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.snapshot = UsageSnapshot()

        saved = self.settings.value("max_tokens")
        try:
            if saved and int(saved) > 0:
                set_max_tokens_override(int(saved))
        except (TypeError, ValueError):
            self.settings.remove("max_tokens")

        self.pet = PetWidget(self)
        self.panel = PanelWidget()
        self.panel.on_leave = self.schedule_panel_hide
        self.bubble = SpeechBubble()

        # real-time activity: log watcher (Stufe 1) + hook receiver (Stufe 2)
        self.quiet = self.settings.value("quiet", False, type=bool)
        self._newest_log: Optional[Path] = None
        self._last_activity = None
        self._hook_hold_until = 0.0
        self._activity_timer = QTimer()
        self._activity_timer.setInterval(ACTIVITY_POLL_MS)
        self._activity_timer.timeout.connect(self._check_activity)
        self._udp = QUdpSocket()
        if self._udp.bind(QHostAddress.LocalHost, HOOK_UDP_PORT):
            self._udp.readyRead.connect(self._read_hook_datagrams)

        self._scan_thread: Optional[ScanThread] = None
        self._scan_timer = QTimer()
        self._scan_timer.setInterval(SCAN_INTERVAL_MS)
        self._scan_timer.timeout.connect(self.refresh)

        self._hide_check = QTimer()
        self._hide_check.setSingleShot(True)
        self._hide_check.setInterval(400)
        self._hide_check.timeout.connect(self._maybe_hide_panel)

        self.tray: Optional[QSystemTrayIcon] = None
        self._tray_menu: Optional[QMenu] = None
        if with_tray and QSystemTrayIcon.isSystemTrayAvailable():
            self._setup_tray()

        self._restore_position()

    # -------------------------------------------------- lifecycle

    def start(self):
        self.pet.show()
        self._scan_timer.start()
        self._activity_timer.start()
        self.refresh()

    def quit(self):
        self.save_position()
        self._scan_timer.stop()
        self._activity_timer.stop()
        self._udp.close()
        thread = self._scan_thread
        if thread is not None and thread.isRunning():
            thread.requestInterruption()
            thread.wait(5000)   # destroying a running QThread aborts the process
        if self.tray:
            self.tray.hide()
        self.app.quit()

    # -------------------------------------------------- tray

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(make_app_icon(), self.app)
        self.tray.setToolTip("Clawd – Claude Code Nutzung")
        self._tray_menu = self.build_menu(None)
        self.tray.setContextMenu(self._tray_menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.toggle_pet_visible()

    def _rebuild_tray_menu(self):
        """The tray menu is cached, so it must be rebuilt when its items change."""
        if self.tray is None:
            return
        old = self._tray_menu
        self._tray_menu = self.build_menu(None)
        self.tray.setContextMenu(self._tray_menu)
        if old is not None:
            old.deleteLater()

    def build_menu(self, parent) -> QMenu:
        menu = QMenu(parent)
        act_refresh = QAction("Jetzt aktualisieren", menu)
        act_refresh.triggered.connect(self.refresh)
        menu.addAction(act_refresh)

        act_panel = QAction("Panel öffnen/schließen", menu)
        act_panel.triggered.connect(self.toggle_panel)
        menu.addAction(act_panel)

        menu.addSeparator()
        act_quiet = QAction("Sprechblasen einblenden" if self.quiet
                            else "Sprechblasen ausblenden", menu)
        act_quiet.triggered.connect(self.toggle_quiet)
        menu.addAction(act_quiet)

        if hooks_registered(CLAUDE_SETTINGS_FILE):
            act_hooks = QAction("Echtzeit-Hooks deaktivieren", menu)
            act_hooks.triggered.connect(self.disable_hooks)
        else:
            act_hooks = QAction("Echtzeit-Hooks aktivieren (Beta)", menu)
            act_hooks.triggered.connect(self.enable_hooks)
        menu.addAction(act_hooks)

        act_cal = QAction("Limit kalibrieren …", menu)
        act_cal.triggered.connect(self.calibrate)
        menu.addAction(act_cal)
        if is_calibrated():
            act_reset = QAction("Kalibrierung zurücksetzen", menu)
            act_reset.triggered.connect(self.reset_calibration)
            menu.addAction(act_reset)
        menu.addSeparator()

        if self.tray is not None:   # without a tray there is no way to un-hide
            act_show = QAction("Clawd anzeigen/verstecken", menu)
            act_show.triggered.connect(self.toggle_pet_visible)
            menu.addAction(act_show)

        menu.addSeparator()
        act_quit = QAction("Beenden", menu)
        act_quit.triggered.connect(self.quit)
        menu.addAction(act_quit)
        return menu

    def toggle_pet_visible(self):
        if self.pet.isVisible():
            if self.tray is None:
                return   # hiding without a tray would leave no way back
            self.pet.hide()
            self.panel.hide_animated()
        else:
            self.pet.show()

    # -------------------------------------------------- scanning

    def refresh(self):
        if self._scan_thread is not None and self._scan_thread.isRunning():
            return
        self._scan_thread = ScanThread()
        self._scan_thread.result.connect(self._on_scan_result)
        self._scan_thread.start()

    def _on_scan_result(self, snap: UsageSnapshot):
        self.snapshot = snap
        self._newest_log = Path(snap.newest_file) if snap.newest_file else None
        self.pet.set_snapshot(snap)
        self.panel.update_snapshot(snap)
        if self.tray:
            if snap.error:
                self.tray.setToolTip(f"Clawd – {snap.error}")
            else:
                self.tray.setToolTip(
                    f"Clawd – {fmt_pct_de(snap.pct)}  "
                    f"({fmt_de(snap.total)} Tokens verbraucht)")
            self.tray.setIcon(make_app_icon(mood_for_pct(snap.pct)))

    # -------------------------------------------------- panel control

    def toggle_panel(self):
        if self.panel.isVisible() and self.panel.pinned:
            self.panel.hide_animated()
        else:
            self.panel.show_for(self.pet, pinned=True)

    def hover_panel(self):
        self._hide_check.stop()
        # unconditional: show_for() also rescues a panel that is mid-fade-out
        self.panel.show_for(self.pet, pinned=False)

    def schedule_panel_hide(self):
        self._hide_check.start()

    def _maybe_hide_panel(self):
        if not self.panel.isVisible() or self.panel.pinned:
            return
        cursor = QCursor.pos()
        pet_zone = self.pet.frameGeometry().adjusted(-8, -8, 8, 8)
        panel_zone = self.panel.frameGeometry().adjusted(-8, -8, 8, 8)
        if pet_zone.contains(cursor) or panel_zone.contains(cursor):
            self._hide_check.start()   # still hovering, check again later
            return
        self.panel.hide_animated()

    def pet_moved(self):
        self.panel.reposition(self.pet)
        if self.bubble.isVisible():
            self.bubble.follow(self.pet)

    # -------------------------------------------------- real-time activity

    def _check_activity(self):
        if time.monotonic() < self._hook_hold_until:
            return                       # live hook events take precedence
        act = read_last_activity(self._newest_log) if self._newest_log else None
        prev = self._last_activity
        self._last_activity = act
        self.pet.set_activity(act)
        if act == prev or self.quiet or not self.pet.isVisible():
            return
        if act and act[0] == "working" and act[1]:
            text = TOOL_BUBBLES.get(act[1])
            if text and (not prev or prev[0] != "working" or prev[1] != act[1]):
                self.bubble.show_text(text, self.pet)
        elif act and act[0] == "waiting" and prev and prev[0] == "working":
            self.bubble.show_text("Fertig! Wartet auf dich.", self.pet)

    def _read_hook_datagrams(self):
        while self._udp.hasPendingDatagrams():
            data, _host, _port = self._udp.readDatagram(65535)
            try:
                event = json.loads(bytes(data).decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(event, dict):
                self._handle_hook_event(event)

    def _handle_hook_event(self, event: dict):
        name = event.get("hook_event_name") or ""
        act = None
        text = None
        if name == "PreToolUse":
            act = ("working", event.get("tool_name"))
            text = TOOL_BUBBLES.get(event.get("tool_name"))
        elif name == "Notification":
            act = ("needs_input", None)
            text = "Claude wartet auf deine Eingabe!"
        elif name in ("Stop", "TaskCompleted"):
            act = ("waiting", None)
        elif name == "PostToolUseFailure":
            act = ("error", None)
            QTimer.singleShot(5000, self._clear_error_state)
        elif name == "SessionStart":
            text = "Neue Claude-Session gestartet"
        else:
            return
        self._hook_hold_until = time.monotonic() + 15.0
        if act is not None:
            self._last_activity = act
            self.pet.set_activity(act)
        if text and not self.quiet and self.pet.isVisible():
            self.bubble.show_text(
                text, self.pet, 8000 if name == "Notification" else 4200)

    def _clear_error_state(self):
        if self.pet._activity and self.pet._activity[0] == "error":
            self._last_activity = None
            self.pet.set_activity(None)

    def toggle_quiet(self):
        self.quiet = not self.quiet
        self.settings.setValue("quiet", self.quiet)
        if self.quiet:
            self.bubble.hide()
        self._rebuild_tray_menu()

    def enable_hooks(self):
        command = hook_command()
        if not command:
            QMessageBox.warning(
                None, "Python benötigt",
                "Für Echtzeit-Hooks wird eine Python-Installation benötigt\n"
                "(pythonw/py im PATH). Der Log-Watcher läuft trotzdem weiter.")
            return
        if register_hooks(CLAUDE_SETTINGS_FILE, command):
            QMessageBox.information(
                None, "Hooks aktiviert",
                "Clawd reagiert ab der nächsten Claude-Code-Session sofort auf\n"
                "Ereignisse — inklusive „Claude wartet auf deine Eingabe“.\n\n"
                f"Backup der Einstellungen: {CLAUDE_SETTINGS_FILE.name}.clawd-bak")
        self._rebuild_tray_menu()

    def disable_hooks(self):
        unregister_hooks(CLAUDE_SETTINGS_FILE)
        self._rebuild_tray_menu()

    # -------------------------------------------------- calibration

    def calibrate(self):
        """Derive the real token budget from the percentage Claude displays.

        Anthropic publishes no token quota, so the only ground truth is the
        number in Claude's own /usage popup. Given that percentage and the
        tokens we counted in the same window, the budget is a simple ratio.
        """
        snap = self.snapshot
        if snap.source == "api":
            QMessageBox.information(
                None, "Kalibrierung nicht nötig",
                "Die App bezieht gerade die echten Prozentwerte direkt von "
                "Anthropic. Eine Kalibrierung ändert daran nichts.")
            return
        if snap.total <= 0:
            QMessageBox.warning(
                None, "Keine Daten",
                "In den letzten 5 Stunden wurden keine Tokens gezählt.\n"
                "Nutze Claude Code kurz und versuche es erneut.")
            return

        current = fmt_de(snap.total)
        pct, ok = QInputDialog.getDouble(
            None, "Limit kalibrieren",
            "Öffne in Claude Code das Nutzungs-Popup (Befehl /usage).\n"
            "Welchen Prozentwert zeigt dort das 5-Stunden-Limit?\n\n"
            f"Clawd hat im selben Fenster {current} Tokens gezählt.",
            value=65.0, min=0.5, max=100.0, decimals=1)
        if not ok:
            return

        budget = int(round(snap.total / (pct / 100.0)))
        self.settings.setValue("max_tokens", budget)
        set_max_tokens_override(budget)
        self._rebuild_tray_menu()
        QMessageBox.information(
            None, "Kalibriert",
            f"Dein 5-Stunden-Kontingent liegt bei etwa {fmt_de(budget)} Tokens.\n"
            "Clawds Anzeige entspricht ab jetzt Claudes eigener.")
        self.refresh()

    def reset_calibration(self):
        self.settings.remove("max_tokens")
        set_max_tokens_override(None)
        self._rebuild_tray_menu()
        self.refresh()

    # -------------------------------------------------- position memory

    def save_position(self):
        self.settings.setValue("pet_pos", self.pet.pos())

    def _restore_position(self):
        pos = self.settings.value("pet_pos")
        if isinstance(pos, QPoint):
            center = QPoint(pos.x() + self.pet.width() // 2,
                            pos.y() + self.pet.height() // 2)
            screen = (QGuiApplication.screenAt(center)
                      or QGuiApplication.primaryScreen())
            avail = screen.availableGeometry()
            x = max(avail.left(), min(pos.x(), avail.right() - self.pet.width()))
            y = max(avail.top(), min(pos.y(), avail.bottom() - self.pet.height()))
            self.pet.move(x, y)
        else:
            avail = QGuiApplication.primaryScreen().availableGeometry()
            self.pet.move(avail.right() - self.pet.width() - 24,
                          avail.bottom() - self.pet.height() - 48)


# ======================================================================
#  Entry points
# ======================================================================

def run_selftest() -> int:
    """Headless smoke test: scan logs, render every mood, build the panel."""
    app = QApplication(sys.argv)

    snap = scan_usage()
    print(f"[selftest] dir={CLAUDE_PROJECTS_DIR}")
    print(f"[selftest] files_scanned={snap.files_scanned} entries={snap.entries}")
    print(f"[selftest] input={snap.input_tokens} output={snap.output_tokens} "
          f"cache_read={snap.cache_read} cache_creation={snap.cache_creation}")
    print(f"[selftest] total={snap.total} pct={snap.pct:.1f}% "
          f"mood={mood_for_pct(snap.pct)} oldest={snap.oldest} error={snap.error!r}")

    sprites = SpriteSet()
    frames = {m: len(s.pixmaps) for m, s in sprites.sprites.items()}
    print(f"[selftest] sprites loaded: {sorted(sprites.sprites)} frames={frames}")
    print(f"[selftest] by_model (5h): {snap.by_model}")

    pet = PetWidget(None)
    for pct in (10, 60, 90, 120):
        pet.set_pct(pct)
        pm = pet.grab()
        assert not pm.isNull(), f"render failed for pct={pct}"

    panel = PanelWidget()
    panel.update_snapshot(snap)
    panel.adjustSize()
    assert panel.height() > 100, "panel layout collapsed"

    api_snap = UsageSnapshot(updated_at=datetime.now(), source="api", pct=65.0)
    api_snap.buckets = [
        UsageBucket("five_hour", "5-Stunden-Limit", 65.0,
                    datetime.now(timezone.utc) + timedelta(hours=3, minutes=53)),
        UsageBucket("seven_day", "Wöchentlich · alle Modelle", 28.0,
                    datetime.now(timezone.utc) + timedelta(days=5)),
        UsageBucket("seven_day_fable", "Wöchentlich · Fable", 52.0,
                    datetime.now(timezone.utc) + timedelta(days=5)),
    ]
    panel.update_snapshot(api_snap)
    assert set(panel._rows) >= {"five_hour", "seven_day", "seven_day_fable"}
    print(f"[selftest] api-mode panel rows: {sorted(panel._rows)}")

    # Child geometry only exists once the widget has been shown — do it
    # far off-screen so the check runs unattended.
    panel.move(-4000, -4000)
    panel.show()
    app.processEvents()
    assert not panel._rows["estimate"]["bar"].isVisible(), \
        "log-mode row still visible in api mode"

    # let the progress-bar animations settle so the preview shows real bars
    deadline = time.monotonic() + 1.2
    while time.monotonic() < deadline:
        app.processEvents()

    # Verify no label is clipped: every label must fit its own width.
    clipped = []
    for w in panel.findChildren(QLabel):
        txt = w.text()
        if not txt or not w.isVisibleTo(panel):
            continue
        need = w.fontMetrics().boundingRect(
            QRect(0, 0, w.width(), 10_000),
            Qt.TextWordWrap if w.wordWrap() else 0, txt)
        if need.width() > w.width() + 1 or need.height() > w.height() + 1:
            clipped.append((txt[:40], need.width(), w.width(), need.height(), w.height()))
    print(f"[selftest] panel size: {panel.width()}x{panel.height()}, clipped labels: {clipped}")
    assert not clipped, f"clipped labels: {clipped}"

    assert panel.height() > 200, f"panel too short ({panel.height()}px) — rows squashed"

    here = Path(__file__).resolve().parent
    panel.grab().save(str(here / "panel_preview_api.png"))

    panel.update_snapshot(snap)          # back to log mode
    app.processEvents()
    assert not panel._rows["five_hour"]["bar"].isVisible(), \
        "api row still visible in log mode"
    deadline = time.monotonic() + 1.2
    while time.monotonic() < deadline:
        app.processEvents()
    panel.grab().save(str(here / "panel_preview_logs.png"))
    print(f"[selftest] log-mode panel: {panel.width()}x{panel.height()}")
    panel.hide()
    print("[selftest] previews written: panel_preview_api.png, panel_preview_logs.png")

    # calibration: budget derived from Claude's own percentage
    assert effective_max_tokens() == MAX_TOKENS and not is_calibrated()
    set_max_tokens_override(int(round(178_000 / 0.65)))
    assert is_calibrated() and effective_max_tokens() == 273_846
    probe = scan_usage()
    expected = probe.total / 273_846 * 100.0
    assert abs(probe.pct - expected) < 0.01, "calibrated budget not used by scan"
    print(f"[selftest] calibration: 178.000 Tokens @ 65 % -> "
          f"{effective_max_tokens()} Tokens Budget; live pct now {probe.pct:.1f}%")
    set_max_tokens_override(None)
    assert not is_calibrated()

    # fixed-window replay: chained windows and fresh starts after silence
    base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    h = timedelta(hours=1)
    chain = [base, base + 2 * h, base + 4 * h, base + 5 * h + h / 2, base + 6 * h]
    assert _current_window_start(chain, base + 6 * h) == base + 5 * h + h / 2
    fresh = [base, base + 12 * h]
    assert _current_window_start(fresh, base + 12 * h + h / 2) == base + 12 * h
    assert _current_window_start([base], base + 9 * h) is None
    print("[selftest] window replay OK")

    # real-time activity: tail parser + hook registration on scratch files
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "session.jsonl"
        log.write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash"}]}}) + "\n",
            encoding="utf-8")
        assert read_last_activity(log) == ("working", "Bash")
        log.write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "done"}]}}) + "\n",
            encoding="utf-8")
        assert read_last_activity(log) == ("waiting", None)
        future = datetime.now(timezone.utc) + timedelta(seconds=ACTIVITY_IDLE_S + 60)
        assert read_last_activity(log, now=future) is None

        sp = Path(td) / "settings.json"
        sp.write_text(json.dumps(
            {"hooks": {"PreToolUse": [{"matcher": "x", "hooks": []}]}}),
            encoding="utf-8")
        assert register_hooks(sp, 'py "clawd_hook.py"')
        assert hooks_registered(sp)
        data = json.loads(sp.read_text(encoding="utf-8"))
        assert len(data["hooks"]["PreToolUse"]) == 2
        assert "Notification" in data["hooks"]
        assert unregister_hooks(sp)
        assert not hooks_registered(sp)
        data = json.loads(sp.read_text(encoding="utf-8"))
        assert data["hooks"]["PreToolUse"] == [{"matcher": "x", "hooks": []}]
    print("[selftest] activity parser + hook registration OK")

    assert not sprites.sprites or "happy" in sprites.sprites, "happy sprite missing"

    bubble = SpeechBubble()
    bubble.show_text("führt Befehle aus …", pet)
    bubble.hide()

    pet.set_pct(10)
    pet.set_activity(("working", "Bash"))
    assert pet.mood == "focus"
    pet.set_activity(("waiting", None))
    assert pet.mood == "happy"
    pet.set_pct(90)                      # quota alarm overrides activity
    assert pet.mood == "panic"
    pet.set_pct(10)
    pet.set_activity(None)
    assert pet.mood == "chill"
    print("[selftest] activity mood combination OK")

    assert not make_clawd_icon().isNull(), "tray icon failed"
    assert fmt_de(1234567) == "1.234.567"
    assert mood_for_pct(49.9) == "chill" and mood_for_pct(50) == "focus"
    assert mood_for_pct(80) == "panic" and mood_for_pct(100) == "limit"

    print("[selftest] OK")
    del app
    return 0


def main() -> int:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    if "--selftest" in sys.argv:
        return run_selftest()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # lives in the tray
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setWindowIcon(make_app_icon())

    controller = ClawdApp(app)
    controller.start()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
