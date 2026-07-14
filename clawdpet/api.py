"""Live usage via the Anthropic OAuth API.

Token sources, in order: Clawd's OWN independent grant (~/.clawd/auth.json,
auto-refreshed — rotating it can never affect Claude Code's login), then the
token Claude Code stored (READ-ONLY), then the calibrated local estimate."""
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config import (
    API_MAX_BACKOFF_S,
    API_OK_INTERVAL_S,
    API_RETRY_S,
    CLAWD_AUTH_FILE,
    RATE_LIMIT_BASE_S,
    RATE_LIMIT_MAX_S,
    CREDENTIALS_FILE,
    OAUTH_CLIENT_ID,
    OAUTH_TOKEN_URL,
    REFRESH_COOLDOWN_S,
    USAGE_URL,
    USE_API_USAGE,
    WINDOW_HOURS,
)
from .i18n import tr
from .usage import (UsageSnapshot, _parse_iso_ts, auto_calibration,
                    scan_usage, set_auto_calibration)

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


def _store_clawd_auth(auth: dict, path: Path = CLAWD_AUTH_FILE) -> bool:
    """Atomically write Clawd's own token file, tightened to owner-only perms."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(auth), encoding="utf-8")
        os.replace(str(tmp), str(path))
        try:
            os.chmod(str(path), 0o600)   # no-op-ish on Windows, protects POSIX
        except OSError:
            pass
        return True
    except OSError:
        return False


def _refresh_clawd_token(auth: dict, path: Path = CLAWD_AUTH_FILE) -> Optional[str]:
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
    _store_clawd_auth(auth, path)                  # best-effort; own file, low stakes
    return new_at


def _clawd_own_token(path: Path = CLAWD_AUTH_FILE) -> Optional[str]:
    """Clawd's own independent access token, refreshing it if it has expired."""
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = auth.get("access_token")
    expires_ms = auth.get("expires_at") or 0
    if token and time.time() * 1000 < expires_ms - 60_000:
        return token
    return _refresh_clawd_token(auth, path)   # expired — safe self-refresh of our own token


