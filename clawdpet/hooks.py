"""Claude Code hook registration + datagram authentication (Stufe 2).

The hook sender lives in clawd_hook.py (repo root); registration is only
ever touched from the tray menu."""
import hmac
import json
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import Optional

from .config import CLAUDE_SETTINGS_FILE, HOOK_EVENTS, HOOK_TOKEN_FILE

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
        # clawd_hook.py lives next to the clawd_pet.py entry point, one
        # level above this package
        hook_py = Path(__file__).resolve().parent.parent / "clawd_hook.py"
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


def ensure_hook_token(path: Path = HOOK_TOKEN_FILE) -> str:
    """Shared secret authenticating hook datagrams, created on first use.

    The UDP receiver listens on 127.0.0.1, which any local process can reach;
    without a check, other software on the machine could spoof "Claude is
    waiting for your input" toasts or hold the mood override. clawd_hook.py
    reads this file and prefixes every datagram with the token; the pet only
    accepts datagrams carrying it. The file is chmod 0600 where supported."""
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    except OSError:
        pass
    token = secrets.token_hex(16)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass                    # unwritable home -> hooks stay silent, app works
    return token


def parse_hook_datagram(data: bytes, token: str) -> Optional[dict]:
    """Validate and decode one hook datagram: '<token>\\n<event json>'.

    Returns the event dict, or None for datagrams without the correct token
    (including the pre-token legacy format) or with broken JSON."""
    nl = data.find(b"\n")
    if nl <= 0:
        return None
    sent = data[:nl].decode("utf-8", errors="replace").strip()
    if not token or not hmac.compare_digest(sent, token):
        return None
    try:
        event = json.loads(data[nl + 1:].decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def refresh_hook_copy() -> None:
    """Keep the copied hook sender in sync with the running exe (frozen only).

    In frozen mode enable_hooks() copies clawd_hook.py to ~/.claude; after an
    app update that copy would lag behind (e.g. predate the datagram token and
    get silently dropped by the receiver). While hooks are registered, refresh
    the copy from the bundle on every startup. In source mode the script lives
    in the repo and updates together with the app — nothing to do."""
    if not getattr(sys, "frozen", False):
        return
    if not hooks_registered(CLAUDE_SETTINGS_FILE):
        return
    src = Path(getattr(sys, "_MEIPASS", "")) / "clawd_hook.py"
    dst = Path.home() / ".claude" / "clawd_hook.py"
    try:
        if src.is_file():
            shutil.copy2(src, dst)
    except OSError:
        pass                       # best effort — the log watcher still works
