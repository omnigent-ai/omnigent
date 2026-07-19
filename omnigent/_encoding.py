"""Encoding detection for external / vendor-written text files.

Windows text I/O defaults to the ANSI code page (cp1252), while modern tools —
and Python 3.15's default UTF-8 Mode — write UTF-8. To round-trip either without
corruption, detect the file's encoding (UTF-8 first, then the true OS locale
codec) and preserve it on rewrite.

The fallback uses :func:`locale.getencoding`, **not**
``locale.getpreferredencoding(False)``: the latter returns ``"utf-8"`` under
Python UTF-8 Mode even on a cp1252 host, so it would misdetect a real cp1252
file as UTF-8. ``getencoding()`` always reports the actual locale encoding.
See https://docs.python.org/3.12/library/locale.html#locale.getpreferredencoding
"""

from __future__ import annotations

import locale
from pathlib import Path


def locale_encoding() -> str:
    """The true OS locale text encoding, independent of Python UTF-8 Mode."""
    return locale.getencoding()


def detect_encoding(path: Path) -> str:
    """Return ``"utf-8"`` if *path* decodes as UTF-8, else the locale encoding.

    UTF-8 first because that is what modern CLIs write (and what UTF-8 Mode
    produces); the locale codec is the legacy fallback. A missing/unreadable
    path resolves to the locale encoding — callers that then hand it to
    ``ConfigParser.read`` (which silently skips missing files) stay correct.
    """
    try:
        path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return locale_encoding()
    return "utf-8"
