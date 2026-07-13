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
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QLockFile,
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

# Session-window budget in COST-WEIGHTED token units (see WEIGHT_* and
# MODEL_COST below). Anthropic publishes no real numbers, so this is a rough
# Max-5x starting guess; calibration (manual via tray menu, or automatic
# whenever a live API sync succeeds) replaces it and is persisted in QSettings.
# It is sized for the cost-weighted scale (Opus tokens weigh ~5x), so an
# uncalibrated Opus-heavy window reads a plausible mid-range %, not a false
# >100% "limit".
MAX_TOKENS = 40_000_000            # placeholder default (Max 5x, cost-weighted units)
PLAN_NAME = "Max 5x"               # shown in the panel header
WINDOW_HOURS = 5                   # length of Anthropic's fixed session window
REPLAY_HOURS = 48                  # look-back to reconstruct the window chain
WEEK_REPLAY_HOURS = 192            # look-back covering the weekly limit window
SCAN_INTERVAL_MS = 2_000           # how often the logs are rescanned (feels live)
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Anthropic meters the plan limits by cost, not by raw token count. These
# weights mirror the public API price ratios (output = 5x input, cache write
# = 1.25x, cache read = 0.1x), so the local percentage scales like Claude's
# own display across changing usage mixes. Displayed token numbers stay raw
# input+output; only percentages and calibration use the weighted sum.
WEIGHT_INPUT = 1.0
WEIGHT_OUTPUT = 5.0
WEIGHT_CACHE_CREATION = 1.25
WEIGHT_CACHE_READ = 0.1

# Models also cost very differently against the plan. These multipliers mirror
# the public per-token price tiers (relative to Sonnet), so the estimate tracks
# Claude's percentage across changing model mixes — one calibration then holds
# whether you code with Opus or Fable, instead of drifting every window.
MODEL_COST = {"Opus": 5.0, "Sonnet": 1.0, "Haiku": 0.3, "Fable": 0.3}
WEIGHT_VERSION = 2      # bump to drop stale calibrations after changing the weights


def model_cost(name: str) -> float:
    return MODEL_COST.get(name, 1.0)

PET_HEIGHT = 132                   # on-screen pixel height of Clawd
PANEL_WIDTH = 392                  # width of the slide-out panel

# Animated GIF sprites (community pixel-art recreation of the official mascot,
# MIT-licensed: https://github.com/KebeliSamet0/clawd). If a file is missing,
# the built-in vector Clawd is drawn instead.
SPRITE_DIR = Path(__file__).resolve().parent / "sprites"
SPRITE_FILES = {
    "sleep": "clawd-sleeping.gif",       # no activity in the rolling window
    "chill": "clawd-idle.gif",           # budget left, no live activity
    "focus": "clawd-building.gif",       # running commands / 50-80 % quota
    "type": "clawd-typing.gif",          # editing / writing files
    "read": "clawd-idle-reading.gif",    # reading / searching / browsing
    "think": "clawd-thinking.gif",       # working with no tool (Claude thinking)
    "notify": "clawd-notification.gif",  # Claude is waiting for your input
    "happy": "clawd-happy.gif",          # turn finished
    "panic": "clawd-debugger.gif",       # 80-100 % quota / tool error
    "limit": "clawd-error.gif",          # over the limit
    "pet": "clawd-react-double-jump.gif",  # transient double-click reaction
    "annoyed": "clawd-react-annoyed.gif",  # over-petted
    # random idle flourishes, played occasionally while calm (see IDLE_FLOURISHES)
    "juggle": "clawd-juggling.gif",
    "conduct": "clawd-conducting.gif",
    "sweep": "clawd-sweeping.gif",
    "carry": "clawd-carrying.gif",
}

# --- Real-time activity (Stufe 1: log watcher, Stufe 2: opt-in hooks) -------
ACTIVITY_POLL_MS = 1500     # how often the newest session log is checked
ACTIVITY_IDLE_S = 240       # log untouched this long -> no activity
HOOK_UDP_PORT = 52741       # clawd_hook.py sends Claude Code events here
CLAUDE_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"
HOOK_EVENTS = ["PreToolUse", "Notification", "Stop", "SessionStart"]

# --- Live sync with the Anthropic usage endpoint ----------------------------
# Two token sources, in order of preference:
#  1. Clawd's OWN independent OAuth token in ~/.clawd/auth.json (set up via the
#     one-time login, "Clawd-Login einrichten"). This is a separate grant — like
#     a third device — so Clawd may auto-refresh it freely: rotating it only ever
#     affects this file and can never lock the user out of Claude Code.
#  2. Fallback: the token Claude Code itself stored in ~/.claude/.credentials.json,
#     READ-ONLY. Clawd never refreshes or writes that shared file, because a
#     failed write-back of Claude Code's rotating token could break its login.
# If neither token is valid, the local log estimate is used; the last live
# reading is remembered as a calibration so the estimate stays close.
USE_API_USAGE = True
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
CLAWD_AUTH_FILE = Path.home() / ".clawd" / "auth.json"     # Clawd's own token (writable)
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"   # Claude Code's public client id
REFRESH_COOLDOWN_S = 300.0         # don't retry a failing own-token refresh more than every 5 min
API_OK_INTERVAL_S = 30.0           # hit the usage endpoint at most this often when healthy
API_RETRY_S = 5.0                  # base back-off after a failed usage fetch
API_MAX_BACKOFF_S = 120.0          # cap the exponential back-off on repeated failures
API_STALE_S = 180.0                # keep showing the last live % up to this long, then estimate

ORG_NAME = "ClawdPet"
APP_NAME = "Clawd"
APP_VERSION = "1.8.0"

# --- Burn-rate forecast + threshold notifications ----------------------------
BURN_LOOKBACK_S = 3600      # forecast fits over at most the last hour
BURN_MIN_SPAN_S = 300       # ... but needs at least 5 minutes of samples
NOTIFY_THRESHOLDS = (95.0, 80.0)   # toast on upward crossings, highest wins
WAIT_ALERT_MIN_S = 15       # only alert "your turn" on turns worked at least this long
ALERT_COOLDOWN_S = 20       # collapse near-simultaneous alerts (hook + log)

# --- Autostart (Windows Run key, toggled via the tray menu) ------------------
AUTOSTART_REG_PATH = (r"HKEY_CURRENT_USER\Software\Microsoft"
                      r"\Windows\CurrentVersion\Run")
AUTOSTART_REG_NAME = "ClawdPet"

# --- Update check (GitHub Releases, read-only, best effort) ------------------
GITHUB_REPO = "malzinger/clawd-pet"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
GITHUB_LATEST_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
UPDATE_CHECK = True                # look for a newer release (once/launch + 6 h)
UPDATE_RECHECK_MS = 6 * 3600 * 1000

# --- Usage history (local sparkline in the panel) ----------------------------
HISTORY_FILE = Path.home() / ".clawd" / "history.json"
HISTORY_INTERVAL_S = 300           # store at most one point every 5 minutes
HISTORY_KEEP_DAYS = 7              # prune points older than this
HISTORY_WINDOW_H = 24             # span shown in the panel sparkline
HISTORY_GAP_S = 1800              # break the line across gaps longer than this


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
    week_total: int = 0                           # input+output in the weekly window
    week_by_model: dict = field(default_factory=dict)
    week_start: Optional[datetime] = None
    week_reset: Optional[datetime] = None
    weighted: float = 0.0                         # cost-weighted window usage
    week_weighted: float = 0.0
    by_model_weighted: dict = field(default_factory=dict)
    week_by_model_weighted: dict = field(default_factory=dict)
    burn_eta: Optional[datetime] = None           # projected time of hitting 100 %


_MAX_TOKENS_OVERRIDE: Optional[int] = None      # set once manually calibrated

# Auto-calibration: whenever the live API sync succeeds, the exact percentages
# plus our locally counted tokens yield the real budgets — remembered so the
# log-estimate mode stays accurate even after the OAuth token expires again.
_AUTO_BUDGET_5H: Optional[int] = None
_WEEKLY_ANCHOR: Optional[datetime] = None       # one known weekly reset boundary
_WEEKLY_BUDGET_ALL: Optional[int] = None
_WEEKLY_BUDGET_MODELS: dict = {}


def effective_max_tokens() -> int:
    """Manual calibration wins, then auto-calibration, then the placeholder."""
    return _MAX_TOKENS_OVERRIDE or _AUTO_BUDGET_5H or MAX_TOKENS


def set_auto_calibration(budget_5h=None, weekly_anchor=None,
                         weekly_budget=None, weekly_model_budgets=None) -> None:
    global _AUTO_BUDGET_5H, _WEEKLY_ANCHOR, _WEEKLY_BUDGET_ALL, _WEEKLY_BUDGET_MODELS
    if budget_5h:
        _AUTO_BUDGET_5H = int(budget_5h)
    if weekly_anchor is not None:
        _WEEKLY_ANCHOR = weekly_anchor
    if weekly_budget:
        _WEEKLY_BUDGET_ALL = int(weekly_budget)
    if weekly_model_budgets:
        _WEEKLY_BUDGET_MODELS = {str(k): int(v)
                                 for k, v in weekly_model_budgets.items()}


def reset_auto_calibration() -> None:
    global _AUTO_BUDGET_5H, _WEEKLY_ANCHOR, _WEEKLY_BUDGET_ALL, _WEEKLY_BUDGET_MODELS
    _AUTO_BUDGET_5H = None
    _WEEKLY_ANCHOR = None
    _WEEKLY_BUDGET_ALL = None
    _WEEKLY_BUDGET_MODELS = {}


def auto_calibration() -> dict:
    return {"budget_5h": _AUTO_BUDGET_5H, "anchor": _WEEKLY_ANCHOR,
            "weekly_budget": _WEEKLY_BUDGET_ALL,
            "models": dict(_WEEKLY_BUDGET_MODELS)}


def auto_budget_active() -> bool:
    return _AUTO_BUDGET_5H is not None


def weekly_budget_all() -> Optional[int]:
    return _WEEKLY_BUDGET_ALL


def weekly_model_budgets() -> dict:
    return dict(_WEEKLY_BUDGET_MODELS)


def _weekly_window(now: datetime):
    """(start, reset) of the current fixed weekly window.

    Anchored at a reset boundary learned from the live API; without one we
    fall back to a rolling 7 days (reset unknown)."""
    week = timedelta(days=7)
    if _WEEKLY_ANCHOR is not None:
        reset = _WEEKLY_ANCHOR + week * math.ceil((now - _WEEKLY_ANCHOR) / week)
        if reset <= now:
            reset += week
        return reset - week, reset
    return now - week, None


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


_FILE_CACHE: dict = {}      # path -> (mtime_ns, size, entries) — worker thread only


