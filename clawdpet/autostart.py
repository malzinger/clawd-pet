"""Start-at-login toggle: Windows Run key / macOS LaunchAgent (tray menu)."""
import plistlib
import shutil
import sys
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QSettings

from .config import AUTOSTART_REG_NAME, AUTOSTART_REG_PATH

# macOS: a per-user LaunchAgent with RunAtLoad starts the pet at login.
# No launchctl call is made — the plist simply takes effect at the next login,
# which matches what the Windows Run key does.
LAUNCH_AGENT = (Path.home() / "Library" / "LaunchAgents"
                / "com.clawdpet.clawd.plist")


def autostart_supported() -> bool:
    return sys.platform in ("win32", "darwin")


def _autostart_args() -> Optional[list]:
    """argv that launches the pet at login, or None if unavailable."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    # launch the clawd_pet.py entry point one level above this package
    script = Path(__file__).resolve().parent.parent / "clawd_pet.py"
    if not script.is_file():
        return None
    runners = (("pythonw", "pyw", "python", "py") if sys.platform == "win32"
               else ("python3", "python"))
    for runner in runners:
        exe = shutil.which(runner)
        if exe:
            return [exe, str(script)]
    return None


def autostart_command() -> Optional[str]:
    """Quoted command line (stored in the Run key / shown in diagnostics)."""
    args = _autostart_args()
    if not args:
        return None
    return " ".join(f'"{a}"' for a in args)


def autostart_enabled() -> bool:
    if sys.platform == "win32":
        reg = QSettings(AUTOSTART_REG_PATH, QSettings.NativeFormat)
        return bool(reg.value(AUTOSTART_REG_NAME))
    if sys.platform == "darwin":
        return LAUNCH_AGENT.is_file()
    return False


def set_autostart(enabled: bool) -> bool:
    if sys.platform == "win32":
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
    if sys.platform == "darwin":
        return _set_autostart_darwin(enabled, LAUNCH_AGENT)
    return False


def _set_autostart_darwin(enabled: bool, plist_path: Path) -> bool:
    """Write or remove the LaunchAgent plist (separate for testability)."""
    if enabled:
        args = _autostart_args()
        if not args:
            return False
        try:
            plist_path.parent.mkdir(parents=True, exist_ok=True)
            with open(plist_path, "wb") as fh:
                plistlib.dump({
                    "Label": "com.clawdpet.clawd",
                    "ProgramArguments": args,
                    "RunAtLoad": True,
                }, fh)
            return True
        except OSError:
            return False
    try:
        plist_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True