def _claude_code_token() -> Optional[str]:
    """The token Claude Code itself stored, strictly READ-ONLY.

    Never refreshed and never written back — a failed write-back of Claude
    Code's rotating login could lock the user out of Claude Code. On
    Windows/Linux it lives in ~/.claude/.credentials.json; on macOS Claude
    Code keeps it in the login keychain instead, read via `security`."""
    creds = None
    try:
        creds = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if creds is None and sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["security", "find-generic-password",
                 "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=10)
            if proc.returncode == 0:
                creds = json.loads(proc.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            creds = None
    if not isinstance(creds, dict):
        return None
    oauth = creds.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    expires_ms = oauth.get("expiresAt") or 0
    if token and time.time() * 1000 < expires_ms - 60_000:
        return token
    return None


def _get_access_token() -> Optional[str]:
    """Prefer Clawd's own independent token (auto-refreshed); otherwise fall back
    to the token Claude Code stored, READ-ONLY (never refreshed or written, so a
    rotation we could not persist can never lock the user out of Claude Code)."""
    if os.environ.get("CLAWD_NO_API"):
        return None
    return _clawd_own_token() or _claude_code_token()


def clawd_build_authorize_url():
    """Build a fresh PKCE authorize URL for Clawd's own login.
    Returns (url, code_verifier, redirect_uri)."""
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


def clawd_exchange_code(raw_code: str, verifier: str, redirect_uri: str) -> None:
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


def force_live_refetch() -> None:
    """Drop the poll throttle so the next scan hits the usage endpoint at once
    (used right after a successful Clawd login)."""
    _api_cache["next"] = 0.0


# Why the last fetch failed — drives the back-off (429 gets a much longer,
# Retry-After-aware pause than a network blip) and the panel's status line.
_fetch_fail = {"kind": None, "retry_after": None}    # kind: no_token|429|http|net


def _parse_retry_after(err: "urllib.error.HTTPError") -> Optional[float]:
    """Seconds from a Retry-After header (delta or HTTP-date), clamped."""
    raw = (err.headers.get("Retry-After") or "").strip() if err.headers else ""
    if not raw:
        return None
    try:
        secs = float(raw)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            secs = (parsedate_to_datetime(raw)
                    - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            return None
    return max(0.0, min(secs, RATE_LIMIT_MAX_S))


def _failure_backoff(fails: int) -> float:
    """Pause before the next usage poll after `fails` consecutive failures.

    Being rate-limited means every retry is wasted AND prolongs the lockout,
    so 429 backs off much harder (Retry-After when sent, else 5 min doubling
    to 1 h) than an ordinary blip (5 s doubling to 2 min)."""
    if _fetch_fail["kind"] == "429":
        if _fetch_fail["retry_after"]:
            return max(_fetch_fail["retry_after"], RATE_LIMIT_BASE_S)
        return min(RATE_LIMIT_MAX_S, RATE_LIMIT_BASE_S * 2 ** min(fails - 1, 4))
    return min(API_MAX_BACKOFF_S, API_RETRY_S * 2 ** (fails - 1))


_source_pause = {}     # token source name -> time.time() until it rests (429)


def _fetch_usage_with(token: str):
    """One usage request. Returns (data, err_kind, retry_after_s)."""
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "ClawdPet/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        return None, ("429" if e.code == 429 else "http"), _parse_retry_after(e)
    except (urllib.error.URLError, OSError, ValueError):
        return None, "net", None
    return (data if isinstance(data, dict) else None), None, None


def fetch_api_usage() -> Optional[list]:
    """Real utilization buckets straight from Anthropic, or None on failure.

    The endpoint rate-limits PER TOKEN (verified live: Clawd's own token was
    locked out for an hour while Claude Code's token answered fine), so both
    tokens are candidates: Clawd's own grant first, then Claude Code's token
    read-only. A 429 pauses only that token; the other one carries on.
    """
    if os.environ.get("CLAWD_NO_API"):
        _fetch_fail.update(kind="no_token", retry_after=None)
        return None
    candidates = []
    own = _clawd_own_token()
    if own:
        candidates.append(("own", own))
    cc = _claude_code_token()
    if cc and cc != own:
        candidates.append(("claude-code", cc))
    if not candidates:
        _fetch_fail.update(kind="no_token", retry_after=None)
        return None
    now = time.time()
    last_kind, last_ra = None, None
    for source, token in candidates:
        if now < _source_pause.get(source, 0.0):
            if last_kind is None:      # every skipped source counts as limited
                last_kind = "429"
                last_ra = _source_pause[source] - now
            continue
        data, kind, ra = _fetch_usage_with(token)
        if data is not None:
            _fetch_fail.update(kind=None, retry_after=None)
            return _parse_usage_buckets(data)
        last_kind, last_ra = kind, ra
        if kind == "429":
            _source_pause[source] = now + max(ra or 0.0, RATE_LIMIT_BASE_S)
            continue                   # this token rests; the next one may work
        break                          # network/server error — rotation won't help
    _fetch_fail.update(kind=last_kind or "net", retry_after=last_ra)
    return None


def _parse_usage_buckets(data: dict) -> Optional[list]:
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


# throttle + back-off + the projection base: the local counters at the moment
# of the last successful fetch. Between polls the display shows the LIVE
# percentages plus only the locally counted growth since that fetch — the local
# scale never sets the absolute level (gold standard: server values are the
# single source of truth, local logs only bridge the gap between two syncs).
_api_cache = {"buckets": None, "ts": 0.0, "next": 0.0, "fails": 0,
              "base": None, "boundary": None}


def _project_buckets(buckets, base, snap) -> list:
    """Live pct + locally counted delta since the fetch, per bucket.

    Deltas are scaled by the live-learned budgets and clamped to [live, 100];
    if a local counter shrank (window reset, log rewrite) the raw live value
    is kept — a projection must never show LESS than the last live truth."""
    cal = auto_calibration()
    out = []
    for b in buckets:
        pct = b.pct
        if (b.key == "five_hour" and cal["budget_5h"]
                and snap.weighted >= base["weighted"]):
            pct += (snap.weighted - base["weighted"]) / cal["budget_5h"] * 100.0
        elif (b.key == "seven_day" and cal["weekly_budget"]
                and snap.week_weighted >= base["week_weighted"]):
            pct += ((snap.week_weighted - base["week_weighted"])
                    / cal["weekly_budget"] * 100.0)
        elif b.key.startswith("weekly_"):
            name = b.label.split("·")[-1].strip()
            bud = cal["models"].get(name)
            prev = base["week_model"].get(name, 0.0)
            cur = snap.week_by_model_weighted.get(name, 0.0)
            if bud and cur >= prev:
                pct += (cur - prev) / bud * 100.0
        out.append(UsageBucket(key=b.key, label=b.label,
                               pct=min(100.0, max(b.pct, pct)),
                               resets_at=b.resets_at))
    return out


def collect_usage(should_stop=None) -> UsageSnapshot:
    """API first (exact numbers), local log estimate as fallback. The usage
    endpoint is polled at most every API_OK_INTERVAL_S; between polls the last
    buckets are reused so the log scan can run every couple of seconds without
    hammering Anthropic. Between polls the shown percentages are the last live
    values plus the locally counted delta (see _project_buckets); repeated
    failures back off exponentially and only a persistently dead sync (>= 3
    fails over two intervals) drops the panel to the pure local estimate."""
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
                _api_cache["next"] = now_s + _failure_backoff(_api_cache["fails"])
                # a single blip must NOT flip the panel to the local absolute
                # scale — the last live reading (plus projection) stays up
                # until the sync has failed repeatedly for two whole intervals
                if (_api_cache["buckets"] is not None
                        and _api_cache["fails"] >= 3
                        and now_s - _api_cache["ts"] > 2 * API_OK_INTERVAL_S):
                    _api_cache["buckets"] = None
                    _api_cache["base"] = None
        buckets = _api_cache["buckets"]
        if buckets:
            # keep the local per-model token counts as extra detail
            snap = scan_usage(should_stop=should_stop)
            if snap.error:
                snap = UsageSnapshot(updated_at=datetime.now())
            snap.error = ""
            snap.source = "api"
            if not fetched_ok and _api_cache["base"] is not None:
                shown = _project_buckets(buckets, _api_cache["base"], snap)
            else:
                shown = buckets
            snap.buckets = shown
            snap.live_fetched_at = datetime.fromtimestamp(_api_cache["ts"])
            five = next((b for b in shown if b.key == "five_hour"), shown[0])
            snap.pct = five.pct
            # the live window just rolled over -> get fresh numbers right away
            # (once per boundary; a failing repoll keeps its normal back-off)
            live_five = next((b for b in buckets if b.key == "five_hour"), None)
            if (live_five is not None and live_five.resets_at is not None
                    and datetime.now(timezone.utc) >= live_five.resets_at
                    and _api_cache["boundary"] != live_five.resets_at):
                _api_cache["boundary"] = live_five.resets_at
                _api_cache["next"] = 0.0

            # auto-calibrate only from a *freshly fetched* reading: pairing a
            # cached (stale) percentage with the still-growing local token count
            # would slowly skew the learned budget.
            if fetched_ok:
                _calibrate_from_buckets(snap, buckets)
                _api_cache["base"] = {
                    "weighted": snap.weighted,
                    "week_weighted": snap.week_weighted,
                    "week_model": dict(snap.week_by_model_weighted),
                }
            snap.live_state, snap.live_until = "live", None
            return snap
    snap = scan_usage(should_stop=should_stop)
    snap.live_state, snap.live_until = live_status()
    return snap


def live_status():
    """Why there are no live numbers right now: ('rate_limited', until) when
    Anthropic sent a 429, ('no_token', None) without any login, ('error',
    None) after other failures, ('live'/'off', None) otherwise."""
    if not USE_API_USAGE:
        return "off", None
    if _api_cache["buckets"] is not None:
        return "live", None
    kind = _fetch_fail["kind"]
    if kind == "429":
        return "rate_limited", datetime.fromtimestamp(_api_cache["next"])
    if kind == "no_token":
        return "no_token", None
    if kind in ("http", "net"):
        return "error", None
    return "off", None


def _windows_aligned(local_end, live_end, tolerance_s: float) -> bool:
    if local_end is None or live_end is None:
        return False
    return abs((local_end - live_end).total_seconds()) <= tolerance_s


def _calibrate_from_buckets(snap, buckets) -> None:
    """Learn budgets + window boundaries from one fresh live reading.

    The reset boundaries (5h and weekly) are always stored — they re-anchor
    the local window replay. The budget RATIOS are only trusted when the
    locally counted window ends where the live one does: pairing a rolling/
    drifted local window with a fixed live percentage once seeded budgets
    that were off by 2x (observed live: panel 24 % vs claude.ai 39 %). On
    the first fetch after a drift only the anchors are stored; the very
    next scan counts aligned windows and then the ratios calibrate too.
    """
    budget_5h = weekly_budget = anchor = session_reset = None
    model_budgets = {}
    window = timedelta(hours=WINDOW_HOURS)
    local_5h_end = snap.oldest + window if snap.oldest is not None else None
    for b in buckets:
        if b.key == "five_hour" and b.resets_at is not None:
            session_reset = b.resets_at
        if b.key == "seven_day" and b.resets_at is not None:
            anchor = b.resets_at
        if b.pct < 3.0:
            continue              # too close to zero to divide reliably
        if b.key == "five_hour" and snap.weighted > 0:
            if _windows_aligned(local_5h_end, b.resets_at, 600):
                budget_5h = round(snap.weighted / (b.pct / 100.0))
        elif b.key == "seven_day" and snap.week_weighted > 0:
            if _windows_aligned(snap.week_reset, b.resets_at, 3600):
                weekly_budget = round(snap.week_weighted / (b.pct / 100.0))
        elif b.key.startswith("weekly_"):
            name = b.label.split("·")[-1].strip()
            wtok = snap.week_by_model_weighted.get(name, 0.0)
            if wtok > 0 and _windows_aligned(snap.week_reset, b.resets_at, 3600):
                model_budgets[name] = round(wtok / (b.pct / 100.0))
    set_auto_calibration(budget_5h, anchor, weekly_budget,
                         model_budgets or None, session_reset=session_reset)


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
