"""ZCode print-mode validation."""

from __future__ import annotations

MODES: frozenset[str] = frozenset({"yolo"})
DEFAULT_MODE = "yolo"


class ZCodeModelError(ValueError):
    """A print-mode setting is unsupported."""


def normalize_mode(mode: str | None, *, yolo: bool | None = None) -> str:
    """Return a ZCode permission mode.

    Print mode cannot answer ``interaction/requestPermission``, so only
    ``yolo`` is safe. ``bypassPermissions`` and a true *yolo* flag map to it.

    :raises ZCodeModelError: *mode* is not ``yolo``.
    """
    if yolo is True and mode is None:
        return "yolo"
    if mode is None or mode == "":
        return DEFAULT_MODE
    if mode == "bypassPermissions":
        return "yolo"
    if mode not in MODES:
        raise ZCodeModelError(
            f"unsupported ZCode mode: {mode}; print mode supports only yolo because "
            "headless approval requests fail closed"
        )
    return mode
