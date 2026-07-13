"""Tests for claude-native (and codex-native) model resolution from the spec.

``_claude_native_model_from_spec`` is the seam that turns a session's
``executor.model`` (set via a config.yaml ``model:`` key) into the
``--model`` the auto-created Claude terminal launches with. Before it
existed, an agent config's ``executor.model`` was silently ignored on the
host-spawned native launch (the host log showed ``model_override_set=False``
and the child came up on the harness default) — the exact repro was an
uploaded ``config_path`` agent with ``executor.model: claude-haiku-4-5``.

``_codex_native_model_from_spec`` shares a related fix: it used to read the
legacy ``executor.config["model"]`` bag (never the canonical field), so a
spec-declared model never reached the codex launch either.
"""

from __future__ import annotations

from pathlib import Path

from omnigent.runner.app import (
    ResolvedSpec,
    _claude_native_model_from_spec,
    _codex_native_model_from_spec,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _spec(model: str | None) -> AgentSpec:
    """Build a minimal agent spec carrying *model* on its executor block."""
    return AgentSpec(spec_version=1, name="leaf", executor=ExecutorSpec(model=model))


def test_claude_native_model_passthrough() -> None:
    """A pinned model id is returned verbatim."""
    assert _claude_native_model_from_spec(_spec("claude-haiku-4-5")) == "claude-haiku-4-5"
    assert _claude_native_model_from_spec(_spec("databricks-claude-haiku-4-5")) == (
        "databricks-claude-haiku-4-5"
    )


def test_claude_native_model_no_pin_returns_none() -> None:
    """No model declared → None (launch falls back to the provider default)."""
    assert _claude_native_model_from_spec(_spec(None)) is None
    assert _claude_native_model_from_spec(_spec("")) is None


def test_claude_native_model_none_spec() -> None:
    """A missing spec yields no model override."""
    assert _claude_native_model_from_spec(None) is None


def test_claude_native_model_from_resolved_spec_wrapper() -> None:
    """The model is read through a ``ResolvedSpec`` wrapper too."""
    wrapped = ResolvedSpec(spec=_spec("claude-haiku-4-5"), workdir=Path("/tmp"))
    assert _claude_native_model_from_spec(wrapped) == "claude-haiku-4-5"


def test_codex_native_model_reads_canonical_executor_field() -> None:
    """``executor.model`` (the canonical field) is read first."""
    assert _codex_native_model_from_spec(_spec("gpt-5-4-mini")) == "gpt-5-4-mini"


def test_codex_native_model_falls_back_to_legacy_config_key() -> None:
    """Configs that stashed the model in ``executor.config`` still work."""
    spec = AgentSpec(
        spec_version=1,
        name="leaf",
        executor=ExecutorSpec(config={"model": "gpt-5-4-mini"}),
    )
    assert _codex_native_model_from_spec(spec) == "gpt-5-4-mini"


def test_codex_native_model_canonical_wins_over_legacy() -> None:
    """When both locations are set, the canonical field wins."""
    spec = AgentSpec(
        spec_version=1,
        name="leaf",
        executor=ExecutorSpec(model="gpt-5-4", config={"model": "gpt-5-4-mini"}),
    )
    assert _codex_native_model_from_spec(spec) == "gpt-5-4"
