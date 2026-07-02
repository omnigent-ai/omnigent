"""Import-safe install metadata types for harness plugins."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HarnessInstallSpec:
    """Install + auth metadata for one coding-harness CLI.

    This type intentionally lives outside :mod:`omnigent.onboarding` so
    optional harness plugins can declare setup metadata during entry-point
    discovery without importing the onboarding/provider stack.
    """

    display: str
    binary: str
    package: str | None
    login_args: tuple[str, ...] | None = None
    logout_args: tuple[str, ...] | None = None
    status_args: tuple[str, ...] | None = None
    install_hint: str | None = None
    login_status_key: str | None = None
    auth_hint: str | None = None
