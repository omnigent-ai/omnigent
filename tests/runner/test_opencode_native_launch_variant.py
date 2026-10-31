"""Launch-side model/variant resolution for the opencode-native terminal.

Covers the pure helpers ``_auto_create_opencode_terminal`` uses to decide the
model and variant pin it persists to bridge state, independently of booting a
real ``opencode serve``.
"""

from __future__ import annotations

import logging

import pytest

from omnigent.runner.native.orchestration import (
    _resolve_opencode_launch_model_variant,
    _variant_for_launch_model,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _spec(*, model: str | None = None, variant: str | None = None) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="opencode_agent",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "opencode-native"},
            model=model,
            variant=variant,
        ),
    )


def test_bundle_model_suffix_variant() -> None:
    """A ``model#variant`` suffix on the bundle model is split into the pin."""
    model, variant = _resolve_opencode_launch_model_variant(
        None, _spec(model="opencode-go/muse#xhigh")
    )
    assert (model, variant) == ("opencode-go/muse", "xhigh")


def test_bundle_explicit_variant() -> None:
    """An explicit ``executor.variant`` pins alongside the bundle model."""
    model, variant = _resolve_opencode_launch_model_variant(
        None, _spec(model="opencode-go/muse", variant="max")
    )
    assert (model, variant) == ("opencode-go/muse", "max")


def test_variant_only_spec_keeps_pin_without_model() -> None:
    """A variant-only spec keeps its pin with no model (OpenCode default)."""
    model, variant = _resolve_opencode_launch_model_variant(None, _spec(variant="max"))
    assert model is None
    assert variant == "max"


def test_explicit_variant_wins_over_suffix_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``executor.variant`` overrides a conflicting model-id suffix, logged."""
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        model, variant = _resolve_opencode_launch_model_variant(
            None, _spec(model="opencode-go/muse#low", variant="max"), session_id="conv_1"
        )
    assert (model, variant) == ("opencode-go/muse", "max")
    assert any("executor.variant" in r.message and "conv_1" in r.message for r in caplog.records)


def test_session_override_replaces_model_and_drops_bundle_variant() -> None:
    """A session/CLI model override replaces the model; the bundle variant
    does not ride along onto the replacement."""
    model, variant = _resolve_opencode_launch_model_variant(
        "provider/other", _spec(model="opencode-go/muse", variant="max")
    )
    assert model == "provider/other"
    assert variant is None


def test_no_model_no_variant() -> None:
    """No bundle model, no variant, no override resolves to nothing pinned."""
    assert _resolve_opencode_launch_model_variant(None, _spec()) == (None, None)
    assert _resolve_opencode_launch_model_variant(None, None) == (None, None)


def test_variant_dropped_when_launch_model_replaced() -> None:
    """Gateway/managed-connect swapping the model clears the pin."""
    assert _variant_for_launch_model("max", "opencode-go/muse", "databricks/gateway-x") is None


def test_variant_kept_when_launch_model_unchanged() -> None:
    """The pin survives when the resolved model is used unchanged."""
    assert _variant_for_launch_model("max", "opencode-go/muse", "opencode-go/muse") == "max"


def test_variant_for_launch_model_noop_without_variant() -> None:
    """No variant stays ``None`` regardless of model replacement."""
    assert _variant_for_launch_model(None, "opencode-go/muse", "databricks/gateway-x") is None