def _parse_file_entries(fp: Path) -> list:
    """All usage entries of one session log, cached by (mtime, size).

    Finished session files never change, so each is parsed exactly once per
    process; only actively written logs are re-read on the 20 s rescans."""
    try:
        st = fp.stat()
    except OSError:
        return []
    key = str(fp)
    cached = _FILE_CACHE.get(key)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    entries = []
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
                if ts is None:
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
                entry.append(mid if isinstance(mid, str) else "")
                entries.append(entry)
    except OSError:
        return []
    _FILE_CACHE[key] = (st.st_mtime_ns, st.st_size, entries)
    return entries


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
    horizon = now - timedelta(hours=WEEK_REPLAY_HOURS)
    chain_cutoff = now - timedelta(hours=REPLAY_HOURS)
    snap = UsageSnapshot(updated_at=datetime.now())

    if not CLAUDE_PROJECTS_DIR.is_dir():
        snap.error = tr("err_dir", p=CLAUDE_PROJECTS_DIR)
        return snap

    by_msg_id = {}
    anonymous = []
    newest_mtime = datetime.min.replace(tzinfo=timezone.utc)

    try:
        files = list(CLAUDE_PROJECTS_DIR.rglob("*.jsonl"))
    except OSError as exc:
        snap.error = tr("err_logs", e=exc)
        return snap

    for fp in files:
        if should_stop is not None and should_stop():
            break   # app is quitting — a partial snapshot is fine
        try:
            mtime = datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < horizon:
            continue  # untouched since before the weekly window — irrelevant
        if mtime > newest_mtime:
            newest_mtime = mtime
            snap.newest_file = str(fp)
        snap.files_scanned += 1
        for entry in _parse_file_entries(fp):
            ts = entry[4]
            if ts < horizon or ts > now + timedelta(minutes=5):
                continue
            mid = entry[6]
            if mid:
                prev = by_msg_id.get(mid)
                if prev is None or _entry_weight(entry) > _entry_weight(prev):
                    by_msg_id[mid] = entry
            else:
                anonymous.append(entry)

    all_entries = list(by_msg_id.values()) + anonymous
    chain_ts = sorted(e[4] for e in all_entries if e[4] >= chain_cutoff)
    window_start = _current_window_start(chain_ts, now)
    snap.oldest = window_start          # countdown target: window_start + 5 h

    week_start, week_reset = _weekly_window(now)
    snap.week_start = week_start
    snap.week_reset = week_reset

    for inp, out, cr, cc, ts, model, _mid in all_entries:
        name = pretty_model(model)
        w = ((inp * WEIGHT_INPUT + out * WEIGHT_OUTPUT
              + cc * WEIGHT_CACHE_CREATION + cr * WEIGHT_CACHE_READ)
             * model_cost(name))
        if ts >= week_start:
            snap.week_total += inp + out
            snap.week_weighted += w
            snap.week_by_model[name] = snap.week_by_model.get(name, 0) + inp + out
            snap.week_by_model_weighted[name] = (
                snap.week_by_model_weighted.get(name, 0.0) + w)
        if window_start is None or ts < window_start:
            continue                    # previous, already reset window
        snap.entries += 1
        snap.input_tokens += inp
        snap.output_tokens += out
        snap.cache_read += cr
        snap.cache_creation += cc
        snap.weighted += w
        snap.by_model[name] = snap.by_model.get(name, 0) + inp + out
        snap.by_model_weighted[name] = snap.by_model_weighted.get(name, 0.0) + w

    snap.total = snap.input_tokens + snap.output_tokens
    budget = effective_max_tokens()
    snap.pct = (snap.weighted / budget * 100.0) if budget > 0 else 0.0
    return snap


class ScanThread(QThread):
    """Runs scan_usage() off the GUI thread."""
    result = pyqtSignal(object)

    def run(self):
        try:
            snap = collect_usage(should_stop=self.isInterruptionRequested)
        except Exception as exc:  # never let the worker die silently
            snap = UsageSnapshot(error=tr("err_scan", e=exc), updated_at=datetime.now())
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


def bucket_label(key: str) -> str:
    if key == "five_hour":
        return tr("row_5h")
    if key == "seven_day":
        return tr("row_week_all")
    if key.startswith("seven_day_"):
        name = key[len("seven_day_"):].replace("_", " ").title()
        return tr("row_week_model", name=name)
    return key.replace("_", " ").title()


_clawd_refresh_ts = 0.0            # throttles own-token refresh attempts (module-global)


def _store_clawd_auth(auth: dict) -> bool:
    """Atomically write Clawd's own token file, tightened to owner-only perms."""
    try:
        CLAWD_AUTH_FILE.parent.mkdir(exist_ok=True)
        tmp = CLAWD_AUTH_FILE.with_name(CLAWD_AUTH_FILE.name + ".tmp")
        tmp.write_text(json.dumps(auth), encoding="utf-8")
        os.replace(str(tmp), str(CLAWD_AUTH_FILE))
        try:
            os.chmod(str(CLAWD_AUTH_FILE), 0o600)   # no-op-ish on Windows, protects POSIX
        except OSError:
            pass
        return True
    except OSError:
        return False


def _refresh_clawd_token(auth: dict) -> Optional[str]:
    """Refresh Clawd's OWN independent token (~/.clawd/auth.json) and write it
    back. Safe to write: this grant is separate from Claude Code's login, so a
    failed refresh only ever drops Clawd back to the read-only/estimate path —
    it can never lock the user out of Claude Code. Throttled; returns the new
    access token or None on any failure."""
    global _clawd_refresh_ts
    now = time.time()
    if now - _clawd_refresh_ts < REFRESH_COOLDOWN_S:
        return None
    _clawd_refresh_ts = now
    refresh = auth.get("refresh_token")
    if not refresh:
        return None
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": OAUTH_CLIENT_ID,
    }).encode("utf-8")
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "ClawdPet/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    new_at = data.get("access_token")
    if not new_at:
        return None
    auth["access_token"] = new_at
    if data.get("refresh_token"):
        auth["refresh_token"] = data["refresh_token"]
    exp_in = data.get("expires_in")
    if isinstance(exp_in, (int, float)) and exp_in > 0:
        auth["expires_at"] = int(time.time() * 1000 + exp_in * 1000)
    _store_clawd_auth(auth)                        # best-effort; own file, low stakes
    return new_at


