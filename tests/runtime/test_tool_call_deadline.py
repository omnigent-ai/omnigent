"""Execution budgets preserve time spent working across bounded approval waits."""

import asyncio

import pytest

from omnigent.runtime import tool_call_deadline as deadlines


def test_execution_budget_preserves_consumed_time_and_bounds_a_stuck_approval(monkeypatch):
    now = 0.0
    monkeypatch.setattr(deadlines, "monotonic", lambda: now)
    deadline = deadlines.ToolCallDeadline(300)
    now = 100
    with deadline.pause(86400):
        now = 1000
        assert deadline.remaining() == 200
        with deadline.pause(86400):
            now = 1100
        assert deadline.remaining() == 200
        now = 86550
        assert deadline.remaining() == 150
    now = 86600
    assert deadline.remaining() == 100


@pytest.mark.asyncio
async def test_inherited_context_cannot_pause_the_parent_calls_budget(monkeypatch):
    now = 0.0
    monkeypatch.setattr(deadlines, "monotonic", lambda: now)
    deadline = deadlines.ToolCallDeadline(300)

    async def child():
        nonlocal now
        with deadlines.pause_tool_call_deadline(86400):
            now = 100
            assert deadline.remaining() == 200

    with deadlines.bind_tool_call_deadline(deadline):
        await asyncio.create_task(child())
        with deadlines.pause_tool_call_deadline(86400):
            now = 200
            assert deadline.remaining() == 200
    with deadlines.pause_tool_call_deadline(86400):
        now = 250
        assert deadline.remaining() == 150
