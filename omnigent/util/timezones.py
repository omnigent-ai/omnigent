"""IANA timezone name validation shared by the server and runner."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def is_valid_timezone(name: object) -> bool:
    """
    Tell whether *name* is a usable IANA timezone key.

    :param name: Candidate zone key such as ``"America/Los_Angeles"``.
    :returns: ``True`` when the installed tz database resolves it.
    """
    if not isinstance(name, str) or not name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True
