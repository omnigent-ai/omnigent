"""Tests for routing the cognee memory builtins through runner-local dispatch.

Without runner-local dispatch a wrapped harness's (claude-sdk / codex / …)
call to cognee_search falls through to the harness, which has no such tool,
and silently no-ops. These lock in that the tools dispatch locally, are
relayed to native harnesses, resolve the dataset from the threaded agent
identity, and honor the runtime kill-switch.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _COGNEE_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _execute_cognee_tool,
    should_dispatch_locally,
)


def _spec(config: dict[str, str], name: str = "cognee_search") -> SimpleNamespace:
    return SimpleNamespace(
        executor=SimpleNamespace(model="claude-opus-4-8"),
        tools=SimpleNamespace(builtins=[SimpleNamespace(name=name, config=config)]),
    )


@pytest.fixture()
def _cognee_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the runtime gate open and the registry to serve the tools."""
    import omnigent.runtime.memory as memory_mod
    import omnigent.tools.builtins as builtins_mod
    from omnigent.tools.builtins.cognee import CogneeRememberTool, CogneeSearchTool

    monkeypatch.setattr(memory_mod, "cognee_available", lambda: True)
    monkeypatch.setattr(memory_mod, "cognee_settings", dict)
    real_get = builtins_mod.get_builtin_tool

    def serving_get(name: str, config: dict[str, str] | None = None) -> object | None:
        if name == "cognee_search":
            return CogneeSearchTool(config=config)
        if name == "cognee_remember":
            return CogneeRememberTool(config=config)
        return real_get(name, config)

    monkeypatch.setattr(builtins_mod, "get_builtin_tool", serving_get)


def test_cognee_tool_set_is_exactly_the_two() -> None:
    assert set(_COGNEE_TOOLS) == {"cognee_search", "cognee_remember"}


@pytest.mark.parametrize("name", ["cognee_search", "cognee_remember"])
def test_cognee_tools_are_runner_local(name: str) -> None:
    assert name in _ALL_LOCAL_TOOLS
    assert should_dispatch_locally(name) is True


@pytest.mark.parametrize("name", ["cognee_search", "cognee_remember"])
def test_cognee_tools_relayed_to_native_harnesses(name: str) -> None:
    # Like the Hindsight memory builtins: native harnesses have no built-in
    # long-term memory of their own, so the relay must advertise these.
    assert name in _NATIVE_RELAY_BUILTIN_TOOLS


@pytest.mark.usefixtures("_cognee_usable")
def test_search_dispatch_uses_agent_id_as_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no config dataset, the dataset defaults to the threaded agent_id."""
    import omnigent.runtime.memory as memory_mod

    search = MagicMock(return_value=["cognee is a knowledge graph"])
    monkeypatch.setattr(memory_mod, "memory_search", search)
    result = asyncio.run(
        _execute_cognee_tool(
            {"query": "what is cognee"},
            tool_name="cognee_search",
            agent_spec=_spec({}),
            conversation_id="conv_1",
            agent_id="ag_remy",
        )
    )
    assert "knowledge graph" in result
    assert search.call_args.args[1] == ["ag_remy"]


@pytest.mark.usefixtures("_cognee_usable")
def test_remember_dispatch_honors_shared_dataset_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec-granted shared_dataset lets agents exchange memory."""
    import omnigent.runtime.memory as memory_mod

    add = MagicMock(return_value=True)
    monkeypatch.setattr(memory_mod, "memory_add", add)
    spec = _spec({"shared_dataset": "team_knowledge"}, name="cognee_remember")
    result = asyncio.run(
        _execute_cognee_tool(
            {"content": "release friday", "scope": "shared"},
            tool_name="cognee_remember",
            agent_spec=spec,
            conversation_id="conv_1",
            agent_id="ag_remy",
        )
    )
    assert "'team_knowledge'" in result
    assert add.call_args.args == ("release friday", "team_knowledge")


def test_dispatch_reports_disabled_when_kill_switched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.runtime.memory as memory_mod

    monkeypatch.setattr(memory_mod, "cognee_available", lambda: False)
    result = asyncio.run(
        _execute_cognee_tool(
            {"query": "q"},
            tool_name="cognee_search",
            agent_spec=_spec({}),
            agent_id="ag_remy",
        )
    )
    assert "not available" in result
    json.dumps(result)  # result is a plain string
