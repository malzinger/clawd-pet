"""Hide the Dock icon on macOS — the pet lives in the menu bar (tray) only.

A plain Python/Qt process shows up in the Dock and the Cmd-Tab switcher by
default. Switching NSApplication to the Accessory activation policy removes
both while keeping the tray icon and all windows working — the same thing an
LSUIElement Info.plist entry would do for a bundled app. Done via ctypes so
no PyObjC dependency is needed; any failure is silently ignored (the pet
then simply keeps its Dock icon).
"""
import ctypes
import ctypes.util
import sys

_ACCESSORY = 1        # NSApplicationActivationPolicyAccessory


def hide_dock_icon() -> bool:
    """Menu-bar-only mode. Call after QApplication exists. True on success."""
    if sys.platform != "darwin":
        return False
    try:
        path = ctypes.util.find_library("objc")
        if not path:
            return False
        libobjc = ctypes.cdll.LoadLibrary(path)
        libobjc.objc_getClass.restype = ctypes.c_void_p
        libobjc.objc_getClass.argtypes = [ctypes.c_char_p]
        libobjc.sel_registerName.restype = ctypes.c_void_p
        libobjc.sel_registerName.argtypes = [ctypes.c_char_p]
        # exact prototypes per call — required for the arm64 objc_msgSend ABI
        send = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
            ("objc_msgSend", libobjc))
        send_policy = ctypes.CFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(
            ("objc_msgSend", libobjc))
        nsapp = send(libobjc.objc_getClass(b"NSApplication"),
                     libobjc.sel_registerName(b"sharedApplication"))
        if not nsapp:
            return False
        return bool(send_policy(
            nsapp, libobjc.sel_registerName(b"setActivationPolicy:"),
            _ACCESSORY))
    except Exception:                    # noqa: BLE001 — cosmetic feature only
        return False
