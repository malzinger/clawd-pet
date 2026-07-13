"""Notification sounds (F9): tiny bundled WAVs played via QSoundEffect."""
from pathlib import Path

# The WAV assets live next to the package, one level above it — the same
# layout as SPRITE_DIR in config, which also holds inside a PyInstaller
# onefile bundle (_MEIPASS/clawdpet/sounds.py sits below the _MEIPASS/sounds
# payload). PyInstaller builds must ship them via:
#   --add-data "sounds;sounds"   (Windows; use "sounds:sounds" elsewhere)
SOUNDS_DIR = Path(__file__).resolve().parent.parent / "sounds"
_FILES = {"done": "done.wav", "attention": "attention.wav"}

# QSoundEffect instances are cached module-globally: a local effect would be
# garbage-collected the moment play() returns, cutting the sound short.
_effects = {}


def play(kind: str) -> bool:
    """Start the named notification sound; True when playback began.

    Offscreen/CI-safe: returns False — and never raises — when QtMultimedia
    is missing, the audio engine is unavailable or the file does not exist.
    The caller is expected to fall back to QApplication.beep() on False.
    """
    fname = _FILES.get(kind)
    if not fname:
        return False
    fp = SOUNDS_DIR / fname
    if not fp.is_file():
        return False
    try:
        effect = _effects.get(kind)
        if effect is None:
            try:
                from PyQt5.QtCore import QUrl
                from PyQt5.QtMultimedia import QSoundEffect
            except ImportError:
                return False
            effect = QSoundEffect()
            effect.setSource(QUrl.fromLocalFile(str(fp)))
            effect.setVolume(0.5)
            _effects[kind] = effect
        effect.play()
        return True
    except Exception:
        return False
