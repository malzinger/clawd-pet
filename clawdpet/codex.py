"""Codex CLI integration (X1): real rate limits + turn-complete notify.

Rate limits come from `codex app-server` (stdio JSON-RPC): initialize,
then account/rateLimits/read — the same local channel CodexBar uses, no
network code of our own and no Codex credentials ever touched. The call
spawns a short-lived subprocess, so it is polled rarely, cached, and must
only ever run OFF the GUI thread (the scan thread does).

Turn-complete alerts use Codex's documented `notify` hook: config.toml
gets `notify = [<python>, <codex_notify.py>]`, and the script forwards
the event to the pet over the authenticated UDP protocol. Registration
follows the same never-clobber rule as the Claude statusline: a foreign
notify entry is left alone and the caller reports "please remove it".
"""
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .api import UsageBucket
from .config import (
    CODEX_CONFIG_FILE,
    CODEX_POLL_INTERVAL_S,
    CODEX_RETRY_S,
    CODEX_RPC_TIMEOUT_S,
)
from .i18n import tr

NOTIFY_MARKER = "codex_notify.py"
_NOTIFY_RE = re.compile(r"^\s*notify\s*=", re.MULTILINE)


def codex_available() -> bool:
    return shutil.which("codex") is not None


# ---------------------------------------------------------------- rate limits

def _window_bucket(win, which: str) -> Optional[UsageBucket]:
    """UsageBucket from one RateLimitWindow {usedPercent, resetsAt,
    windowDurationMins} — labeled by its actual window length."""
    if not isinstance(win, dict):
        return None
    pct = win.get("usedPercent")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    resets = win.get("resetsAt")
    resets_at = None
    if isinstance(resets, (int, float)) and resets > 0:
        try:
            resets_at = datetime.fromtimestamp(float(resets), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            resets_at = None
    mins = win.get("windowDurationMins")
    if isinstance(mins, (int, float)) and mins >= 7 * 24 * 60:
        label = tr("row_codex_week")
    elif isinstance(mins, (int, float)) and mins > 0:
        hours = int(round(mins / 60.0))
        label = tr("row_codex_hours", h=max(1, hours))
    else:
        label = tr("row_codex_week") if which == "primary" else "Codex"
    return UsageBucket(key=f"codex_{which}", label=label,
                       pct=float(pct), resets_at=resets_at)


def _parse_rate_limits(result) -> Optional[list]:
    """Buckets from an account/rateLimits/read result (pure, testable)."""
    if not isinstance(result, dict):
        return None
    snap = result.get("rateLimits")
    if not isinstance(snap, dict):
        return None
    buckets = []
    for which in ("primary", "secondary"):
        b = _window_bucket(snap.get(which), which)
        if b is not None:
            buckets.append(b)
    return buckets or None


def fetch_codex_rate_limits(timeout: float = CODEX_RPC_TIMEOUT_S) -> Optional[list]:
    """Live Codex buckets via `codex app-server`, or None. BLOCKING (up to
    `timeout` seconds) — call from a worker thread only."""
    if not codex_available() or os.environ.get("CLAWD_NO_API"):
        return None
    try:
        proc = subprocess.Popen(
            ["codex", "app-server"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace")
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    try:
        def send(obj):
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "clawd-pet",
                                        "version": "1.8.0"}}})
        sent_read = False
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") == 1 and not sent_read:
                sent_read = True     # handshake done -> ask for the limits
                send({"jsonrpc": "2.0", "method": "initialized"})
                send({"jsonrpc": "2.0", "id": 2,
                      "method": "account/rateLimits/read", "params": {}})
            elif msg.get("id") == 2:
                return _parse_rate_limits(msg.get("result"))
        return None
    except (OSError, ValueError):
        return None
    finally:
        try:
            proc.kill()
        except OSError:
            pass


_codex_cache = {"buckets": None, "next": 0.0, "fails": 0}


def codex_usage() -> Optional[list]:
    """Throttled fetch — a subprocess spawn is heavyweight, so at most every
    CODEX_POLL_INTERVAL_S, with a shorter retry after a failure. The last
    good reading survives two blips, then the panel drops the Codex line."""
    now = time.monotonic()
    if now >= _codex_cache["next"]:
        fresh = fetch_codex_rate_limits()
        if fresh:
            _codex_cache.update(buckets=fresh, fails=0,
                                next=now + CODEX_POLL_INTERVAL_S)
        else:
            _codex_cache["fails"] += 1
            _codex_cache["next"] = now + CODEX_RETRY_S
            if _codex_cache["fails"] >= 3:
                _codex_cache["buckets"] = None
    return _codex_cache["buckets"]


# ---------------------------------------------------------------- notify hook

def notify_command(runner: str, script_path: Path) -> str:
    """The TOML line registering our notify program."""
    return ('notify = [{}, {}]  # clawd-pet turn alerts'
            .format(json.dumps(runner), json.dumps(str(script_path))))


def notify_registered(config_path: Path = CODEX_CONFIG_FILE) -> bool:
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return False
    for ln in text.splitlines():
        if _NOTIFY_RE.match(ln):
            return NOTIFY_MARKER in ln    # first notify line decides
    return False


def register_notify(line: str, config_path: Path = CODEX_CONFIG_FILE) -> bool:
    """Append our notify line; False if any notify already exists.

    A foreign `notify` entry is never replaced (the user configured it) —
    the caller detects that case via notify_registered() and explains it."""
    try:
        text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    except OSError:
        return False
    if _NOTIFY_RE.search(text):
        return False                 # ours or foreign — either way, done/refuse
    try:
        if config_path.exists():
            shutil.copy2(config_path,
                         config_path.with_suffix(".toml.clawd-bak"))
        body = text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"
        tmp = config_path.with_suffix(".toml.tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(config_path)
        return True
    except OSError:
        return False


def unregister_notify(config_path: Path = CODEX_CONFIG_FILE) -> bool:
    """Remove exactly our notify line, never a user-written one."""
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return False
    kept = [ln for ln in text.splitlines()
            if not (_NOTIFY_RE.match(ln) and NOTIFY_MARKER in ln)]
    if len(kept) == len(text.splitlines()):
        return False
    try:
        tmp = config_path.with_suffix(".toml.tmp")
        tmp.write_text("\n".join(kept) + ("\n" if text.endswith("\n") else ""),
                       encoding="utf-8")
        tmp.replace(config_path)
        return True
    except OSError:
        return False
