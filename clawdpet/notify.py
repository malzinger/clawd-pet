"""Native, non-activating desktop notifications.

Qt's QSystemTrayIcon.showMessage falls back to a Qt-drawn balloon window
when the process is not a bundled app (a plain `python clawd_pet.py` on
macOS) — and showing that balloon activates the app, so whatever window
the user was typing in loses focus on every toast. Notification Center
via `osascript` has no such side effect and honors the system's Do Not
Disturb / Focus settings on top.

Only macOS needs the detour; on Windows/Linux Qt uses the native
notification path already. Returns False whenever the caller should fall
back to tray.showMessage.
"""
import os
import subprocess
import sys


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def post_notification(title: str, text: str) -> bool:
    """Fire-and-forget native notification. True if it was handed off."""
    if sys.platform != "darwin" or os.environ.get("CLAWD_NO_NATIVE_NOTIFY"):
        return False
    script = ("display notification {} with title {}"
              .format(_applescript_str(text), _applescript_str(title)))
    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False
