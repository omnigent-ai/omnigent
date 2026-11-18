"""Press key chords on the X display through XTEST, like a physical keyboard.

Electron runs menu accelerators such as View > Zoom In only for OS key events
the page did not handle. Playwright's synthetic keys never reach that path, so
desktop tests that cover accelerators use this instead.

Usage: ``python3 xtestKeys.py --check`` exits 0 when XTEST is usable, and
``python3 xtestKeys.py ctrl+equal ctrl+shift+equal`` presses each chord in turn.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
import time

_MODIFIERS = {"ctrl": "Control_L", "shift": "Shift_L", "alt": "Alt_L", "meta": "Super_L"}


def _load() -> tuple[ctypes.CDLL, ctypes.CDLL, int]:
    x11_path, xtst_path = ctypes.util.find_library("X11"), ctypes.util.find_library("Xtst")
    if not x11_path or not xtst_path:
        raise OSError("libX11 or libXtst is not installed")
    x11, xtst = ctypes.CDLL(x11_path), ctypes.CDLL(xtst_path)
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XStringToKeysym.restype = ctypes.c_ulong
    x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
    x11.XKeysymToKeycode.restype = ctypes.c_ubyte
    x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XFlush.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    xtst.XTestFakeKeyEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    xtst.XTestQueryExtension.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_int)] * 4
    display = x11.XOpenDisplay(None)
    if not display:
        raise OSError("cannot open the X display")
    versions = [ctypes.c_int() for _ in range(4)]
    if not xtst.XTestQueryExtension(display, *(ctypes.byref(value) for value in versions)):
        x11.XCloseDisplay(display)
        raise OSError("the X display does not support XTEST")
    return x11, xtst, display


def _keycode(x11: ctypes.CDLL, display: int, name: str) -> int:
    code = x11.XKeysymToKeycode(display, x11.XStringToKeysym(name.encode()))
    if not code:
        raise ValueError(f"no keycode for {name!r}")
    return code


def main(argv: list[str]) -> int:
    try:
        x11, xtst, display = _load()
    except OSError as error:
        print(error, file=sys.stderr)
        return 1
    try:
        if argv == ["--check"]:
            return 0
        for chord in argv:
            keys = [_MODIFIERS.get(part, part) for part in chord.split("+")]
            codes = [_keycode(x11, display, key) for key in keys]
            for code in codes:
                xtst.XTestFakeKeyEvent(display, code, True, 0)
            for code in reversed(codes):
                xtst.XTestFakeKeyEvent(display, code, False, 0)
            x11.XFlush(display)
            time.sleep(0.2)
        return 0
    finally:
        x11.XCloseDisplay(display)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
