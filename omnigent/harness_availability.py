"""Shared harness-readiness states and harness-family identifiers."""

from __future__ import annotations

from typing import Final, Literal, TypeGuard

HARNESS_BINARY_MISSING: Final[Literal["binary-missing"]] = "binary-missing"
HARNESS_NEEDS_AUTH: Final[Literal["needs-auth"]] = "needs-auth"

HarnessUnavailableReason = Literal["binary-missing", "needs-auth"]
HarnessAvailability = Literal[True, False, "binary-missing", "needs-auth"]

# Readiness and model-family checks must agree on every Codex spelling.
CODEX_CANONICAL_HARNESSES: Final[frozenset[str]] = frozenset(
    {"codex", "codex-native", "native-codex"}
)


def is_harness_availability(value: object) -> TypeGuard[HarnessAvailability]:
    """Return whether a decoded value is a supported readiness state."""
    return isinstance(value, bool) or value in (HARNESS_BINARY_MISSING, HARNESS_NEEDS_AUTH)
