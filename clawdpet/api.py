"""Live usage via the Anthropic OAuth API — read-only, best effort."""
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .config import (
    API_MAX_BACKOFF_S,
    API_OK_INTERVAL_S,
    API_RETRY_S,
    API_STALE_S,
    CREDENTIALS_FILE,
    USAGE_URL,
    USE_API_USAGE,
)
from .i18n import tr
from .usage import UsageSnapshot, _parse_iso_ts, scan_usage, set_auto_calibration

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


def _get_access_token() -> Optional[str]:
    """The OAuth token Claude Code stored, but only while it is still valid.

    READ-ONLY on purpose: Clawd never refreshes the token and never writes the
    credential store. Anthropic rotates the refresh token on every use, so a
    passive monitor that refreshed it could consume a rotation it fails to
    persist (a contended file write, or a network timeout after the server
    already rotated) and lock the user out of Claude Code itself. When the token
    is expired we return None and fall back to the local, calibrated estimate."""
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
