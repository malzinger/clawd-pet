"""Autostart via the Windows Run key (tray-menu toggle)."""
import shutil
import sys
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QSettings

from .config import AUTOSTART_REG_NAME, AUTOSTART_REG_PATH

def autostart_supported() -> bool:
    return sys.platform == "win32"


def autostart_command() -> Optional[str]:
    """Command line the Windows Run key should launch at login."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    # launch the clawd_pet.py entry point one level above this package
    script = Path(__file__).resolve().parent.parent / "clawd_pet.py"
    if not script.is_file():
        return None
    for runner in ("pythonw", "pyw", "python", "py"):
        exe = shutil.which(runner)
        if exe:
            return f'"{exe}" "{script}"'
    return None


def autostart_enabled() -> bool:
    if not autostart_supported():
        return False
    reg = QSettings(AUTOSTART_REG_PATH, QSettings.NativeFormat)
    return bool(reg.value(AUTOSTART_REG_NAME))


def set_autostart(enabled: bool) -> bool:
    if not autostart_supported():
        return False
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
