"""Tests for codex-native model resolution from the agent spec.

``_codex_native_model_from_spec`` is the seam that turns a session's
``executor.model`` (set via a config.yaml ``model:`` key) into the model
the native Codex TUI launches with. The canonical ``executor.model`` field
wins — the same field every in-process harness consumes — with a fallback
to ``executor.config["model"]`` for bundle specs that pin the model inside
the harness config block. Gateway-routed ``databricks-*`` ids are valid
Codex models on the Databricks path, so they pass through (unlike
cursor-native).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runner.app import ResolvedSpec, _codex_native_model_from_spec
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _spec(model: str | None, config_model: str | None = None) -> AgentSpec:
    """Build a minimal agent spec carrying *model* on its executor block."""
    config: dict[str, str] = {"harness": "codex-native"}
    if config_model is not None:
        config["model"] = config_model
    return AgentSpec(
        spec_version=1,
        name="codex",
        executor=ExecutorSpec(model=model, config=config),
    )


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-5.4-mini", "gpt-5.4-mini"),
        ("databricks-gpt-5-4-mini", "databricks-gpt-5-4-mini"),
        (None, None),
        ("", None),
    ],
    ids=[
        "openai-model",
        "databricks-passthrough",
        "no-model",
        "empty-model",
    ],
)
def test_codex_native_model_from_spec(model: str | None, expected: str | None) -> None:
    """A pinned model id on ``executor.model`` is returned; missing/empty pins resolve to None."""
    assert _codex_native_model_from_spec(_spec(model)) == expected


def test_codex_native_model_from_spec_config_fallback() -> None:
    """A bundle spec pinning the model inside ``executor.config`` still resolves."""
    assert (
        _codex_native_model_from_spec(_spec(None, config_model="gpt-5.4-mini")) == "gpt-5.4-mini"
    )


def test_codex_native_model_from_spec_canonical_field_wins() -> None:
    """When both slots are set, the canonical ``executor.model`` wins."""
    spec = _spec("databricks-gpt-5-4-mini", config_model="gpt-5.5")
    assert _codex_native_model_from_spec(spec) == "databricks-gpt-5-4-mini"


def test_codex_native_model_from_spec_non_string_config_model() -> None:
    """A non-string ``config["model"]`` is ignored rather than propagated."""
    spec = AgentSpec(
        spec_version=1,
        name="codex",
        executor=ExecutorSpec(config={"harness": "codex-native", "model": 5}),
    )
    assert _codex_native_model_from_spec(spec) is None


def test_codex_native_model_from_spec_none_spec() -> None:
    """A missing spec yields no model (provider default applies)."""
    assert _codex_native_model_from_spec(None) is None


def test_codex_native_model_from_spec_resolved_wrapper() -> None:
    """A ResolvedSpec wrapper unwraps to the same model pin."""
    wrapped = ResolvedSpec(spec=_spec("gpt-5.4-mini"), workdir=Path("/tmp"))
    assert _codex_native_model_from_spec(wrapped) == "gpt-5.4-mini"
