"""Dispatch tests for the ``nimble_extract`` builtin (runner-local Extract Templates).

Lock in that nimble_extract is runner-local (so a non-OpenAI model's call
resolves to ``NimbleExtractTool.invoke`` → ``POST /v2/extract/templates/run``)
and not relayed to native harnesses.
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
    _execute_nimble_extract_tool,
    _nimble_extract_config_from_spec,
    should_dispatch_locally,
)

_RUN_URL = "https://sdk.nimbleway.com/v2/extract/templates/run"


def _spec_with_nimble_extract(config: dict[str, str]) -> SimpleNamespace:
    """Minimal agent_spec stub: a single ``nimble_extract`` builtin carrying ``config``."""
    return SimpleNamespace(
        tools=SimpleNamespace(
            builtins=[SimpleNamespace(name="nimble_extract", config=config)],
        ),
    )


def test_nimble_extract_is_runner_local() -> None:
    """``nimble_extract`` dispatches locally, like ``web_search``."""
    assert "nimble_extract" in _ALL_LOCAL_TOOLS
    assert should_dispatch_locally("nimble_extract") is True


def test_nimble_extract_not_relayed_to_native_harnesses() -> None:
    """Native harnesses use their own tooling; nimble_extract is not relayed."""
    assert "nimble_extract" not in _NATIVE_RELAY_BUILTIN_TOOLS


def test_retired_nimble_agent_name_not_runner_local() -> None:
    """The retired predecessor name has no dispatch route at all."""
    assert "nimble_agent" not in _ALL_LOCAL_TOOLS
    assert should_dispatch_locally("nimble_agent") is False


def test_config_from_spec_reads_config() -> None:
    """The config helper returns the ``nimble_extract`` builtin's config dict."""
    config = {"api_key": "k", "template": "google_search"}
    spec = _spec_with_nimble_extract(config)
    assert _nimble_extract_config_from_spec(spec) == config


def test_config_from_spec_empty_when_absent() -> None:
    """No ``nimble_extract`` entry → empty config."""
    spec = SimpleNamespace(tools=SimpleNamespace(builtins=[]))
    assert _nimble_extract_config_from_spec(spec) == {}
    assert _nimble_extract_config_from_spec(None) == {}


@respx.mock
def test_dispatch_calls_extract_run() -> None:
    """The handler builds NimbleExtractTool from spec config and runs the
    template off the event loop, carrying the tracking header."""
    route = respx.post(_RUN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": "task_dispatch",
                "url": "https://www.google.com/search",
                "status": "success",
                "data": {
                    "parsing": {
                        "status": "success",
                        "entities": {"OrganicResult": [{"title": "Nimble"}]},
                    }
                },
                "metadata": {},
            },
        )
    )

    spec = _spec_with_nimble_extract({"api_key": "k", "template": "acme_serp_snapshot"})
    out = asyncio.run(
        _execute_nimble_extract_tool(
            {"params": {"query": "nimbleway"}},
            agent_spec=spec,
            conversation_id="c",
        )
    )

    assert route.call_count == 1
    request = route.calls.last.request
    assert request.headers["X-Client-Source"] == "omnigent"
    assert json.loads(request.content) == {
        "template": "acme_serp_snapshot",
        "params": {"query": "nimbleway"},
    }
    assert "OrganicResult" in out


@respx.mock
def test_dispatch_returns_error_string_not_exception() -> None:
    """HTTP failures surface as strings through dispatch, never as exceptions."""
    respx.post(_RUN_URL).mock(return_value=httpx.Response(401))
    spec = _spec_with_nimble_extract({"api_key": "k", "template": "acme_serp_snapshot"})
    out = asyncio.run(
        _execute_nimble_extract_tool(
            {"params": {"query": "x"}}, agent_spec=spec, conversation_id="c"
        )
    )
    assert out.startswith("Nimble extract error: HTTP 401")
