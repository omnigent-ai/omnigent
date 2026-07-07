"""Dispatch tests for the ``nimble_agent`` builtin (runner-local Nimble WSA).

Lock in that nimble_agent is runner-local (so a non-OpenAI model's call resolves
to ``NimbleAgentTool.invoke`` → Nimble ``/v1/agent``) and not relayed to native
harnesses.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _execute_nimble_agent_tool,
    _nimble_agent_config_from_spec,
    should_dispatch_locally,
)


def _spec_with_nimble_agent(config: dict[str, str]) -> SimpleNamespace:
    """Minimal agent_spec stub: a single ``nimble_agent`` builtin carrying ``config``."""
    return SimpleNamespace(
        tools=SimpleNamespace(
            builtins=[SimpleNamespace(name="nimble_agent", config=config)],
        ),
    )


def test_nimble_agent_is_runner_local() -> None:
    """``nimble_agent`` dispatches locally, like ``web_search``."""
    assert "nimble_agent" in _ALL_LOCAL_TOOLS
    assert should_dispatch_locally("nimble_agent") is True


def test_nimble_agent_not_relayed_to_native_harnesses() -> None:
    """Native harnesses use their own tooling; nimble_agent is not relayed."""
    assert "nimble_agent" not in _NATIVE_RELAY_BUILTIN_TOOLS


def test_config_from_spec_reads_config() -> None:
    """The config helper returns the ``nimble_agent`` builtin's config dict."""
    spec = _spec_with_nimble_agent({"api_key": "k", "agent": "google_search"})
    assert _nimble_agent_config_from_spec(spec) == {"api_key": "k", "agent": "google_search"}


def test_config_from_spec_empty_when_absent() -> None:
    """No ``nimble_agent`` entry → empty config."""
    spec = SimpleNamespace(tools=SimpleNamespace(builtins=[]))
    assert _nimble_agent_config_from_spec(spec) == {}


def test_dispatch_calls_nimble_agent() -> None:
    """The dispatch handler builds NimbleAgentTool from spec config and calls Nimble WSA."""
    spec = _spec_with_nimble_agent({"api_key": "k"})
    fake = MagicMock()
    fake.json.return_value = {
        "data": {"parsing": {"entities": {"OrganicResult": []}}},
        "status": "success",
    }
    with patch("omnigent.tools.builtins.nimble_agent.httpx.post") as mock_post:
        mock_post.return_value = fake
        result = asyncio.run(
            _execute_nimble_agent_tool(
                {"query": "x"},
                agent_spec=spec,
                conversation_id="c",
            )
        )

    assert mock_post.call_count == 1, "dispatch must call Nimble WSA"
    assert "OrganicResult" in result
    assert mock_post.call_args.kwargs["headers"]["X-Client-Source"] == "omnigent"
