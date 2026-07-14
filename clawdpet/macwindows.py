"""Frontmost-window geometry via the Quartz CGWindowList API (macOS only).

Pure logic, no Qt widgets: pet.py polls frontmost_window_frame() so Clawd can
sit on the title bar of the frontmost regular window (Shimeji style, opt-in).

Two backends, tried in this order:
  1. pyobjc's ``Quartz`` module, when installed.
  2. A minimal ctypes bridge to CoreGraphics/CoreFoundation. It only ever
     touches values behind CFGetTypeID checks, releases the one object it
     copies, and converts the bounds dict with the system's own
     CGRectMakeWithDictionaryRepresentation instead of hand-parsing it.

Notes:
- Window GEOMETRY needs no screen-recording permission — only reading window
  *titles* would trigger that prompt — so this works out of the box.
- Every call into either backend is guarded: this module must never crash
  the pet. When neither backend works, ``window_tracking_available()`` is
  False and the caller hides the menu entry (graceful degradation).
"""
import os
import sys
from typing import Optional, Tuple

from .config import WINDOW_SIT_MIN_H, WINDOW_SIT_MIN_W

try:
    import Quartz  # pyobjc (pyobjc-framework-Quartz)
except Exception:  # missing or broken installs both mean "try ctypes"
    Quartz = None

# CGWindow.h constants (stable public ABI values)
_OPT_ON_SCREEN_ONLY = 1 << 0        # kCGWindowListOptionOnScreenOnly
_OPT_EXCLUDE_DESKTOP = 1 << 4       # kCGWindowListExcludeDesktopElements
_NULL_WINDOW_ID = 0                 # kCGNullWindowID

_CT = None          # lazy ctypes backend: None = untried, False = unusable


def _ctypes_backend():
    """Build (once) a dict of ctypes handles for the CGWindowList calls.

    Returns None when unavailable; any failure is permanent for the process
    so a broken system never gets probed every poll tick.
    """
    global _CT
    if _CT is not None:
        return _CT or None
    if sys.platform != "darwin":
        _CT = False
        return None
    try:
        import ctypes
        import ctypes.util

        cf_path = ctypes.util.find_library("CoreFoundation")
        cg_path = ctypes.util.find_library("CoreGraphics")
        if not cf_path or not cg_path:
            raise OSError("CoreFoundation/CoreGraphics not found")
        cf = ctypes.CDLL(cf_path)
        cg = ctypes.CDLL(cg_path)

        class CGRect(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double),
                        ("w", ctypes.c_double), ("h", ctypes.c_double)]

        # explicit signatures: the c_int defaults would truncate pointers
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p,
                                                 ctypes.c_char_p,
                                                 ctypes.c_uint32]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_long,
                                        ctypes.c_void_p]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
        cf.CFNumberGetTypeID.restype = ctypes.c_ulong
        cf.CFDictionaryGetTypeID.restype = ctypes.c_ulong
        cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
        cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32,
                                                  ctypes.c_uint32]
        cg.CGRectMakeWithDictionaryRepresentation.restype = ctypes.c_bool
        cg.CGRectMakeWithDictionaryRepresentation.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(CGRect)]

        utf8 = 0x08000100                        # kCFStringEncodingUTF8
        # the public key constants equal their own names as CFStrings; the
        # keys live for the whole process, so they are created exactly once
        keys = {name: cf.CFStringCreateWithCString(
                    None, name.encode("ascii"), utf8)
                for name in ("kCGWindowLayer", "kCGWindowOwnerPID",
                             "kCGWindowAlpha", "kCGWindowBounds")}
        if not all(keys.values()):
            raise OSError("CFString key creation failed")

        _CT = {"ctypes": ctypes, "cf": cf, "cg": cg, "CGRect": CGRect,
               "keys": keys,
               "number_tid": cf.CFNumberGetTypeID(),
               "dict_tid": cf.CFDictionaryGetTypeID()}
    except Exception:
        _CT = False
        return None
    return _CT


def window_tracking_available() -> bool:
    """True when frontmost_window_frame() can possibly return frames."""
    if sys.platform != "darwin":
        return False
    return Quartz is not None or _ctypes_backend() is not None


