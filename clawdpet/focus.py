"""Bring the user's terminal to the front (best effort, macOS only for now).

Used when the user clicks a "Claude needs you" bubble: instead of hunting
for the right window, the frontmost known terminal app is activated. On
other platforms this is a silent no-op — the bubble still informs.
"""
import subprocess
import sys

# checked in order; Warp first because that is where Claude Code usually runs
TERMINAL_APPS = ("Warp", "iTerm2", "iTerm", "Terminal",
                 "Visual Studio Code", "Cursor")

_OSA_LIST = ('tell application "System Events" to get name of '
             '(processes where background only is false)')


def focus_terminal() -> bool:
    """Activate the first running known terminal app. True on success."""
    if sys.platform != "darwin":
        return False
    try:
        out = subprocess.run(["osascript", "-e", _OSA_LIST],
                             capture_output=True, text=True, timeout=3)
        running = {n.strip() for n in out.stdout.split(",")}
        for app in TERMINAL_APPS:
            if app in running:
                subprocess.run(
                    ["osascript", "-e", f'tell application "{app}" to activate'],
                    capture_output=True, timeout=3)
                return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False