def _clawd_own_token() -> Optional[str]:
    """Clawd's own independent access token, refreshing it if it has expired."""
    try:
        auth = json.loads(CLAWD_AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = auth.get("access_token")
    expires_ms = auth.get("expires_at") or 0
    if token and time.time() * 1000 < expires_ms - 60_000:
        return token
    return _refresh_clawd_token(auth)   # expired — safe self-refresh of our own token


def _get_access_token() -> Optional[str]:
    """Prefer Clawd's own independent token (auto-refreshed); otherwise fall back
    to the token Claude Code stored, READ-ONLY (never refreshed or written, so a
    rotation we could not persist can never lock the user out of Claude Code)."""
    if os.environ.get("CLAWD_NO_API"):
        return None
    own = _clawd_own_token()
    if own:
        return own
    try:
        creds = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    oauth = creds.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    expires_ms = oauth.get("expiresAt") or 0
    if token and time.time() * 1000 < expires_ms - 60_000:
        return token
    return None                   # both expired — fall back to the log estimate


def _clawd_build_authorize_url():
    """Build a fresh PKCE authorize URL for Clawd's own login.
    Returns (url, code_verifier, redirect_uri)."""
    import secrets, hashlib, base64, urllib.parse
    redirect = "https://console.anthropic.com/oauth/code/callback"
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    params = {
        "code": "true", "client_id": OAUTH_CLIENT_ID, "response_type": "code",
        "redirect_uri": redirect, "scope": "user:profile user:inference",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    }
    return ("https://platform.claude.com/oauth/authorize?"
            + urllib.parse.urlencode(params), verifier, redirect)


def _clawd_exchange_code(raw_code: str, verifier: str, redirect_uri: str) -> None:
    """Exchange the pasted authorization code (``code#state``) for Clawd's own
    token and store it in ~/.clawd/auth.json. Raises on failure."""
    raw = raw_code.strip()
    code, _, state = raw.partition("#")
    body = json.dumps({
        "grant_type": "authorization_code", "code": code, "state": state,
        "code_verifier": verifier, "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
    }).encode("utf-8")
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "ClawdPet/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    at = (data or {}).get("access_token")
    if not at:
        raise ValueError("no access_token in response")
    exp_in = data.get("expires_in") or 0
    auth = {
        "access_token": at,
        "refresh_token": data.get("refresh_token"),
        "expires_at": int(time.time() * 1000 + exp_in * 1000),
        "scope": data.get("scope"),
    }
    if not _store_clawd_auth(auth):
        raise OSError("could not write " + str(CLAWD_AUTH_FILE))


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
                key, label = "five_hour", tr("row_5h")
            elif kind == "weekly_all":
                key, label = "seven_day", tr("row_week_all")
            elif isinstance(display, str) and display:
                key, label = (f"weekly_{display.lower()}",
                              tr("row_week_model", name=display))
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
            buckets.append(UsageBucket(key=key, label=bucket_label(key),
                                       pct=pct, resets_at=resets_at))

    order = {"five_hour": 0, "seven_day": 1}
    buckets.sort(key=lambda b: order.get(b.key, 2))
    return buckets or None


_api_cache = {"buckets": None, "ts": 0.0, "next": 0.0, "fails": 0}   # throttle + back off


def collect_usage(should_stop=None) -> UsageSnapshot:
    """API first (exact numbers), local log estimate as fallback. The usage
    endpoint is polled at most every API_OK_INTERVAL_S; between polls the last
    buckets are reused so the log scan can run every couple of seconds without
    hammering Anthropic. Repeated failures back off exponentially, and a single
    blip keeps the last live reading (up to API_STALE_S) instead of flipping the
    whole panel to the estimate."""
    fetched_ok = False
    if USE_API_USAGE:
        now_s = time.time()
        if now_s >= _api_cache["next"]:
            fresh = fetch_api_usage()
            if fresh:
                _api_cache["buckets"] = fresh
                _api_cache["ts"] = now_s
                _api_cache["fails"] = 0
                _api_cache["next"] = now_s + API_OK_INTERVAL_S
                fetched_ok = True
            else:
                _api_cache["fails"] += 1
                backoff = min(API_MAX_BACKOFF_S, API_RETRY_S * 2 ** (_api_cache["fails"] - 1))
                _api_cache["next"] = now_s + backoff
                if _api_cache["buckets"] is not None and now_s - _api_cache["ts"] > API_STALE_S:
                    _api_cache["buckets"] = None       # last live reading too old — show the estimate
        buckets = _api_cache["buckets"]
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

            # auto-calibrate only from a *freshly fetched* reading: pairing a
            # cached (stale) percentage with the still-growing local token count
            # would slowly skew the learned budget.
            if fetched_ok:
                budget_5h = weekly_budget = anchor = None
                model_budgets = {}
                for b in buckets:
                    if b.key == "seven_day" and b.resets_at is not None:
                        anchor = b.resets_at
                    if b.pct < 3.0:
                        continue          # too close to zero to divide reliably
                    if b.key == "five_hour" and snap.weighted > 0:
                        budget_5h = round(snap.weighted / (b.pct / 100.0))
                    elif b.key == "seven_day" and snap.week_weighted > 0:
                        weekly_budget = round(snap.week_weighted / (b.pct / 100.0))
                    elif b.key.startswith("weekly_"):
                        name = b.label.split("·")[-1].strip()
                        wtok = snap.week_by_model_weighted.get(name, 0.0)
                        if wtok > 0:
                            model_budgets[name] = round(wtok / (b.pct / 100.0))
                set_auto_calibration(budget_5h, anchor, weekly_budget,
                                     model_budgets or None)
            return snap
    return scan_usage(should_stop=should_stop)


def _fmt_reset(resets_at: Optional[datetime]) -> str:
    if resets_at is None:
        return ""
    secs = int((resets_at - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return tr("reset_running")
    if secs < 24 * 3600:
        h, m = divmod(secs // 60, 60)
        if h:
            return tr("reset_in_hm", h=h, m=m)
        return tr("reset_in_m", m=m)
    local = resets_at.astimezone()
    wd = tr("weekdays")[local.weekday()]
    return tr("reset_at", wd=wd, t=local.strftime("%H:%M"))


# ======================================================================
#  Real-time activity — Stufe 1: watch the newest session log's tail
# ======================================================================

TOOL_BUBBLES = {
    "de": {
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
    },
    "en": {
        "Read": "reading files …",
        "Edit": "writing code …",
        "Write": "writing code …",
        "MultiEdit": "writing code …",
        "NotebookEdit": "writing code …",
        "Bash": "running commands …",
        "PowerShell": "running commands …",
        "Grep": "searching the code …",
        "Glob": "searching the code …",
        "Task": "delegating to agents …",
        "Agent": "delegating to agents …",
        "WebFetch": "browsing the web …",
        "WebSearch": "browsing the web …",
    },
}


def tool_bubble(tool) -> Optional[str]:
    return TOOL_BUBBLES.get(_LANG, TOOL_BUBBLES["de"]).get(tool or "")


# Detailed phrasing that names the concrete target ("{d}"), used by the panel's
# "what Clawd is working on" line and by the enriched speech bubbles.
TOOL_ACTIONS = {
    "de": {
        "Read": "liest {d}", "Edit": "bearbeitet {d}", "Write": "schreibt {d}",
        "MultiEdit": "bearbeitet {d}", "NotebookEdit": "bearbeitet {d}",
        "Bash": "führt aus: {d}", "PowerShell": "führt aus: {d}",
        "Grep": "durchsucht: {d}", "Glob": "durchsucht: {d}",
        "Task": "delegiert: {d}", "Agent": "delegiert: {d}",
        "WebFetch": "surft: {d}", "WebSearch": "sucht: {d}",
    },
    "en": {
        "Read": "reading {d}", "Edit": "editing {d}", "Write": "writing {d}",
        "MultiEdit": "editing {d}", "NotebookEdit": "editing {d}",
        "Bash": "running: {d}", "PowerShell": "running: {d}",
        "Grep": "searching: {d}", "Glob": "searching: {d}",
        "Task": "delegating: {d}", "Agent": "delegating: {d}",
        "WebFetch": "browsing: {d}", "WebSearch": "searching: {d}",
    },
}


def tool_action(tool, detail: str) -> str:
    """Localized phrase naming the concrete target, e.g. 'bearbeitet foo.py'."""
    if not detail:
        return tool_bubble(tool) or ""
    tmpl = TOOL_ACTIONS.get(_LANG, TOOL_ACTIONS["de"]).get(tool or "")
    return tmpl.format(d=detail) if tmpl else (tool_bubble(tool) or detail)


def tool_detail(name, inp) -> str:
    """Extract the concrete target from a tool_use input block (best effort)."""
    if not isinstance(inp, dict):
        return ""
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        fp = inp.get("file_path") or inp.get("notebook_path") or ""
        return Path(fp).name if fp else ""
    if name in ("Bash", "PowerShell"):
        cmd = (inp.get("command") or "").strip().splitlines()
        return cmd[0][:48] if cmd else ""
    if name in ("Grep", "Glob"):
        return (inp.get("pattern") or "")[:40]
    if name in ("Task", "Agent"):
        return (inp.get("description") or "")[:40]
    if name == "WebFetch":
        return (inp.get("url") or "")[:48]
    if name == "WebSearch":
        return (inp.get("query") or "")[:40]
    return ""


def _user_prompt_text(content) -> str:
    """A genuine typed user prompt from a message's content, or '' for
    tool results / slash-command and system wrappers (which start with '<')."""
    if isinstance(content, str):
        s = content.strip()
    elif isinstance(content, list):
        s = " ".join(
            b.get("text", "").strip() for b in content
            if isinstance(b, dict) and b.get("type") == "text")
    else:
        return ""
    s = " ".join(s.split())
    return "" if not s or s.startswith("<") else s


@dataclass
class SessionContext:
    """What Claude is doing in the newest session, for the panel task view."""
    kind: Optional[str] = None    # "working" | "waiting" | None (idle)
    tool: Optional[str] = None    # current tool name
    detail: str = ""              # concrete target (file, command, pattern)
    task: str = ""                # latest genuine user prompt (truncated)
    project: str = ""             # working directory basename


def read_session_context(path: Path,
                         now: Optional[datetime] = None) -> Optional[SessionContext]:
    """Inspect the tail of a session log.

    Returns a SessionContext (current activity + the task Claude is working on
    + project), or None when the log has gone quiet. The .kind/.tool pair keeps
    the exact meaning of the old activity tuple so the mood logic is unchanged.
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

    ctx = SessionContext()
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
        if not ctx.project and isinstance(rec.get("cwd"), str) and rec["cwd"]:
            ctx.project = Path(rec["cwd"]).name
        rtype = rec.get("type")
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else None

        if ctx.kind is None and msg is not None:
            if rtype == "assistant":
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if (isinstance(block, dict)
                                and block.get("type") == "tool_use"):
                            ctx.kind = "working"
                            ctx.tool = block.get("name")
                            ctx.detail = tool_detail(ctx.tool, block.get("input"))
                            break
                if ctx.kind is None:
                    ctx.kind = "waiting"  # spoke without tools -> turn is over
            elif rtype == "user":
                ctx.kind = "working"      # tool result / prompt just arrived

        if not ctx.task and rtype == "user" and msg is not None:
            txt = _user_prompt_text(msg.get("content"))
            if txt:
                ctx.task = txt[:160]

        if ctx.kind is not None and ctx.task and ctx.project:
            break

    return ctx if ctx.kind is not None else None


def read_last_activity(path: Path, now: Optional[datetime] = None):
    """Backward-compatible activity tuple derived from the session context."""
    ctx = read_session_context(path, now)
    return (ctx.kind, ctx.tool) if ctx and ctx.kind else None


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
    "read": "#3fb950",
    "think": "#3fb950",
    "type": "#d29922",
    "focus": "#d29922",
    "notify": "#d29922",
    "happy": "#3fb950",
    "panic": "#f0883e",
    "limit": "#f85149",
    "pet": "#3fb950",
    "annoyed": "#d29922",
    "juggle": "#3fb950", "conduct": "#3fb950", "sweep": "#3fb950",
    "carry": "#3fb950",
}

# Random idle animations played now and then while Clawd is calm (mood "chill").
IDLE_FLOURISHES = ("juggle", "conduct", "sweep", "carry")
IDLE_SWITCH_MS = 6000       # how often the idle animation may change
IDLE_FLOURISH_PROB = 0.45   # chance an idle tick starts a flourish (else idle)
PET_SPAM_WINDOW_S = 3.0     # over-petting window
PET_SPAM_COUNT = 3          # this many pets within the window -> annoyed

# Which animation each running tool maps to (used only when quota is calm).
TOOL_MOODS = {
    "Read": "read", "Grep": "read", "Glob": "read",
    "WebFetch": "read", "WebSearch": "read",
    "Edit": "type", "Write": "type", "MultiEdit": "type", "NotebookEdit": "type",
    "Bash": "focus", "PowerShell": "focus", "Task": "focus", "Agent": "focus",
}

# If a mapped animation is missing (older sprites/ folder), fall back sensibly.
MOOD_FALLBACK = {"type": "focus", "read": "chill", "think": "focus",
                 "notify": "happy", "pet": "happy", "annoyed": "happy",
                 "juggle": "chill", "conduct": "chill", "sweep": "chill",
                 "carry": "chill"}


# ======================================================================
#  Language / i18n — toggled via the tray menu, persisted in QSettings
# ======================================================================

_LANG = "de"

def burn_eta(samples, limit: float = 100.0) -> Optional[datetime]:
    """Linear burn-rate forecast: when does usage hit the limit?

    samples: (utc datetime, pct) tuples, oldest first, all from the current
    session window. Returns the projected UTC time of reaching `limit`, or
    None while there is too little data or usage is flat/falling.
    """
    if len(samples) < 2:
        return None
    (t0, p0), (t1, p1) = samples[0], samples[-1]
    span = (t1 - t0).total_seconds()
    if span < BURN_MIN_SPAN_S or p1 <= p0 or p1 >= limit:
        return None
    rate = (p1 - p0) / span                       # pct per second
    return t1 + timedelta(seconds=(limit - p1) / rate)


def notify_decision(prev: Optional[float], cur: float) -> Optional[str]:
    """Map a pct transition between two scans to a notification key (or None).

    A big downward jump after real usage means the 5-hour window reset;
    upward crossings of the warning thresholds fire once each, highest wins.
    """
    if prev is None:
        return None
    if prev >= 50.0 and cur < prev - 40.0:
        return "reset"
    for th in NOTIFY_THRESHOLDS:
        if prev < th <= cur:
            return f"warn{int(th)}"
    return None


def autostart_supported() -> bool:
    return sys.platform == "win32"


def autostart_command() -> Optional[str]:
    """Command line the Windows Run key should launch at login."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    script = Path(__file__).resolve()
    for runner in ("pythonw", "pyw", "python", "py"):
        exe = shutil.which(runner)
        if exe:
            return f'"{exe}" "{script}"'
    return None


def autostart_enabled() -> bool:
    if not autostart_supported():
        return False
    reg = QSettings(AUTOSTART_REG_PATH, QSettings.NativeFormat)
    return bool(reg.value(AUTOSTART_REG_NAME))


def set_autostart(enabled: bool) -> bool:
    if not autostart_supported():
        return False
    command = autostart_command()
    if enabled and not command:
        return False
    reg = QSettings(AUTOSTART_REG_PATH, QSettings.NativeFormat)
    if enabled:
        reg.setValue(AUTOSTART_REG_NAME, command)
    else:
        reg.remove(AUTOSTART_REG_NAME)
    reg.sync()
    return reg.status() == QSettings.NoError


def parse_version(tag: str) -> tuple:
    """'v1.2.0' / '1.2' -> (1, 2, 0); non-numeric chunks count as 0."""
    if not tag:
        return ()
    core = tag.strip().lstrip("vV").split("-")[0].split("+")[0]
    parts = []
    for chunk in core.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def version_is_newer(latest: str, current: str) -> bool:
    """True if the release tag `latest` is a strictly newer version."""
    lv, cv = parse_version(latest), parse_version(current)
    if not lv:
        return False
    n = max(len(lv), len(cv))
    lv += (0,) * (n - len(lv))
    cv += (0,) * (n - len(cv))
    return lv > cv


class UpdateThread(QThread):
    """Fetch the latest GitHub release tag off the GUI thread (best effort)."""
    result = pyqtSignal(str, str)   # (tag_name, html_url); empty tag on failure

    def run(self):
        try:
            req = urllib.request.Request(
                GITHUB_LATEST_API,
                headers={"User-Agent": f"ClawdPet/{APP_VERSION}",
                         "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            tag = str(data.get("tag_name") or "")
            url = str(data.get("html_url") or RELEASES_URL)
            self.result.emit(tag, url)
        except (urllib.error.URLError, OSError, ValueError):
            self.result.emit("", "")


class HistoryStore:
    """Append-only local usage history for the panel sparkline (JSON file)."""

    def __init__(self, path: Path = HISTORY_FILE):
        self.path = path
        self._points = self._load()       # list[(datetime utc, pct)]
        # honour the throttle across restarts: the newest on-disk point counts
        self._last_write: Optional[datetime] = (
            self._points[-1][0] if self._points else None)

    def _load(self) -> list:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out = []
        for it in raw if isinstance(raw, list) else []:
            if not isinstance(it, dict):
                continue
            ts = _parse_iso_ts(it.get("t"))
            if ts is None:
                continue
            try:
                out.append((ts, float(it.get("pct", 0.0))))
            except (TypeError, ValueError):
                pass
        out.sort(key=lambda p: p[0])
        return out

    def add(self, now: datetime, pct: float) -> bool:
        """Record a point, throttled to HISTORY_INTERVAL_S. True if stored."""
        if (self._last_write is not None and
                (now - self._last_write).total_seconds() < HISTORY_INTERVAL_S):
            return False
        self._points.append((now, pct))
        self._last_write = now
        cutoff = now - timedelta(days=HISTORY_KEEP_DAYS)
        self._points = [p for p in self._points if p[0] >= cutoff]
        self._save()
        return True

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                [{"t": t.isoformat(), "pct": round(p, 2)}
                 for t, p in self._points]), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def series(self, window_h: int = HISTORY_WINDOW_H,
               now: Optional[datetime] = None) -> list:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=window_h)
        return [p for p in self._points if p[0] >= cutoff]


class HistoryChart(QWidget):
    """Compact area sparkline of the 5-hour usage pct over the last day."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._series = []
        self.setFixedHeight(54)

    def set_series(self, series) -> None:
        self._series = list(series)
        self.setVisible(len(self._series) >= 2)
        self.update()

    def paintEvent(self, _event):
        if len(self._series) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        pad = 2.0
        t0 = self._series[0][0]
        span = (self._series[-1][0] - t0).total_seconds() or 1.0

        def px(t):
            return pad + (t - t0).total_seconds() / span * (w - 2 * pad)

        def py(pct):
            v = max(0.0, min(100.0, pct))
            return h - pad - v / 100.0 * (h - 2 * pad)

        # 80 % warning guide
        p.setPen(QPen(QColor("#6b5836"), 1, Qt.DashLine))
        y80 = py(80.0)
        p.drawLine(int(pad), int(y80), int(w - pad), int(y80))

        # split into segments so long gaps (pet was off) are not bridged
        segments, prev_t = [[]], None
        for t, pct in self._series:
            if prev_t is not None and (t - prev_t).total_seconds() > HISTORY_GAP_S:
                segments.append([])
            segments[-1].append((px(t), py(pct)))
            prev_t = t

        line_pen = QPen(QColor("#6879f8"), 2)
        line_pen.setCapStyle(Qt.RoundCap)
        line_pen.setJoinStyle(Qt.RoundJoin)
        for seg in segments:
            if len(seg) < 2:
                if seg:
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor("#6879f8"))
                    x, y = seg[0]
                    p.drawEllipse(QRectF(x - 1.6, y - 1.6, 3.2, 3.2))
                continue
            path = QPainterPath()
            path.moveTo(seg[0][0], seg[0][1])
            for x, y in seg[1:]:
                path.lineTo(x, y)
            fill = QPainterPath(path)
            fill.lineTo(seg[-1][0], h - pad)
            fill.lineTo(seg[0][0], h - pad)
            fill.closeSubpath()
            p.fillPath(fill, QColor(104, 121, 248, 40))
            p.strokePath(path, line_pen)
        p.end()


STRINGS = {
    "de": {
        "panel_title": "Plan-Nutzungslimits · {plan}",
        "row_5h": "5-Stunden-Limit",
        "row_week_all": "Wöchentlich · alle Modelle",
        "row_week_model": "Wöchentlich · {name}",
        "tokens_n": "{n} Tokens",
        "tokens_inout": "{n} Tokens (In + Out)",
        "rolling7": "≈ letzte 7 Tage",
        "reset_running": "Zurücksetzung läuft …",
        "reset_in_hm": "Zurücksetzung in {h} Std. {m:02d} Min.",
        "reset_in_m": "Zurücksetzung in {m} Min.",
        "reset_at": "Zurücksetzung {wd}, {t}",
        "weekdays": ("Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So."),
        "detail_used": "{n} Tokens verbraucht (Input + Output) · {hint}",
        "hint_manual": "Limit kalibriert",
        "hint_auto": "Limits automatisch kalibriert",
        "hint_placeholder": "Platzhalter-Limit – im Tray-Menü kalibrieren",
        "detail_local": "Lokal gezählt (5-h-Fenster): {parts} Tokens",
        "updated": "Zuletzt aktualisiert: {t} ({src})",
        "src_live": "live",
        "src_local": "lokal",
        "tooltip_wait": "Clawd wartet auf die ersten Daten …",
        "tooltip_api": "Claude-Nutzung (5-h-Fenster): {p}",
        "tooltip_est": "Claude-Nutzung (Schätzung): {p}  ({n} Tokens verbraucht)",
        "tray_title": "Clawd – Claude Code Nutzung",
        "tray_tooltip": "Clawd – {p}  ({n} Tokens verbraucht)",
        "bubble_done": "Fertig! Wartet auf dich.",
        "bubble_input": "Claude wartet auf deine Eingabe!",
        "bubble_session": "Neue Claude-Session gestartet",
        "menu_refresh": "Jetzt aktualisieren",
        "menu_panel": "Panel öffnen/schließen",
        "menu_quiet_on": "Sprechblasen einblenden",
        "menu_quiet_off": "Sprechblasen ausblenden",
        "menu_hooks_on": "Echtzeit-Hooks aktivieren (Beta)",
        "menu_hooks_off": "Echtzeit-Hooks deaktivieren",
        "menu_cal": "Limit kalibrieren …",
        "menu_cal_reset": "Kalibrierung zurücksetzen",
        "menu_clawd_login": "Clawd-Login einrichten …",
        "clawd_login_title": "Clawd-Login",
        "clawd_login_prompt": "1. Im Browser einloggen und „Authorize“ klicken.\n2. Den angezeigten Code (xxx#yyy) hier einfügen:",
        "clawd_login_ok": "Clawd-Login eingerichtet — Live-Werte sind aktiv.",
        "clawd_login_fail": "Login fehlgeschlagen: {e}",
        "clawd_login_nocode": "Kein Code eingegeben.",
        "menu_lang": "Language: English",
        "menu_show": "Clawd anzeigen/verstecken",
        "menu_quit": "Beenden",
        "cal_api_title": "Kalibrierung nicht nötig",
        "cal_api_text": "Die App bezieht gerade die echten Prozentwerte direkt "
                        "von Anthropic. Eine Kalibrierung ändert daran nichts.",
        "cal_nodata_title": "Keine Daten",
        "cal_nodata_text": "Im aktuellen 5-Stunden-Fenster wurden keine Tokens "
                           "gezählt.\nNutze Claude Code kurz und versuche es erneut.",
        "cal_prompt_title": "Limit kalibrieren",
        "cal_prompt_text": "Öffne in Claude Code das Nutzungs-Popup (Befehl /usage).\n"
                           "Welchen Prozentwert zeigt dort das 5-Stunden-Limit?\n\n"
                           "Clawd hat im selben Fenster {n} Tokens gezählt.",
        "cal_done_title": "Kalibriert",
        "cal_done_text": "Dein 5-Stunden-Kontingent liegt bei etwa {n} Tokens.\n"
                         "Clawds Anzeige entspricht ab jetzt Claudes eigener.",
        "hooks_py_title": "Python benötigt",
        "hooks_py_text": "Für Echtzeit-Hooks wird eine Python-Installation benötigt\n"
                         "(pythonw/py im PATH). Der Log-Watcher läuft trotzdem weiter.",
        "hooks_on_title": "Hooks aktiviert",
        "hooks_on_text": "Clawd reagiert ab der nächsten Claude-Code-Session sofort\n"
                         "auf Ereignisse — inklusive „Claude wartet auf deine "
                         "Eingabe“.\n\nBackup der Einstellungen: {f}",
        "err_dir": "Log-Verzeichnis nicht gefunden: {p}",
        "err_logs": "Logs nicht lesbar: {e}",
        "err_scan": "Scan-Fehler: {e}",
        "menu_notify_on": "Benachrichtigungen aktivieren",
        "menu_notify_off": "Benachrichtigungen deaktivieren",
        "menu_autostart": "Mit Windows starten",
        "forecast_eta": "Bei diesem Tempo: Limit ca. {t} Uhr",
        "forecast_ok": "Tempo reicht bis zum Reset ✓",
        "notify_warn80_title": "Clawd wird nervös",
        "notify_warn80_text": "80 % des 5-Stunden-Limits sind verbraucht.",
        "notify_warn95_title": "Clawd ist in Panik!",
        "notify_warn95_text": "95 % verbraucht — gleich ist Schluss.",
        "notify_reset_title": "Budget wieder frisch!",
        "notify_reset_text": "Das 5-Stunden-Fenster wurde zurückgesetzt.",
        "single_title": "Clawd läuft bereits",
        "single_text": "Eine andere Clawd-Instanz läuft schon –\n"
                       "schau ins Tray oder auf deinen Desktop.",
        "history_title": "Verlauf (24 Std.)",
        "menu_check_updates": "Auf Updates prüfen",
        "menu_update": "⬇ Update {v} laden",
        "update_available": "Update {v} verfügbar!",
        "update_text": "Zum Herunterladen klicken.",
        "task_title": "Woran Clawd arbeitet",
        "task_project": "Projekt · {name}",
        "task_waiting": "Wartet auf dich",
        "task_quote": "„{s}“",
        "notify_done_title": "Claude ist fertig",
        "notify_done_text": "Dein Turn – Claude wartet auf dich.",
        "notify_input_title": "Claude braucht dich",
        "notify_input_text": "Claude wartet auf deine Eingabe.",
        "menu_sound_on": "Benachrichtigungston aktivieren",
        "menu_sound_off": "Benachrichtigungston deaktivieren",
    },
    "en": {
        "panel_title": "Plan usage limits · {plan}",
        "row_5h": "5-hour limit",
        "row_week_all": "Weekly · all models",
        "row_week_model": "Weekly · {name}",
        "tokens_n": "{n} tokens",
        "tokens_inout": "{n} tokens (in + out)",
        "rolling7": "≈ last 7 days",
        "reset_running": "Resetting …",
        "reset_in_hm": "Resets in {h} h {m:02d} min",
        "reset_in_m": "Resets in {m} min",
        "reset_at": "Resets {wd}, {t}",
        "weekdays": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
        "detail_used": "{n} tokens used (input + output) · {hint}",
        "hint_manual": "Limit calibrated",
        "hint_auto": "Limits auto-calibrated",
        "hint_placeholder": "Placeholder limit – calibrate via the tray menu",
        "detail_local": "Locally counted (5 h window): {parts} tokens",
        "updated": "Last updated: {t} ({src})",
        "src_live": "live",
        "src_local": "local",
        "tooltip_wait": "Clawd is waiting for first data …",
        "tooltip_api": "Claude usage (5 h window): {p}",
        "tooltip_est": "Claude usage (estimate): {p}  ({n} tokens used)",
        "tray_title": "Clawd – Claude Code usage",
        "tray_tooltip": "Clawd – {p}  ({n} tokens used)",
        "bubble_done": "Done! Waiting for you.",
        "bubble_input": "Claude is waiting for your input!",
        "bubble_session": "New Claude session started",
        "menu_refresh": "Refresh now",
        "menu_panel": "Toggle panel",
        "menu_quiet_on": "Show speech bubbles",
        "menu_quiet_off": "Hide speech bubbles",
        "menu_hooks_on": "Enable real-time hooks (beta)",
        "menu_hooks_off": "Disable real-time hooks",
        "menu_cal": "Calibrate limit …",
        "menu_cal_reset": "Reset calibration",
        "menu_clawd_login": "Set up Clawd login …",
        "clawd_login_title": "Clawd login",
        "clawd_login_prompt": "1. Sign in in the browser and click \"Authorize\".\n2. Paste the code shown (xxx#yyy) here:",
        "clawd_login_ok": "Clawd login set up — live values are active.",
        "clawd_login_fail": "Login failed: {e}",
        "clawd_login_nocode": "No code entered.",
        "menu_lang": "Sprache: Deutsch",
        "menu_show": "Show/hide Clawd",
        "menu_quit": "Quit",
        "cal_api_title": "No calibration needed",
        "cal_api_text": "The app is currently getting the exact percentages "
                        "straight from Anthropic. Calibration would not change that.",
        "cal_nodata_title": "No data",
        "cal_nodata_text": "No tokens were counted in the current 5-hour window.\n"
                           "Use Claude Code briefly and try again.",
        "cal_prompt_title": "Calibrate limit",
        "cal_prompt_text": "Open the usage popup in Claude Code (/usage command).\n"
                           "What percentage does the 5-hour limit show there?\n\n"
                           "Clawd counted {n} tokens in the same window.",
        "cal_done_title": "Calibrated",
        "cal_done_text": "Your 5-hour budget is roughly {n} tokens.\n"
                         "Clawd's display now matches Claude's own.",
        "hooks_py_title": "Python required",
        "hooks_py_text": "Real-time hooks need a Python installation\n"
                         "(pythonw/py on PATH). The log watcher keeps working anyway.",
        "hooks_on_title": "Hooks enabled",
        "hooks_on_text": "From the next Claude Code session on, Clawd reacts\n"
                         "instantly to events — including \"Claude is waiting for "
                         "your input\".\n\nSettings backup: {f}",
        "err_dir": "Log directory not found: {p}",
        "err_logs": "Logs unreadable: {e}",
        "err_scan": "Scan error: {e}",
        "menu_notify_on": "Enable notifications",
        "menu_notify_off": "Disable notifications",
        "menu_autostart": "Start with Windows",
        "forecast_eta": "At this pace: limit around {t}",
        "forecast_ok": "Current pace lasts until the reset ✓",
        "notify_warn80_title": "Clawd is getting nervous",
        "notify_warn80_text": "80 % of the 5-hour limit is used.",
        "notify_warn95_title": "Clawd is panicking!",
        "notify_warn95_text": "95 % used — almost out.",
        "notify_reset_title": "Fresh budget!",
        "notify_reset_text": "The 5-hour window has reset.",
        "single_title": "Clawd is already running",
        "single_text": "Another Clawd instance is already running –\n"
                       "check the tray or your desktop.",
        "history_title": "History (24 h)",
        "menu_check_updates": "Check for updates",
        "menu_update": "⬇ Get update {v}",
        "update_available": "Update {v} available!",
        "update_text": "Click to download.",
        "task_title": "What Clawd is working on",
        "task_project": "Project · {name}",
        "task_waiting": "Waiting for you",
        "task_quote": "“{s}”",
        "notify_done_title": "Claude is done",
        "notify_done_text": "Your turn — Claude is waiting for you.",
        "notify_input_title": "Claude needs you",
        "notify_input_text": "Claude is waiting for your input.",
        "menu_sound_on": "Enable notification sound",
        "menu_sound_off": "Disable notification sound",
    },
}


def tr(key: str, **kw):
    table = STRINGS.get(_LANG) or STRINGS["de"]
    s = table.get(key, STRINGS["de"].get(key, key))
    if isinstance(s, tuple):
        return s
    return s.format(**kw) if kw else s


def set_language(lang: str) -> None:
    global _LANG
    _LANG = lang if lang in STRINGS else "de"


def language() -> str:
    return _LANG


def fmt_de(n: int) -> str:
    """Locale-aware thousands separator: 1.234.567 (de) / 1,234,567 (en)."""
    s = f"{n:,}"
    return s.replace(",", ".") if _LANG == "de" else s


def fmt_pct_de(pct: float) -> str:
    s = f"{pct:.1f}"
    return (s.replace(".", ",") if _LANG == "de" else s) + " %"


def _fmt_dur(seconds: float) -> str:
    """Compact duration: 5 -> '0:05', 74 -> '1:14', 3661 -> '1:01:01'."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


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
#  Pet widget — the always-on-top mascot
# ======================================================================

_HEART_ROWS = ("0110110", "1111111", "1111111", "0111110", "0011100", "0001000")


class PetWidget(QWidget):
    ANIM_TICK_MS = 33          # ~30 fps; sprite timing comes from the GIF delays
    DRAG_THRESHOLD = 6
    MOOD_FADE_MS = 340         # cross-dissolve a mood change
    HEART_LIFE_MS = 1200       # petting hearts float up and fade this long
    REACT_MS = 1300            # how long the petting reaction animation plays

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
        self._title = QLabel(tr("panel_title", plan=PLAN_NAME))
        self._title.setObjectName("h1")
        header.addWidget(self._title, 1)
        lay.addLayout(header)
        lay.addWidget(self._divider())

        # ---- "what Clawd is working on" (current task) -----------------
        self._task_ctx = None
        self._work_since = None
        self._task_action = ""
        self.task_title = QLabel(tr("task_title"))
        self.task_title.setObjectName("note")
        lay.addWidget(self.task_title)
        self.task_project = QLabel("")
        self.task_project.setObjectName("sub")
        self.task_project.setWordWrap(True)
        lay.addWidget(self.task_project)
        self.task_prompt = QLabel("")
        self.task_prompt.setObjectName("sub")
        self.task_prompt.setWordWrap(True)
        self.task_prompt.setStyleSheet("font-style: italic;")
        lay.addWidget(self.task_prompt)
        self.task_activity = QLabel("")
        self.task_activity.setObjectName("rowlabel")
        self.task_activity.setWordWrap(True)
        lay.addWidget(self.task_activity)
        self.task_div = self._divider()
        lay.addWidget(self.task_div)
        self._task_widgets = [self.task_title, self.task_project,
                              self.task_prompt, self.task_activity, self.task_div]
        for w in self._task_widgets:
            w.setVisible(False)

        # ---- usage rows, created on demand from the live buckets --------
        self._rows = {}

        # ---- footer ------------------------------------------------------
        self._footer_div = self._divider()
        lay.addWidget(self._footer_div)
        self.detail_label = QLabel("")
        self.detail_label.setObjectName("sub")
        self.detail_label.setWordWrap(True)
        lay.addWidget(self.detail_label)
        self.forecast_label = QLabel("")
        self.forecast_label.setObjectName("sub")
        self.forecast_label.setWordWrap(True)
        lay.addWidget(self.forecast_label)
        self._history = []
        self.history_title = QLabel(tr("history_title"))
        self.history_title.setObjectName("note")
        self.history_title.setVisible(False)
        lay.addWidget(self.history_title)
        self.history_chart = HistoryChart()
        self.history_chart.setVisible(False)
        lay.addWidget(self.history_chart)
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

    def retranslate(self):
        self._title.setText(tr("panel_title", plan=PLAN_NAME))
        self.history_title.setText(tr("history_title"))
        self.task_title.setText(tr("task_title"))
        self.set_task(self._task_ctx, self._work_since)

    def set_history(self, series):
        self._history = list(series)

    def set_task(self, ctx, work_since=None):
        """Update the 'what Clawd is working on' section from a SessionContext.

        work_since is a time.monotonic() stamp of when the current working
        phase began (or None); the panel derives the live '· M:SS' turn timer
        from it and ticks it every second while visible.
        """
        self._task_ctx = ctx
        self._work_since = work_since
        if ctx is None or not (ctx.task or ctx.kind):
            self._task_action = ""       # else _refresh_countdown re-shows a stale line
            for w in self._task_widgets:
                w.setVisible(False)
            self._relayout()
            return
        self.task_project.setText(
            tr("task_project", name=ctx.project) if ctx.project else "")
        self.task_project.setVisible(bool(ctx.project))
        self.task_prompt.setText(tr("task_quote", s=ctx.task) if ctx.task else "")
        self.task_prompt.setVisible(bool(ctx.task))
        if ctx.kind == "working":
            line = tool_action(ctx.tool, ctx.detail)
            self._task_action = "⚙ " + line if line else "⚙"
        elif ctx.kind == "waiting":
            self._task_action = "✓ " + tr("task_waiting")
        else:
            self._task_action = ""
        self._render_task_activity()
        self.task_title.setVisible(True)
        self.task_div.setVisible(True)
        self._relayout()

    def _render_task_activity(self):
        """Compose the activity line with the live turn timer (no relayout, so
        the per-second tick never resizes or jitters the card)."""
        text = self._task_action
        ctx = self._task_ctx
        if (text and ctx is not None and ctx.kind == "working"
                and self._work_since is not None):
            text += " · " + _fmt_dur(time.monotonic() - self._work_since)
        self.task_activity.setText(text)
        self.task_activity.setVisible(bool(text))

    def _relayout(self):
        # rows and the task section are shown/hidden dynamically — force a full
        # re-layout before resizing, otherwise sizeHint() is stale and the card
        # gets squashed
        self._lay.invalidate()
        self._lay.activate()
        self.layout().invalidate()
        self.layout().activate()
        self.adjustSize()

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
                self.detail_label.setText(tr("detail_local", parts=parts))
            else:
                self.detail_label.setText("")
        else:
            row = self._ensure_row("estimate", tr("row_5h"))
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
                wtok = snap.by_model_weighted.get(name, 0.0)
                share = (wtok / budget * 100.0) if budget > 0 else 0.0
                self._animate_row(mrow, share)
                mrow["reset"].setText(tr("tokens_inout", n=fmt_de(tok)))
                keys.add(mkey)

            # weekly limits, estimated from the same logs
            wtail = ("  ·  " + _fmt_reset(snap.week_reset)
                     if snap.week_reset is not None else "  ·  " + tr("rolling7"))
            wk = self._ensure_row("week_all", tr("row_week_all"))
            wk["reset"].setText(tr("tokens_n", n=fmt_de(snap.week_total)) + wtail)
            wbudget = weekly_budget_all()
            if wbudget:
                self._animate_row(wk, snap.week_weighted / wbudget * 100.0)
            keys.add("week_all")
            for name, mb in weekly_model_budgets().items():
                tokens = snap.week_by_model.get(name, 0)
                wtok = snap.week_by_model_weighted.get(name, 0.0)
                mkey = f"week:{name}"
                mrow = self._ensure_row(mkey, tr("row_week_model", name=name))
                self._animate_row(mrow, wtok / mb * 100.0 if mb else 0.0)
                mrow["reset"].setText(tr("tokens_n", n=fmt_de(tokens)) + wtail)
                keys.add(mkey)
            self._show_only(keys)
            if not wbudget:
                # no learned weekly budget yet: show only the token count, not a
                # percentage bar that can never fill
                self._rows["week_all"]["pct"].setVisible(False)
                self._rows["week_all"]["bar"].setVisible(False)

            hint = (tr("hint_manual") if is_calibrated()
                    else tr("hint_auto") if auto_budget_active()
                    else tr("hint_placeholder"))
            self.detail_label.setText(
                tr("detail_used", n=fmt_de(snap.total), hint=hint))
        self.detail_label.setVisible(bool(self.detail_label.text()))
        self._update_forecast(snap)
        self.history_chart.set_series(self._history)
        self.history_title.setVisible(len(self._history) >= 2)
        if snap.updated_at:
            src = tr("src_live") if snap.source == "api" else tr("src_local")
            self.updated_label.setText(
                tr("updated", t=snap.updated_at.strftime("%H:%M:%S"), src=src))
        self._refresh_countdown()
        self._relayout()

    def _update_forecast(self, snap: UsageSnapshot):
        """Burn-rate line: projected time of hitting the 5-hour limit."""
        reset_at = None
        if snap.source == "api":
            for b in snap.buckets:
                if b.key == "five_hour":
                    reset_at = b.resets_at
        elif snap.oldest is not None:
            reset_at = snap.oldest + timedelta(hours=WINDOW_HOURS)
        eta = snap.burn_eta
        if eta is None or snap.error or snap.pct >= 100.0:
            self.forecast_label.setText("")
        elif reset_at is not None and eta >= reset_at:
            self.forecast_label.setText(tr("forecast_ok"))
        else:
            self.forecast_label.setText(
                tr("forecast_eta", t=eta.astimezone().strftime("%H:%M")))
        self.forecast_label.setVisible(bool(self.forecast_label.text()))

    def _refresh_countdown(self):
        self._render_task_activity()     # tick the turn timer while visible
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
        self._on_click = None

    def show_text(self, text: str, pet: QWidget, duration_ms: int = 4200,
                  on_click=None):
        self._text = text
        self._on_click = on_click
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

    def mousePressEvent(self, _event):
        cb = self._on_click
        self.hide()
        if cb:
            cb()

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
        set_language(str(self.settings.value("language", "de") or "de"))
        self.snapshot = UsageSnapshot()

        if self.settings.value("weight_version", 0, type=int) != WEIGHT_VERSION:
            # the cost model changed, so any stored calibration is in the wrong
            # scale — drop it so the user recalibrates once cleanly instead of
            # seeing a confidently-wrong number
            for _k in ("max_tokens", "auto_budget_5h", "weekly_budget_all",
                       "weekly_anchor", "weekly_budget_models"):
                self.settings.remove(_k)
            reset_auto_calibration()
            self.settings.setValue("weight_version", WEIGHT_VERSION)

        saved = self.settings.value("max_tokens")
        try:
            if saved and int(saved) > 0:
                set_max_tokens_override(int(saved))
        except (TypeError, ValueError):
            self.settings.remove("max_tokens")

        # restore auto-calibration learned from previous live API syncs
        try:
            anchor_raw = self.settings.value("weekly_anchor", "") or ""
            set_auto_calibration(
                budget_5h=int(self.settings.value("auto_budget_5h", 0) or 0) or None,
                weekly_anchor=_parse_iso_ts(anchor_raw) if anchor_raw else None,
                weekly_budget=int(self.settings.value("weekly_budget_all", 0) or 0) or None,
                weekly_model_budgets=json.loads(
                    self.settings.value("weekly_budget_models", "") or "{}") or None,
            )
        except (TypeError, ValueError):
            pass

        self.pet = PetWidget(self)
        self.panel = PanelWidget()
        self.panel.on_leave = self.schedule_panel_hide
        self.bubble = SpeechBubble()

        # real-time activity: log watcher (Stufe 1) + hook receiver (Stufe 2)
        self.quiet = self.settings.value("quiet", False, type=bool)
        self.notify_enabled = self.settings.value("notify", True, type=bool)
        self.notify_sound = self.settings.value("notify_sound", False, type=bool)
        # burn-rate history and last pct are kept PER SOURCE ("api"/"logs"):
        # the two modes report on different absolute scales, so cross-comparing
        # them would fake resets — but a transient api->logs->api fallback (an
        # expired OAuth token, a network blip) must not wipe either history, or
        # the forecast and the threshold toasts would go dark for minutes.
        self._burn_samples = {}          # source -> list[(utc time, pct)]
        self._prev_pct = {}              # source -> last pct seen
        self.history = HistoryStore()
        self.check_updates = self.settings.value(
            "check_updates", UPDATE_CHECK, type=bool)
        self._update_url = ""
        self._update_tag = ""
        self._update_thread: Optional[UpdateThread] = None
        self._last_toast_was_update = False   # gate messageClicked to the update toast
        self._newest_log: Optional[Path] = None
        self._last_activity = None
        self._session_ctx = None
        self._work_kind = None            # last log-derived activity kind
        self._work_started_mono = None    # start of the current working phase
        self._work_log = None             # which session log that phase belongs to
        self._last_alert_mono = 0.0       # rate-limit "your turn" alerts
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

        self._update_timer = QTimer()
        self._update_timer.setInterval(UPDATE_RECHECK_MS)
        self._update_timer.timeout.connect(self._begin_update_check)

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
        if self.check_updates:
            self._begin_update_check()
            self._update_timer.start()

    def quit(self):
        self.save_position()
        self._scan_timer.stop()
        self._activity_timer.stop()
        self._update_timer.stop()
        self._udp.close()
        thread = self._scan_thread
        if thread is not None and thread.isRunning():
            thread.requestInterruption()
            thread.wait(5000)   # destroying a running QThread aborts the process
        upd = self._update_thread
        if upd is not None and upd.isRunning():
            upd.wait(7000)   # must exceed UpdateThread's 6 s network timeout
        if self.tray:
            self.tray.hide()
        self.app.quit()

    # -------------------------------------------------- tray

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(make_app_icon(), self.app)
        self.tray.setToolTip(tr("tray_title"))
        self._tray_menu = self.build_menu(None)
        self.tray.setContextMenu(self._tray_menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.messageClicked.connect(self._on_toast_clicked)
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
        if self._update_url:
            act_update = QAction(tr("menu_update", v=self._update_tag), menu)
            fnt = act_update.font()
            fnt.setBold(True)
            act_update.setFont(fnt)
            act_update.triggered.connect(self._open_update)
            menu.addAction(act_update)
            menu.addSeparator()
        act_refresh = QAction(tr("menu_refresh"), menu)
        act_refresh.triggered.connect(self.refresh)
        menu.addAction(act_refresh)

        act_panel = QAction(tr("menu_panel"), menu)
        act_panel.triggered.connect(self.toggle_panel)
        menu.addAction(act_panel)

        menu.addSeparator()
        act_quiet = QAction(tr("menu_quiet_on") if self.quiet
                            else tr("menu_quiet_off"), menu)
        act_quiet.triggered.connect(self.toggle_quiet)
        menu.addAction(act_quiet)

        act_notify = QAction(tr("menu_notify_off") if self.notify_enabled
                             else tr("menu_notify_on"), menu)
        act_notify.triggered.connect(self.toggle_notify)
        menu.addAction(act_notify)

        act_sound = QAction(tr("menu_sound_off") if self.notify_sound
                            else tr("menu_sound_on"), menu)
        act_sound.triggered.connect(self.toggle_notify_sound)
        menu.addAction(act_sound)

        if hooks_registered(CLAUDE_SETTINGS_FILE):
            act_hooks = QAction(tr("menu_hooks_off"), menu)
            act_hooks.triggered.connect(self.disable_hooks)
        else:
            act_hooks = QAction(tr("menu_hooks_on"), menu)
            act_hooks.triggered.connect(self.enable_hooks)
        menu.addAction(act_hooks)

        act_cal = QAction(tr("menu_cal"), menu)
        act_cal.triggered.connect(self.calibrate)
        menu.addAction(act_cal)
        if is_calibrated():
            act_reset = QAction(tr("menu_cal_reset"), menu)
            act_reset.triggered.connect(self.reset_calibration)
            menu.addAction(act_reset)

        act_login = QAction(tr("menu_clawd_login"), menu)
        act_login.triggered.connect(self.setup_clawd_login)
        menu.addAction(act_login)

        act_lang = QAction(tr("menu_lang"), menu)
        act_lang.triggered.connect(self.toggle_language)
        menu.addAction(act_lang)

        if autostart_supported():
            act_auto = QAction(tr("menu_autostart"), menu)
            act_auto.setCheckable(True)
            act_auto.setChecked(autostart_enabled())
            act_auto.triggered.connect(self.toggle_autostart)
            menu.addAction(act_auto)

        act_upd = QAction(tr("menu_check_updates"), menu)
        act_upd.setCheckable(True)
        act_upd.setChecked(self.check_updates)
        act_upd.triggered.connect(self.toggle_update_check)
        menu.addAction(act_upd)
        menu.addSeparator()

        if self.tray is not None:   # without a tray there is no way to un-hide
            act_show = QAction(tr("menu_show"), menu)
            act_show.triggered.connect(self.toggle_pet_visible)
            menu.addAction(act_show)

        menu.addSeparator()
        act_quit = QAction(tr("menu_quit"), menu)
        act_quit.triggered.connect(self.quit)
        menu.addAction(act_quit)
        return menu

    def toggle_language(self):
        set_language("en" if language() == "de" else "de")
        self.settings.setValue("language", language())
        self._rebuild_tray_menu()
        self.panel.retranslate()
        self.pet.set_snapshot(self.snapshot)
        self.panel.update_snapshot(self.snapshot)
        if self.tray:
            self.tray.setToolTip(tr("tray_title"))
        self.refresh()      # re-derive API bucket labels in the new language

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
        if not snap.error:
            now = datetime.now(timezone.utc)
            samples = self._burn_samples.setdefault(snap.source, [])
            if samples and snap.pct < samples[-1][1] - 1.0:
                samples.clear()             # window reset — old rate is void
            samples.append((now, snap.pct))
            cutoff = now - timedelta(seconds=BURN_LOOKBACK_S)
            samples[:] = [s for s in samples if s[0] >= cutoff]
            snap.burn_eta = burn_eta(samples)
            self._notify_transition(snap.source, snap.pct)
            self.history.add(now, snap.pct)
        cal = auto_calibration()          # persist budgets learned from live syncs
        if cal["budget_5h"]:
            self.settings.setValue("auto_budget_5h", cal["budget_5h"])
        if cal["weekly_budget"]:
            self.settings.setValue("weekly_budget_all", cal["weekly_budget"])
        if cal["anchor"] is not None:
            self.settings.setValue("weekly_anchor", cal["anchor"].isoformat())
        if cal["models"]:
            self.settings.setValue("weekly_budget_models", json.dumps(cal["models"]))
        self.pet.set_snapshot(snap)
        self.panel.set_history(self.history.series())
        self.panel.update_snapshot(snap)
        if self.tray:
            if snap.error:
                self.tray.setToolTip(f"Clawd – {snap.error}")
            else:
                self.tray.setToolTip(tr("tray_tooltip", p=fmt_pct_de(snap.pct),
                                        n=fmt_de(snap.total)))
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
        ctx = read_session_context(self._newest_log) if self._newest_log else None
        self._session_ctx = ctx
        # turn timer + "your turn" alert, keyed to the session log so a switch
        # between concurrent sessions never fakes a turn-end or a cross-session
        # timer (self._newest_log is the newest log across ALL projects)
        kind = ctx.kind if ctx else None
        log = self._newest_log
        if kind == "working" and self._work_kind == "working" and log == self._work_log:
            pass                                    # same working phase continues
        elif kind == "working":
            self._work_started_mono = time.monotonic()   # a new working phase
            self._work_log = log
        else:
            if (self._work_kind == "working" and kind == "waiting"
                    and log == self._work_log):     # same session finished its turn
                self._alert_turn_done(time.monotonic()
                                      - (self._work_started_mono or time.monotonic()))
            self._work_started_mono = None
        self._work_kind = kind
        self.panel.set_task(ctx, self._work_started_mono)  # live task view + timer
        if time.monotonic() < self._hook_hold_until:
            return                       # live hook events drive the mood
        act = (ctx.kind, ctx.tool) if ctx and ctx.kind else None
        prev = self._last_activity
        self._last_activity = act
        self.pet.set_activity(act)
        if act == prev or self.quiet or not self.pet.isVisible():
            return
        if act and act[0] == "working" and act[1]:
            text = (tool_action(ctx.tool, ctx.detail) if ctx else None) \
                or tool_bubble(act[1])
            if text and (not prev or prev[0] != "working" or prev[1] != act[1]):
                self.bubble.show_text(text, self.pet)
        elif act and act[0] == "waiting" and prev and prev[0] == "working":
            self.bubble.show_text(tr("bubble_done"), self.pet)

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
            text = tool_bubble(event.get("tool_name"))
        elif name == "Notification":
            act = ("needs_input", None)
            text = tr("bubble_input")
            self._fire_alert(tr("notify_input_title"), tr("notify_input_text"))
        elif name in ("Stop", "TaskCompleted"):
            act = ("waiting", None)
        elif name == "PostToolUseFailure":
            act = ("error", None)
            QTimer.singleShot(5000, self._clear_error_state)
        elif name == "SessionStart":
            text = tr("bubble_session")
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

    def toggle_notify(self):
        self.notify_enabled = not self.notify_enabled
        self.settings.setValue("notify", self.notify_enabled)
        self._rebuild_tray_menu()

    def toggle_notify_sound(self):
        self.notify_sound = not self.notify_sound
        self.settings.setValue("notify_sound", self.notify_sound)
        self._rebuild_tray_menu()

    def _fire_alert(self, title: str, text: str):
        """A 'your turn' tray toast, rate-limited so near-simultaneous
        triggers (a hook and the log poll) do not double-fire."""
        now = time.monotonic()
        if (not self.notify_enabled or self.tray is None
                or now - self._last_alert_mono < ALERT_COOLDOWN_S):
            return
        self._last_alert_mono = now
        self._last_toast_was_update = False
        self.tray.showMessage(title, text, QSystemTrayIcon.Information, 7000)
        if self.notify_sound:
            QApplication.beep()

    def _alert_turn_done(self, elapsed: float):
        """Alert when a turn Claude actually spent time on has finished."""
        if elapsed >= WAIT_ALERT_MIN_S:
            self._fire_alert(tr("notify_done_title"), tr("notify_done_text"))

    def toggle_autostart(self):
        set_autostart(not autostart_enabled())
        self._rebuild_tray_menu()

    def toggle_update_check(self):
        self.check_updates = not self.check_updates
        self.settings.setValue("check_updates", self.check_updates)
        if self.check_updates:
            self._update_timer.start()
            self._begin_update_check()
        else:
            self._update_timer.stop()
        self._rebuild_tray_menu()

    # -------------------------------------------------- update check

    def _begin_update_check(self):
        if self._update_thread is not None and self._update_thread.isRunning():
            return
        self._update_thread = UpdateThread()
        self._update_thread.result.connect(self._on_update_result)
        self._update_thread.start()

    def _on_update_result(self, tag: str, url: str):
        if not tag or not version_is_newer(tag, APP_VERSION):
            return
        self._update_tag = tag
        self._update_url = url or RELEASES_URL
        self._rebuild_tray_menu()
        if self.pet.isVisible() and not self.quiet:
            self.bubble.show_text(tr("update_available", v=tag), self.pet, 8000,
                                  on_click=self._open_update)
        if self.tray:
            self._last_toast_was_update = True
            self.tray.showMessage(tr("update_available", v=tag),
                                  tr("update_text"),
                                  QSystemTrayIcon.Information, 8000)

    def _on_toast_clicked(self):
        if self._last_toast_was_update:
            self._open_update()

    def _open_update(self):
        if self._update_url:
            webbrowser.open(self._update_url)

    def _notify_transition(self, source: str, pct: float):
        """Fire a tray toast when a scan crosses 80/95 % or the window resets.

        The previous pct is tracked per source so a transient api<->logs
        fallback neither drops a threshold toast nor fakes a reset from the
        level difference between the two modes.
        """
        prev = self._prev_pct.get(source)
        self._prev_pct[source] = pct
        kind = notify_decision(prev, pct)
        if kind is None or self.tray is None or not self.notify_enabled:
            return
        icon = (QSystemTrayIcon.Information if kind == "reset"
                else QSystemTrayIcon.Warning)
        self._last_toast_was_update = False   # this balloon is not the update one
        self.tray.showMessage(tr(f"notify_{kind}_title"),
                              tr(f"notify_{kind}_text"), icon, 6000)

    def enable_hooks(self):
        command = hook_command()
        if not command:
            QMessageBox.warning(
                None, tr("hooks_py_title"), tr("hooks_py_text"))
            return
        if register_hooks(CLAUDE_SETTINGS_FILE, command):
            QMessageBox.information(
                None, tr("hooks_on_title"),
                tr("hooks_on_text", f=f"{CLAUDE_SETTINGS_FILE.name}.clawd-bak"))
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
                None, tr("cal_api_title"), tr("cal_api_text"))
            return
        if snap.total <= 0:
            QMessageBox.warning(
                None, tr("cal_nodata_title"), tr("cal_nodata_text"))
            return

        pct, ok = QInputDialog.getDouble(
            None, tr("cal_prompt_title"),
            tr("cal_prompt_text", n=fmt_de(snap.total)),
            value=65.0, min=0.5, max=100.0, decimals=1)
        if not ok:
            return

        budget = int(round(snap.weighted / (pct / 100.0)))
        self.settings.setValue("max_tokens", budget)
        set_max_tokens_override(budget)
        self._rebuild_tray_menu()
        QMessageBox.information(
            None, tr("cal_done_title"), tr("cal_done_text", n=fmt_de(budget)))
        self.refresh()

    def reset_calibration(self):
        self.settings.remove("max_tokens")
        set_max_tokens_override(None)
        self._rebuild_tray_menu()
        self.refresh()

    def setup_clawd_login(self):
        """One-time login that gives Clawd its OWN independent usage token, so
        live values keep working (auto-refreshed) without touching Claude Code's
        credential store. Opens the browser, then takes the pasted code."""
        import webbrowser
        url, verifier, redirect = _clawd_build_authorize_url()
        try:
            webbrowser.open(url)
        except Exception:
            pass
        raw, ok = QInputDialog.getText(
            None, tr("clawd_login_title"),
            tr("clawd_login_prompt") + "\n\n" + url)
        if not ok:
            return
        if not raw.strip():
            QMessageBox.warning(None, tr("clawd_login_title"), tr("clawd_login_nocode"))
            return
        try:
            _clawd_exchange_code(raw, verifier, redirect)
        except Exception as e:      # noqa: BLE001 — surface any failure to the user
            QMessageBox.warning(
                None, tr("clawd_login_title"), tr("clawd_login_fail", e=str(e)[:200]))
            return
        _api_cache["next"] = 0.0    # force an immediate live fetch with the new token
        self.refresh()
        QMessageBox.information(None, tr("clawd_login_title"), tr("clawd_login_ok"))

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
    for m in ("type", "read", "think", "notify", "pet", "annoyed",
              "juggle", "conduct", "sweep", "carry"):
        # only require a mood when its gif is actually present, so an older
        # sprites/ folder still runs (MOOD_FALLBACK covers the missing ones)
        if (SPRITE_DIR / SPRITE_FILES[m]).is_file():
            assert m in sprites.sprites, f"sprite {m!r} not loaded"
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
    expected = probe.weighted / 273_846 * 100.0
    assert abs(probe.pct - expected) < 0.01, "calibrated budget not used by scan"
    print(f"[selftest] calibration: 178.000 Tokens @ 65 % -> "
          f"{effective_max_tokens()} Tokens Budget; live pct now {probe.pct:.1f}%")
    set_max_tokens_override(None)
    assert not is_calibrated()

    # weekly window from a known anchor + budget-derived percentages
    set_auto_calibration(
        weekly_anchor=datetime(2026, 7, 15, 17, 0, tzinfo=timezone.utc),
        weekly_budget=1_000_000, weekly_model_budgets={"Fable": 500_000})
    ws, wr = _weekly_window(datetime(2026, 7, 11, 0, 0, tzinfo=timezone.utc))
    assert wr == datetime(2026, 7, 15, 17, 0, tzinfo=timezone.utc)
    assert ws == wr - timedelta(days=7)
    panel.update_snapshot(scan_usage())
    assert "week_all" in panel._rows and "week:Fable" in panel._rows
    print(f"[selftest] weekly window OK, week_total={panel._snap.week_total} "
          f"week_by_model={panel._snap.week_by_model}")

    # per-model cost weighting: pricier models count more against the plan
    assert model_cost("Opus") == 5.0 and model_cost("Fable") == 0.3
    assert model_cost("Sonnet") == 1.0 and model_cost("Made-Up-Model") == 1.0
    # regression: the placeholder budget must be on the cost-weighted scale, so
    # a typical heavy all-Opus 5h window does not read a false >100% "limit"
    typical_opus_weighted = 24_000_000        # ~ observed heavy all-Opus window
    assert typical_opus_weighted / MAX_TOKENS * 100 < 100, \
        "placeholder budget too small for cost-weighted scale -> false limit"

    # weekly row shows no empty progress bar when the weekly budget is unknown
    reset_auto_calibration()
    assert weekly_budget_all() is None
    nb = scan_usage()
    nb.week_total = max(nb.week_total, 500_000)
    panel.update_snapshot(nb)
    app.processEvents()
    assert "week_all" in panel._rows
    assert panel._rows["week_all"]["bar"].isHidden(), "empty weekly bar still shown"
    assert panel._rows["week_all"]["pct"].isHidden(), "dash percentage still shown"
    assert panel._rows["week_all"]["reset"].text(), "weekly token count missing"
    print("[selftest] model cost + weekly-bar-when-unbudgeted OK")

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
    pet.set_activity(("working", "Edit"))      # tool picks the animation
    assert pet.mood == "type"
    pet.set_activity(("working", "Read"))
    assert pet.mood == "read"
    pet.set_activity(("working", None))        # thinking (no tool)
    assert pet.mood == "think"
    pet.set_activity(("needs_input", None))    # Claude asks you
    assert pet.mood == "notify"
    pet.set_activity(("waiting", None))
    assert pet.mood == "happy"
    pet.set_pct(90)                      # quota alarm overrides activity
    assert pet.mood == "panic"
    pet.set_pct(100)
    pet.set_activity(("working", "Edit"))
    assert pet.mood == "limit"           # over-limit overrides the tool mood
    pet.set_pct(10)
    pet.set_activity(None)
    assert pet.mood == "chill"
    # petting reaction briefly overrides the mood, then reverts
    if "pet" in pet._sprites.sprites:
        pet._play_reaction()
        assert pet._react_active and pet.mood == "pet"
        pet.set_activity(("working", "Edit"))  # ignored while reacting
        assert pet.mood == "pet"
        pet._end_reaction()
        assert not pet._react_active and pet.mood == "type"
    # over-petting makes Clawd annoyed instead of doing a happy jump
    if "annoyed" in pet._sprites.sprites:
        pet.set_pct(10)
        pet.set_activity(None)
        pet._end_reaction()
        pet._pet_times = []
        for _ in range(PET_SPAM_COUNT):
            pet._play_reaction()
        assert pet.mood == "annoyed", "spam petting should annoy Clawd"
        pet._end_reaction()
    # a random idle flourish shows only while calm, and is dropped when busy
    if pet._idle_pool:
        pet.set_pct(10)
        pet.set_activity(None)
        pet._quota_mood = "chill"
        pet._idle_variant = pet._idle_pool[0]
        pet._update_mood()
        assert pet.mood == pet._idle_pool[0], "idle flourish not shown while calm"
        pet.set_activity(("working", "Read"))   # busy -> flourish ignored AND cleared
        assert pet.mood == "read"
        assert pet._idle_variant is None, "stale flourish not cleared on interruption"
        pet.set_activity(None)                  # back to calm -> plain chill, no resume
        assert pet.mood == "chill"
    print("[selftest] activity mood combination OK")

    # language toggle: strings and number formatting switch together
    assert language() == "de" and fmt_de(1234567) == "1.234.567"
    set_language("en")
    assert fmt_de(1234567) == "1,234,567"
    assert tr("row_week_all") == "Weekly · all models"
    assert tr("reset_in_hm", h=3, m=7) == "Resets in 3 h 07 min"
    assert tool_bubble("Bash") == "running commands …"
    set_language("de")
    assert tr("row_week_all") == "Wöchentlich · alle Modelle"
    print("[selftest] language toggle OK")

    assert not make_clawd_icon().isNull(), "tray icon failed"
    assert fmt_de(1234567) == "1.234.567"
    assert mood_for_pct(49.9) == "chill" and mood_for_pct(50) == "focus"
    assert mood_for_pct(80) == "panic" and mood_for_pct(100) == "limit"

    # burn-rate forecast: linear projection to 100 %
    b0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    m = timedelta(minutes=1)
    eta = burn_eta([(b0, 40.0), (b0 + 20 * m, 50.0)])
    assert eta == b0 + 120 * m, f"burn eta wrong: {eta}"   # 0.5 %/min -> +100 min
    assert burn_eta([(b0, 40.0), (b0 + 2 * m, 50.0)]) is None    # span too short
    assert burn_eta([(b0, 50.0), (b0 + 20 * m, 45.0)]) is None   # usage falling
    assert burn_eta([(b0, 50.0)]) is None
    now_utc = datetime.now(timezone.utc)
    snap_fc = UsageSnapshot(updated_at=datetime.now(), pct=50.0,
                            oldest=now_utc - timedelta(hours=1),
                            burn_eta=now_utc + timedelta(hours=1))
    panel.update_snapshot(snap_fc)
    assert panel.forecast_label.text(), "forecast line missing"
    snap_fc.burn_eta = now_utc + timedelta(hours=9)   # past the window reset
    panel.update_snapshot(snap_fc)
    assert panel.forecast_label.text() == tr("forecast_ok")
    print("[selftest] burn-rate forecast OK")

    # notifications: threshold crossings and window-reset detection
    assert notify_decision(None, 85.0) is None       # no toast right at startup
    assert notify_decision(75.0, 85.0) == "warn80"
    assert notify_decision(85.0, 96.0) == "warn95"
    assert notify_decision(79.0, 96.0) == "warn95"   # highest threshold wins
    assert notify_decision(81.0, 82.0) is None
    assert notify_decision(76.0, 3.0) == "reset"
    assert notify_decision(30.0, 3.0) is None
    for kind in ("warn80", "warn95", "reset"):
        assert tr(f"notify_{kind}_title") and tr(f"notify_{kind}_text")
    print("[selftest] notification decisions OK")

    # per-source state: a transient api<->logs fallback must not wipe the
    # other source's history (else the forecast blanks and toasts never fire)
    capp = ClawdApp(app, with_tray=False)
    capp._notify_transition("api", 78.0)
    assert capp._prev_pct.get("api") == 78.0
    capp._notify_transition("logs", 40.0)          # transient fallback blip
    assert capp._prev_pct.get("api") == 78.0       # api lineage untouched
    assert capp._prev_pct.get("logs") == 40.0
    # so on return to api the crossing is judged 78 -> 82, not None -> 82
    assert notify_decision(capp._prev_pct.get("api"), 82.0) == "warn80"
    capp._burn_samples.setdefault("api", []).append(
        (datetime.now(timezone.utc), 78.0))
    capp._notify_transition("logs", 41.0)
    assert capp._burn_samples.get("api"), "api burn history wiped by logs blip"
    print("[selftest] per-source burn/notify state OK")

    # autostart: command resolvable, registry read only (no write in a test)
    if autostart_supported():
        assert autostart_command(), "no autostart runner found"
        assert isinstance(autostart_enabled(), bool)
    print("[selftest] autostart OK")

    # update check: version parsing and strict-newer comparison
    assert parse_version("v1.2.0") == (1, 2, 0)
    assert parse_version("1.10") == (1, 10)
    assert version_is_newer("v1.2.0", "1.1.0")
    assert version_is_newer("v1.2", "1.1.9")
    assert version_is_newer("v1.10.0", "1.9.0")       # numeric, not lexical
    assert not version_is_newer("v1.1.0", "1.1.0")
    assert not version_is_newer("v1.0.0", "1.1.0")
    assert not version_is_newer("", "1.1.0")
    assert not version_is_newer("garbage", "1.1.0")
    # the toast-click handler only opens the browser for the update toast
    assert capp._last_toast_was_update is False
    print("[selftest] update version compare OK")

    # history store: throttled append, pruning, windowed series, reload
    with tempfile.TemporaryDirectory() as td:
        hp = Path(td) / "history.json"
        hs = HistoryStore(hp)
        hts0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        assert hs.add(hts0, 10.0) is True
        assert hs.add(hts0 + timedelta(seconds=30), 12.0) is False   # throttled
        assert hs.add(hts0 + timedelta(seconds=400), 20.0) is True
        hs._points.insert(0, (hts0 - timedelta(days=HISTORY_KEEP_DAYS + 1), 5.0))
        assert hs.add(hts0 + timedelta(seconds=800), 25.0) is True
        floor = hts0 + timedelta(seconds=800) - timedelta(days=HISTORY_KEEP_DAYS)
        assert all(p[0] >= floor for p in hs._points), "old point not pruned"
        hs2 = HistoryStore(hp)                       # reload from disk
        assert hs2._points and len(hs2._points) == len(hs._points)
        win = hs2.series(window_h=1, now=hts0 + timedelta(seconds=800))
        assert win and all(
            p[0] >= hts0 + timedelta(seconds=800) - timedelta(hours=1)
            for p in win)
        # throttle survives a restart: reload restores _last_write from disk
        hs3 = HistoryStore(hp)
        newest = max(p[0] for p in hs3._points)
        assert hs3.add(newest + timedelta(seconds=60), 99.0) is False
        assert hs3.add(newest + timedelta(seconds=400), 99.0) is True
    print("[selftest] history store OK")

    # history chart: renders standalone and shows up in the panel with data
    hseries = [(hts0 + timedelta(minutes=i * 5), 10.0 + i) for i in range(6)]
    chart = HistoryChart()
    chart.resize(320, 54)
    chart.set_series(hseries)
    assert not chart.grab().isNull(), "history chart render failed"
    chart.set_series(hseries[:1])
    assert not chart.isVisible() or chart.isHidden() is False
    panel.set_history(hseries)
    panel.update_snapshot(snap)
    app.processEvents()
    assert len(panel.history_chart._series) == 6, "panel chart series not applied"
    assert not panel.history_chart.isHidden(), "panel chart hidden with data"
    panel.set_history([])
    panel.update_snapshot(snap)
    assert panel.history_chart.isHidden(), "empty history chart still shown"
    print("[selftest] history chart OK")

    # session context: task + tool detail extracted from the tail
    assert tool_detail("Edit", {"file_path": "C:/x/clawd_pet.py"}) == "clawd_pet.py"
    assert tool_detail("Bash", {"command": "git push\nmore"}).startswith("git push")
    assert tool_detail("Grep", {"pattern": "def foo"}) == "def foo"
    assert tool_action("Edit", "foo.py") and "foo.py" in tool_action("Edit", "foo.py")
    assert _user_prompt_text("<command>/model</command>") == ""      # wrapper skipped
    assert _user_prompt_text("please refactor") == "please refactor"
    with tempfile.TemporaryDirectory() as td:
        slog = Path(td) / "s.jsonl"
        slog.write_text(
            json.dumps({"type": "user", "cwd": "C:/Users/x/Desktop/demo proj",
                        "message": {"content": "please refactor the parser"}}) + "\n"
            + json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit",
                 "input": {"file_path": "C:/a/foo.py"}}]}}) + "\n",
            encoding="utf-8")
        sctx = read_session_context(slog)
        assert sctx and sctx.kind == "working" and sctx.tool == "Edit"
        assert sctx.detail == "foo.py"
        assert sctx.task == "please refactor the parser"
        assert sctx.project == "demo proj"
        assert read_last_activity(slog) == ("working", "Edit")   # wrapper parity
    # a very long project name must wrap, not clip (fixed-width panel)
    long_ctx = SessionContext(
        kind="working", tool="Bash", detail="git push origin main",
        task=sctx.task,
        project="a-really-long-monorepo-folder-name-that-would-overflow-the-panel")
    panel.set_task(long_ctx)
    panel.move(-4000, -4000)
    panel.show()
    app.processEvents()
    for w in (panel.task_project, panel.task_prompt, panel.task_activity):
        if not w.text() or w.isHidden():
            continue
        need = w.fontMetrics().boundingRect(
            QRect(0, 0, w.width(), 10_000),
            Qt.TextWordWrap if w.wordWrap() else 0, w.text())
        assert need.width() <= w.width() + 1, f"task label clipped: {w.text()[:30]}"
    assert not panel.grab().isNull()
    panel.hide()
    panel.set_task(sctx)
    panel.update_snapshot(snap)
    app.processEvents()
    assert not panel.task_prompt.isHidden(), "task prompt hidden with data"
    assert "refactor" in panel.task_prompt.text()
    panel.set_task(None)
    panel.update_snapshot(snap)
    assert panel.task_title.isHidden(), "task section shown while idle"
    assert panel._task_action == "", "stale task action kept after idle"
    panel._render_task_activity()        # the 1 s countdown tick must not re-show it
    assert not panel.task_activity.text(), "orphaned activity line re-shown after idle"
    print("[selftest] session context / task view OK")

    # turn timer formatting + live rendering in the activity line
    assert _fmt_dur(5) == "0:05" and _fmt_dur(74) == "1:14"
    assert _fmt_dur(3661) == "1:01:01"
    work_ctx = SessionContext(kind="working", tool="Edit", detail="foo.py",
                              task="do the thing", project="demo")
    panel.set_task(work_ctx, work_since=time.monotonic() - 90)
    assert "· 1:" in panel.task_activity.text(), \
        f"turn timer missing: {panel.task_activity.text()!r}"
    panel.set_task(work_ctx, work_since=None)      # no timer without a start
    assert "·" not in panel.task_activity.text().split("foo.py")[-1]
    # the "your turn" alert is rate-limited and safe without a tray
    assert capp._work_kind is None and capp._last_alert_mono == 0.0
    assert isinstance(capp.notify_sound, bool)
    capp._fire_alert("t", "x")            # tray is None -> must not raise
    capp._alert_turn_done(5.0)            # below threshold -> no-op

    # cross-session guard: a newest-log switch from a working session to a
    # different waiting session must NOT fire a turn-done alert
    fired = []
    capp._alert_turn_done = lambda e: fired.append(e)
    with tempfile.TemporaryDirectory() as td:
        la, lb = Path(td) / "a.jsonl", Path(td) / "b.jsonl"
        la.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "x"}}]}}) + "\n",
            encoding="utf-8")
        lb.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "done"}]}}) + "\n", encoding="utf-8")
        capp._work_kind = None
        capp._work_started_mono = None
        capp._work_log = None
        capp._newest_log = la
        capp._check_activity()            # session A is working
        assert capp._work_kind == "working" and capp._work_log == la
        capp._newest_log = lb
        capp._check_activity()            # switch to waiting session B -> no alert
        assert not fired, "spurious cross-session turn-done alert"
        capp._work_kind = "working"
        capp._work_log = lb
        capp._work_started_mono = time.monotonic() - 30
        capp._check_activity()            # B's own working->waiting -> alert
        assert fired and fired[-1] >= 20, "same-session turn-end not detected"
    print("[selftest] turn timer + your-turn alert OK")

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

    # Single-instance guard: a second pet would fight over the hook UDP port
    # and keep the exe locked during updates. QLockFile stores the owner PID,
    # so a lock left behind by a crash is detected as stale and removed.
    set_language(str(QSettings(ORG_NAME, APP_NAME).value("language", "de") or "de"))
    lock = QLockFile(str(Path(tempfile.gettempdir()) / "clawd_pet.lock"))
    lock.setStaleLockTime(0)               # the pet runs for days — never age out
    if not lock.tryLock(100):
        QMessageBox.information(None, tr("single_title"), tr("single_text"))
        return 0

    controller = ClawdApp(app)
    controller.start()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
