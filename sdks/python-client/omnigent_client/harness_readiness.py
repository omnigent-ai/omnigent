"""Interpret the harness readiness a host reports in ``GET /v1/hosts``.

Shared by the client SDK and the server so both decide launchability the
same way. Harness-name aliasing is plugin-defined on the server, so callers
pass their own ``canonicalize`` function; this module depends only on the
standard library.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final, Literal

HARNESS_NEEDS_AUTH: Final[Literal["needs-auth"]] = "needs-auth"


def reported_harness_availability(
    harness: str | None,
    readiness: Mapping[str, object] | None,
    canonicalize: Callable[[str], str | None] | None = None,
) -> tuple[bool | None, str | None]:
    """
    Interpret host readiness; absent reports are unknown, absent entries unavailable.

    :param harness: Harness the session needs, e.g. ``"claude-sdk"``.
    :param readiness: The host's ``configured_harnesses`` map, keyed by
        canonical harness name, or ``None`` when the host reported none.
    :param canonicalize: Maps an alias to its canonical harness name, e.g.
        ``"claude"`` to ``"claude-sdk"``. ``None`` looks *harness* up as given.
    :returns: ``(available, reason)``; ``(None, None)`` when unknown.
    """
    if not harness or harness == "auto" or not readiness:
        return None, None
    canonical = (canonicalize(harness) if canonicalize is not None else None) or harness
    value = readiness.get(canonical)
    if value is True:
        return True, None
    if value is False or value is None:
        return False, "unconfigured"
    if isinstance(value, str):
        return False, value
    return None, None


def harness_launch_availability(
    harness: str | None,
    readiness: Mapping[str, object] | None,
    canonicalize: Callable[[str], str | None] | None = None,
) -> tuple[bool | None, str | None]:
    """
    Treat host auth reports as advisory; sessions can supply credentials.

    :param harness: Harness the session needs, e.g. ``"claude-sdk"``.
    :param readiness: The host's ``configured_harnesses`` map, or ``None``.
    :param canonicalize: Optional alias normalizer; see
        :func:`reported_harness_availability`.
    :returns: ``(available, reason)``; ``available`` is ``None`` when
        unknown or when the host only reports missing credentials.
    """
    available, reason = reported_harness_availability(harness, readiness, canonicalize)
    return (None if reason == HARNESS_NEEDS_AUTH else available), reason
