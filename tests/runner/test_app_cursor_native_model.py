"""Tests for cursor-native model resolution from the agent spec.

``_cursor_native_model_from_spec`` is the seam that turns a session's
``executor.model`` (set via ``--model`` or a config.yaml ``model:`` key)
into the ``cursor-agent --model`` value the native TUI launches with.
A gateway-routed id is dropped so cursor-agent keeps its configured
default instead of erroring on an id it does not recognise.
"""

from __future__ import annotations

import logging

import pytest

from omnigent.runner.app import _cursor_native_model_from_spec
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _spec(model: str | None) -> AgentSpec:
    """Build a minimal agent spec carrying *model* on its executor block."""
    return AgentSpec(spec_version=1, name="cursor", executor=ExecutorSpec(model=model))


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # A cursor-agent model id is passed through verbatim.
        ("sonnet-4-thinking", "sonnet-4-thinking"),
        ("gpt-5", "gpt-5"),
        # No pin → None (cursor-agent uses its configured default).
        (None, None),
        ("", None),
        # Gateway-routed ids are not cursor-agent model ids → dropped.
        ("databricks-claude-opus", None),
        ("databricks/claude-opus", None),
    ],
    ids=[
        "cursor-id-passthrough",
        "cursor-id-gpt5",
        "no-model",
        "empty-model",
        "databricks-dash-dropped",
        "databricks-slash-dropped",
    ],
)
def test_cursor_native_model_from_spec(model: str | None, expected: str | None) -> None:
    """A usable cursor model id is returned; non-cursor ids resolve to None."""
    assert _cursor_native_model_from_spec(_spec(model)) == expected


def test_cursor_native_model_from_spec_none_spec() -> None:
    """A missing spec yields no model (no ``--model`` injected)."""
    assert _cursor_native_model_from_spec(None) is None


def test_cursor_native_model_from_spec_warns_on_dropped_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dropping a non-cursor id warns so the silent fallback is visible."""
    with caplog.at_level(logging.WARNING):
        assert _cursor_native_model_from_spec(_spec("databricks-claude-opus")) is None
    assert any("databricks-claude-opus" in rec.getMessage() for rec in caplog.records)
