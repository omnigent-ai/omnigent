"""Give redirected stdio the Windows ANSI code page defaults (cp1252).

Stand-in for a Windows console: CPython opens a redirected stdout strict and
stderr backslashreplace in the locale code page unless UTF-8 mode is requested
explicitly, so PYTHONUTF8=1 / -X utf8 is honoured the way Windows honours it.
"""

from __future__ import annotations

import os
import sys

_UTF8_REQUESTED = os.environ.get("PYTHONUTF8") == "1" or sys._xoptions.get("utf8") not in (
    None,
    "0",
)

if not _UTF8_REQUESTED and not os.environ.get("PYTHONIOENCODING"):
    for _name, _errors in (("stdout", "strict"), ("stderr", "backslashreplace")):
        _stream = getattr(sys, _name, None)
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _stream is not None and _reconfigure is not None and not _stream.isatty():
            _reconfigure(encoding="cp1252", errors=_errors)