def _frame_via_quartz() -> Optional[Tuple[int, int, int, int]]:
    infos = Quartz.CGWindowListCopyWindowInfo(
        _OPT_ON_SCREEN_ONLY | _OPT_EXCLUDE_DESKTOP, _NULL_WINDOW_ID)
    if not infos:
        return None
    own_pid = os.getpid()
    for info in infos:                   # front-to-back: first match wins
        try:
            if int(info.get("kCGWindowLayer", -1)) != 0:
                continue                 # menu bar / dock / overlay
            if int(info.get("kCGWindowOwnerPID", -1)) == own_pid:
                continue                 # our own pet / panel / bubble windows
            if float(info.get("kCGWindowAlpha", 1.0)) <= 0.0:
                continue                 # fully transparent
            bounds = info.get("kCGWindowBounds") or {}
            x = float(bounds.get("X", 0.0))
            y = float(bounds.get("Y", 0.0))
            w = float(bounds.get("Width", 0.0))
            h = float(bounds.get("Height", 0.0))
            if w < WINDOW_SIT_MIN_W or h < WINDOW_SIT_MIN_H:
                continue                 # palette / tooltip / status item
            return (int(x), int(y), int(w), int(h))
        except Exception:
            continue                     # malformed entry — try the next one
    return None


def _frame_via_ctypes() -> Optional[Tuple[int, int, int, int]]:
    be = _ctypes_backend()
    if be is None:
        return None
    ctypes, cf, cg = be["ctypes"], be["cf"], be["cg"]
    keys = be["keys"]

    def _num(dic, key, cf_type, ctype, default):
        val = cf.CFDictionaryGetValue(dic, keys[key])
        if not val or cf.CFGetTypeID(val) != be["number_tid"]:
            return default
        out = ctype()
        if not cf.CFNumberGetValue(val, cf_type, ctypes.byref(out)):
            return default
        return out.value

    infos = cg.CGWindowListCopyWindowInfo(
        _OPT_ON_SCREEN_ONLY | _OPT_EXCLUDE_DESKTOP, _NULL_WINDOW_ID)
    if not infos:
        return None
    try:
        own_pid = os.getpid()
        for i in range(cf.CFArrayGetCount(infos)):
            info = cf.CFArrayGetValueAtIndex(infos, i)     # borrowed ref
            if not info:
                continue
            if _num(info, "kCGWindowLayer", 9, ctypes.c_int, -1) != 0:
                continue                 # 9 = kCFNumberIntType
            if _num(info, "kCGWindowOwnerPID", 9, ctypes.c_int, -1) == own_pid:
                continue
            if _num(info, "kCGWindowAlpha", 13,             # kCFNumberDoubleType
                    ctypes.c_double, 1.0) <= 0.0:
                continue
            bounds = cf.CFDictionaryGetValue(info, keys["kCGWindowBounds"])
            if not bounds or cf.CFGetTypeID(bounds) != be["dict_tid"]:
                continue
            rect = be["CGRect"]()
            if not cg.CGRectMakeWithDictionaryRepresentation(
                    bounds, ctypes.byref(rect)):
                continue
            if rect.w < WINDOW_SIT_MIN_W or rect.h < WINDOW_SIT_MIN_H:
                continue
            return (int(rect.x), int(rect.y), int(rect.w), int(rect.h))
        return None
    finally:
        cf.CFRelease(infos)              # the one +1 reference we hold


def frontmost_window_frame() -> Optional[Tuple[int, int, int, int]]:
    """(x, y, w, h) of the frontmost regular window, global screen coords.

    The window list arrives front-to-back, so the first entry that is a
    regular window (layer 0), visible (alpha > 0), plausibly sized and NOT
    owned by our own process is the frontmost window of the frontmost app.
    Coordinates use the top-left-origin global screen space Qt also uses on
    macOS. Returns None when no suitable window exists or anything at all
    goes wrong — this never raises.
    """
    if sys.platform != "darwin":
        return None
    try:
        if Quartz is not None:
            return _frame_via_quartz()
        return _frame_via_ctypes()
    except Exception:
        return None
