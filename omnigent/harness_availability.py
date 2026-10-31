"""Shared harness-readiness states and harness-family identifiers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal, TypeGuard

from omnigent_client import harness_readiness as _client_readiness

HARNESS_BINARY_MISSING: Final[Literal["binary-missing"]] = "binary-missing"
HARNESS_NEEDS_AUTH: Final[Literal["needs-auth"]] = _client_readiness.HARNESS_NEEDS_AUTH
HARNESS_VERSION_TOO_LOW: Final[Literal["version-too-low"]] = "version-too-low"

HarnessUnavailableReason = Literal["binary-missing", "needs-auth", "version-too-low"]
HarnessAvailability = Literal[
    True,
    False,
    "binary-missing",
    "needs-auth",
    "version-too-low",
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
    )


def reported_harness_availability(
    harness: str | None,
    readiness: Mapping[str, object] | None,
) -> tuple[bool | None, str | None]:
    """Interpret host readiness; absent reports are unknown, absent entries unavailable."""
    from omnigent.harness_aliases import canonicalize_harness

    return _client_readiness.reported_harness_availability(
        harness, readiness, canonicalize_harness
    )


def harness_launch_availability(
    harness: str | None,
    readiness: Mapping[str, object] | None,
) -> tuple[bool | None, str | None]:
    """Treat host auth reports as advisory; sessions can supply credentials."""
    from omnigent.harness_aliases import canonicalize_harness

    return _client_readiness.harness_launch_availability(harness, readiness, canonicalize_harness)
