"""An explicit opencode spec profile is distinguished from the ambient one.

The launch order is: session binding → explicit ``executor.config.profile`` →
configured ``config.yaml`` default → ambient/managed fallback. That precedence
hinges on telling an explicitly-declared profile apart from the ambient
``DATABRICKS_CONFIG_PROFILE`` env: the explicit one is resolved BEFORE the
configured default (so an explicitly selected workspace is never silently
replaced), while the ambient one is only a fallback BELOW it.
"""

from __future__ import annotations

import pytest

from omnigent.runner.native.orchestration import (
    _opencode_native_explicit_profile,
    _opencode_native_profile_from_spec,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _spec(profile: str | None) -> AgentSpec:
    config: dict[str, object] = {"harness": "opencode-native"}
    if profile is not None:
        config["profile"] = profile
    return AgentSpec(
        spec_version=1,
        name="worker",
        executor=ExecutorSpec(type="omnigent", config=config),
    )


def test_explicit_profile_returns_spec_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicitly declared profile is returned and ignores the ambient env."""
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "ambient-workspace")

    assert _opencode_native_explicit_profile(_spec("workspace-A")) == "workspace-A"


def test_explicit_profile_none_without_spec_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a spec profile the explicit resolver declines, even with an ambient env.

    So the configured ``config.yaml`` default is consulted before the ambient
    profile, rather than the ambient profile pre-empting it.
    """
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "ambient-workspace")

    assert _opencode_native_explicit_profile(_spec(None)) is None
    # The ambient profile still surfaces through the fallback resolver.
    assert _opencode_native_profile_from_spec(_spec(None)) == "ambient-workspace"


def test_explicit_profile_none_for_missing_spec() -> None:
    assert _opencode_native_explicit_profile(None) is None
