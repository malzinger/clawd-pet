"""Claude Code statusline registration (context-window display, X2).

The sender lives in clawd_statusline.py (repo root); it is wired into
~/.claude/settings.json as the statusLine command. Unlike hooks (a list per
event), statusLine is a SINGLE slot — so registration must never clobber a
statusline the user configured themselves: register_statusline() refuses
(returns False) unless the slot is absent or already ours, and
unregister_statusline() removes only our own entry.
"""
import json
from pathlib import Path

from .hooks import _load_settings, _write_settings

STATUSLINE_MARKER = "clawd_statusline.py"


def _is_ours(entry) -> bool:
    return isinstance(entry, dict) and STATUSLINE_MARKER in json.dumps(entry)


def statusline_registered(settings_path: Path) -> bool:
    data = _load_settings(settings_path)
    if not isinstance(data, dict):
        return False
    return _is_ours(data.get("statusLine"))


def register_statusline(settings_path: Path, command: str) -> bool:
    """Install our statusline command; False when nothing was written.

    A False return with a foreign statusLine present means "refused" — the
    caller distinguishes that via statusline_registered() and tells the user
    to remove their own statusline first instead of silently replacing it.
    """
    data = _load_settings(settings_path)
    if not isinstance(data, dict):
        return False
    entry = data.get("statusLine")
    if entry is not None and not _is_ours(entry):
        return False                # the user's own statusline — never touch
    if _is_ours(entry) and entry.get("command") == command:
        return False                # already registered, nothing to do
    data["statusLine"] = {"type": "command", "command": command}
    return _write_settings(settings_path, data)


def unregister_statusline(settings_path: Path) -> bool:
    data = _load_settings(settings_path)
    if not isinstance(data, dict):
        return False
    if not _is_ours(data.get("statusLine")):
        return False                # absent or foreign — leave untouched
    del data["statusLine"]
    return _write_settings(settings_path, data)
