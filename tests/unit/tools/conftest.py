"""Shared fixtures for tools tests."""

from __future__ import annotations

import pytest

from omnigent.tools.base import ToolContext


@pytest.fixture()
def tool_ctx() -> ToolContext:
    """
    Dummy :class:`ToolContext` for tool tests that don't
    depend on specific task/agent identity.

    :returns: A :class:`ToolContext` with placeholder IDs.
    """
    return ToolContext(task_id="task_test", agent_id="agent_test")
