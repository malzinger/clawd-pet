"""Usage scanning and calibration (pure logic, no Qt) — worker-thread safe."""
import bisect
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config import (
    BURN_MIN_SPAN_S,
    CLAUDE_PROJECTS_DIR,
    MAX_TOKENS,
    NOTIFY_THRESHOLDS,
    REPLAY_HOURS,
    WEEK_REPLAY_HOURS,
    WEIGHT_CACHE_CREATION,
    WEIGHT_CACHE_READ,
    WEIGHT_INPUT,
    WEIGHT_OUTPUT,
    WINDOW_HOURS,
    model_cost,
)
from .i18n import tr

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
