"""Tests for claude-native model resolution from the agent spec.

``_claude_native_model_from_spec`` is the seam that turns a session's
``executor.model`` (set via a config.yaml ``model:`` key) into the
``claude --model`` value the native TUI launches with. Gateway-routed
``databricks-*`` ids are valid Claude Code models on the Databricks AI
gateway path, so they pass through (unlike cursor-native).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runner.app import ResolvedSpec, _claude_native_model_from_spec
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _spec(model: str | None) -> AgentSpec:
    """Build a minimal agent spec carrying *model* on its executor block."""
    return AgentSpec(spec_version=1, name="claude_code", executor=ExecutorSpec(model=model))


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-sonnet-5", "claude-sonnet-5"),
        ("claude-opus-4-8", "claude-opus-4-8"),
        ("databricks-claude-sonnet-5", "databricks-claude-sonnet-5"),
        (None, None),
        ("", None),
    ],
    ids=[
        "sonnet-5",
        "opus-passthrough",
        "databricks-passthrough",
        "no-model",
        "empty-model",
    ],
)
def test_claude_native_model_from_spec(model: str | None, expected: str | None) -> None:
    """A pinned Claude model id is returned; missing/empty pins resolve to None."""
    assert _claude_native_model_from_spec(_spec(model)) == expected


def test_claude_native_model_from_spec_none_spec() -> None:
    """A missing spec yields no model (no ``--model`` injected)."""
    assert _claude_native_model_from_spec(None) is None


def test_claude_native_model_from_spec_resolved_wrapper() -> None:
    """A ResolvedSpec wrapper unwraps to the same model pin."""
    wrapped = ResolvedSpec(spec=_spec("claude-sonnet-5"), workdir=Path("/tmp"))
    assert _claude_native_model_from_spec(wrapped) == "claude-sonnet-5"
