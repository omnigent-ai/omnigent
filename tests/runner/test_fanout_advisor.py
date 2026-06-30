"""Tests for :mod:`omnigent.runner.fanout_advisor`.

Covers:

- With routing_client configured: returns model/tier/rationale per task.
- With routing_client=None: returns router_on=False with null model/tier.
- Unknown worker agent falls back gracefully (no crash).
- Multiple tasks are all processed independently.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.runner.fanout_advisor import advise_fanout
from omnigent.server.smart_routing import RoutingResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_routing_client(result: RoutingResult | None) -> Any:  # type: ignore[explicit-any]
    """Return a mock routing client whose ``route`` returns *result*."""
    client = MagicMock()
    client.route = AsyncMock(return_value=result)
    return client


def _make_caps(routing_client: Any | None) -> Any:  # type: ignore[explicit-any]
    """Return a minimal mock RuntimeCaps."""
    caps = MagicMock()
    caps.routing_client = routing_client
    return caps


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advise_fanout_with_routing_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a configured routing_client, each task gets a recommendation."""
    verdict = RoutingResult(
        model="databricks-claude-opus-4-8",
        tier="expensive",
        rationale="Multi-file refactor needs deep reasoning.",
    )
    client = _make_routing_client(verdict)
    caps = _make_caps(client)
    monkeypatch.setattr("omnigent.runtime._globals._caps", caps)

    tasks = [
        {"title": "auth-refactor", "agent": "claude_code", "task": "Refactor the auth flow"},
    ]
    results = await advise_fanout(tasks, spec=None, conversation_id="conv_test")

    assert len(results) == 1
    rec = results[0]
    assert rec["title"] == "auth-refactor"
    assert rec["agent"] == "claude_code"
    assert rec["model"] == "databricks-claude-opus-4-8"
    assert rec["tier"] == "expensive"
    assert "reasoning" in rec["rationale"].lower() or rec["rationale"]
    # route was called with the task text and claude tiers
    client.route.assert_called_once()
    call_args = client.route.call_args
    assert call_args[0][0] == "Refactor the auth flow"
    tiers_arg = call_args[0][1]
    assert "cheap" in tiers_arg and "medium" in tiers_arg and "expensive" in tiers_arg


@pytest.mark.asyncio
async def test_advise_fanout_no_routing_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """With routing_client=None, all tasks get null model/tier and 'router not configured'."""
    caps = _make_caps(None)
    monkeypatch.setattr("omnigent.runtime._globals._caps", caps)

    tasks = [
        {"title": "task-a", "agent": "claude_code", "task": "Write tests"},
        {"title": "task-b", "agent": "codex", "task": "Generate code"},
    ]
    results = await advise_fanout(tasks, spec=None, conversation_id=None)

    assert len(results) == 2
    for rec in results:
        assert rec["model"] is None
        assert rec["tier"] is None
        assert rec["rationale"] == "router not configured"


@pytest.mark.asyncio
async def test_advise_fanout_unknown_agent_graceful(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown agent name falls back gracefully without crashing."""
    verdict = RoutingResult(
        model="databricks-gpt-5-4",
        tier="medium",
        rationale="Standard task.",
    )
    client = _make_routing_client(verdict)
    caps = _make_caps(client)
    monkeypatch.setattr("omnigent.runtime._globals._caps", caps)

    tasks = [
        {
            "title": "mystery-task",
            "agent": "completely_unknown_worker",
            "task": "Do something",
        },
    ]
    results = await advise_fanout(tasks, spec=None, conversation_id=None)

    assert len(results) == 1
    rec = results[0]
    # Unknown agent → no tiers → null model/tier with an informative rationale
    assert rec["model"] is None
    assert rec["tier"] is None
    assert "no tiers available" in rec["rationale"]
    # route should NOT have been called (no tiers to pass)
    client.route.assert_not_called()


@pytest.mark.asyncio
async def test_advise_fanout_multiple_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple tasks produce independent recommendations (one route() call per task)."""
    verdict = RoutingResult(
        model="databricks-claude-haiku-4-5",
        tier="cheap",
        rationale="Simple lookup.",
    )
    client = _make_routing_client(verdict)
    caps = _make_caps(client)
    monkeypatch.setattr("omnigent.runtime._globals._caps", caps)

    tasks = [
        {"title": "t1", "agent": "claude_code", "task": "Hello"},
        {"title": "t2", "agent": "claude_code", "task": "World"},
        {"title": "t3", "agent": "codex", "task": "Generate snippet"},
    ]
    results = await advise_fanout(tasks, spec=None, conversation_id=None)

    assert len(results) == 3
    # route called once per task (all three have known harnesses)
    assert client.route.call_count == 3
    titles = [r["title"] for r in results]
    assert titles == ["t1", "t2", "t3"]


@pytest.mark.asyncio
async def test_advise_fanout_none_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """When route() returns None (conversational), recommendation has null model/tier."""
    client = _make_routing_client(None)  # route returns None
    caps = _make_caps(client)
    monkeypatch.setattr("omnigent.runtime._globals._caps", caps)

    tasks = [{"title": "chat", "agent": "claude_code", "task": "Thanks!"}]
    results = await advise_fanout(tasks, spec=None, conversation_id=None)

    assert len(results) == 1
    rec = results[0]
    assert rec["model"] is None
    assert rec["tier"] is None
    assert "no verdict" in rec["rationale"]


@pytest.mark.asyncio
async def test_advise_fanout_uses_spec_sub_agent_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Harness is resolved from the spec sub-agent when available."""
    verdict = RoutingResult(
        model="databricks-gpt-5-4",
        tier="medium",
        rationale="Focused task.",
    )
    client = _make_routing_client(verdict)
    caps = _make_caps(client)
    monkeypatch.setattr("omnigent.runtime._globals._caps", caps)

    # Build a minimal spec with a sub-agent using the openai-agents harness.
    sub_executor = MagicMock()
    sub_executor.config = {"harness": "openai-agents"}
    sub_executor.type = "openai-agents"
    sub_spec = MagicMock()
    sub_spec.name = "my_pi_agent"
    sub_spec.executor = sub_executor

    parent_spec = MagicMock()
    parent_spec.sub_agents = [sub_spec]

    tasks = [
        {"title": "pi-task", "agent": "my_pi_agent", "task": "Analyse the data"},
    ]
    results = await advise_fanout(tasks, spec=parent_spec, conversation_id=None)

    assert len(results) == 1
    rec = results[0]
    assert rec["model"] == "databricks-gpt-5-4"
    assert rec["tier"] == "medium"
    # Tiers passed to route should be GPT tiers (openai-agents → gpt family)
    tiers_arg = client.route.call_args[0][1]
    assert any("gpt" in m for models in tiers_arg.values() for m in models)
