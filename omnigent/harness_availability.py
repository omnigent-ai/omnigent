"""Shared harness-readiness states and harness-family identifiers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal, TypeGuard

HARNESS_BINARY_MISSING: Final[Literal["binary-missing"]] = "binary-missing"
HARNESS_NEEDS_AUTH: Final[Literal["needs-auth"]] = "needs-auth"
HARNESS_VERSION_TOO_LOW: Final[Literal["version-too-low"]] = "version-too-low"
# A native (tmux-backed) harness whose CLI is usable but the host's tmux is
# missing, too old, or of unknown version. Older servers drop it from a hello and
# reject a live readiness refresh that contains it.
HARNESS_TMUX_UNAVAILABLE: Final[Literal["tmux-unavailable"]] = "tmux-unavailable"

HarnessUnavailableReason = Literal[
    "binary-missing", "needs-auth", "version-too-low", "tmux-unavailable"
]
HarnessAvailability = Literal[
    True,
    False,
    "binary-missing",
    "needs-auth",
    "version-too-low",
    "tmux-unavailable",
]

# Readiness and model-family checks must agree on every Codex spelling.
CODEX_CANONICAL_HARNESSES: Final[frozenset[str]] = frozenset(
    {"codex", "codex-native", "native-codex"}
)


def is_harness_availability(value: object) -> TypeGuard[HarnessAvailability]:
    """Return whether a decoded value is a supported readiness state."""
    return isinstance(value, bool) or value in (
        HARNESS_BINARY_MISSING,
        HARNESS_NEEDS_AUTH,
        HARNESS_VERSION_TOO_LOW,
        HARNESS_TMUX_UNAVAILABLE,
    )


def reported_harness_availability(
    harness: str | None,
    readiness: Mapping[str, object] | None,
) -> tuple[bool | None, str | None]:
    """Interpret host readiness; absent reports are unknown, absent entries unavailable."""
    from omnigent.harness_aliases import canonicalize_harness

    if not harness or harness == "auto" or not readiness:
        return None, None
    canonical = canonicalize_harness(harness) or harness
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
) -> tuple[bool | None, str | None]:
    """Treat host auth reports as advisory; sessions can supply credentials."""
    available, reason = reported_harness_availability(harness, readiness)
    return (None if reason == HARNESS_NEEDS_AUTH else available), reason
