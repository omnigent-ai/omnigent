"""Dispatch tests for the ``nimble_research`` builtin (runner-local Agent API v2).

Lock in that nimble_research is runner-local (so a non-OpenAI model's call
resolves to ``NimbleResearchTool.invoke`` → the v2 start → poll → result
lifecycle) and not relayed to native harnesses.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import respx

from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _execute_nimble_research_tool,
    _nimble_research_config_from_spec,
    should_dispatch_locally,
)

_BASE = "https://sdk.nimbleway.com"
_AGENT_ID = "wsa_0a1b2c3d-0000-4000-8000-000000000000"
_RUN_ID = "task_run_11111111-2222-4333-8444-555555555555"


def _spec_with_nimble_research(config: dict[str, str]) -> SimpleNamespace:
    """Minimal agent_spec stub: a single ``nimble_research`` builtin carrying ``config``."""
    return SimpleNamespace(
        tools=SimpleNamespace(
            builtins=[SimpleNamespace(name="nimble_research", config=config)],
        ),
    )


def test_nimble_research_is_runner_local() -> None:
    """``nimble_research`` dispatches locally, like ``web_search``."""
    assert "nimble_research" in _ALL_LOCAL_TOOLS
    assert should_dispatch_locally("nimble_research") is True


def test_nimble_research_not_relayed_to_native_harnesses() -> None:
    """Native harnesses use their own tooling; nimble_research is not relayed."""
    assert "nimble_research" not in _NATIVE_RELAY_BUILTIN_TOOLS


def test_config_from_spec_reads_config() -> None:
    """The config helper returns the ``nimble_research`` builtin's config dict."""
    config = {"api_key": "k", "agent_id": _AGENT_ID, "effort": "low"}
    spec = _spec_with_nimble_research(config)
    assert _nimble_research_config_from_spec(spec) == config


def test_config_from_spec_empty_when_absent() -> None:
    """No ``nimble_research`` entry → empty config."""
    spec = SimpleNamespace(tools=SimpleNamespace(builtins=[]))
    assert _nimble_research_config_from_spec(spec) == {}
    assert _nimble_research_config_from_spec(None) == {}


@respx.mock
def test_dispatch_executes_v2_lifecycle() -> None:
    """The handler builds the tool from spec config and runs the full v2
    lifecycle off the event loop (sync httpx inside ``asyncio.to_thread``
    under a running loop)."""
    completed = {
        "id": _RUN_ID,
        "interaction_id": "int_0001",
        "status": "completed",
        "is_active": False,
        "effort": "low",
        "created_at": "2026-07-22T00:00:00Z",
        "web_search_agent_id": _AGENT_ID,
    }
    create = respx.post(f"{_BASE}/v2/agents/{_AGENT_ID}/runs").mock(
        return_value=httpx.Response(202, json=completed)
    )
    result = respx.get(f"{_BASE}/v2/agents/{_AGENT_ID}/runs/{_RUN_ID}/result").mock(
        return_value=httpx.Response(
            200,
            json={
                "run": completed,
                "output": {
                    "type": "text",
                    "content": "Grounded answer.",
                    "trust": {
                        "confidence": "high",
                        "reasoning": "r",
                        "sources": [],
                        "claims": [],
                    },
                },
            },
        )
    )

    spec = _spec_with_nimble_research({"api_key": "k", "agent_id": _AGENT_ID})
    out = asyncio.run(
        _execute_nimble_research_tool(
            {"task": "profile Nimbleway"},
            agent_spec=spec,
            conversation_id="c",
        )
    )

    assert create.call_count == 1
    assert result.call_count == 1
    assert create.calls.last.request.headers["X-Client-Source"] == "omnigent"
    assert json.loads(create.calls.last.request.content)["input"] == "profile Nimbleway"
    envelope = json.loads(out)
    assert envelope["run_id"] == _RUN_ID
    assert envelope["output"]["content"] == "Grounded answer."


@respx.mock
def test_dispatch_returns_error_string_not_exception() -> None:
    """HTTP failures surface as strings through dispatch, never as exceptions."""
    respx.post(f"{_BASE}/v2/agents/{_AGENT_ID}/runs").mock(return_value=httpx.Response(401))
    spec = _spec_with_nimble_research({"api_key": "k", "agent_id": _AGENT_ID})
    out = asyncio.run(
        _execute_nimble_research_tool({"task": "x"}, agent_spec=spec, conversation_id="c")
    )
    assert out.startswith("Nimble research error: HTTP 401")
