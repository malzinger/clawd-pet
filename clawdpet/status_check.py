"""Anthropic service-status check — the pet gets "sick" during incidents.

Statuspage-standard endpoint; polled rarely (the status page is not an
API to hammer) and always fail-open: any error means "assume healthy",
the pet must never look sick because the user's network blipped.
"""
import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

STATUS_URL = "https://status.anthropic.com/api/v2/status.json"
STATUS_INTERVAL_S = 600.0            # poll at most every 10 minutes
_SICK_LEVELS = ("major", "critical")

_cache = {"indicator": "none", "ts": 0.0}


def fetch_status_indicator() -> Optional[str]:
    """Raw Statuspage indicator: none|minor|major|critical, None on error."""
    if os.environ.get("CLAWD_NO_API"):
        return None                  # offline/CI — fail-open, never "sick"
    req = urllib.request.Request(STATUS_URL, headers={
        "User-Agent": "ClawdPet/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    indicator = (data.get("status") or {}).get("indicator")
    return indicator if isinstance(indicator, str) else None


def current_indicator(now: Optional[float] = None, fetch=None) -> str:
    """Throttled indicator, remembering the last good answer between polls."""
    now = time.time() if now is None else now
    if now - _cache["ts"] >= STATUS_INTERVAL_S:
        _cache["ts"] = now
        fresh = (fetch or fetch_status_indicator)()
        if fresh is not None:
            _cache["indicator"] = fresh
    return _cache["indicator"]


def anthropic_sick(now: Optional[float] = None, fetch=None) -> bool:
    """True while Anthropic reports a major/critical incident."""
    return current_indicator(now, fetch) in _SICK_LEVELS
