"""Runner dispatch and native-relay coverage for session renaming."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import (
    build_native_relay_tool_schemas,
    dispatch_tool_locally,
    execute_tool,
)
from omnigent.spec.types import AgentSpec


@pytest.mark.parametrize("spec", [AgentSpec(spec_version=1), None])
def test_native_relay_exposes_session_rename(spec: AgentSpec | None) -> None:
    schemas = build_native_relay_tool_schemas(spec)

    rename = next(schema for schema in schemas if schema["name"] == "sys_session_rename")

    assert rename["parameters"]["required"] == ["title"]
    assert rename["parameters"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_session_rename_dispatches_to_current_session() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"renamed": True, "title": "Debug auth timeout", "reason": None},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "Debug auth timeout"}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {
        "renamed": True,
        "title": "Debug auth timeout",
        "reason": None,
    }
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/sessions/conv_current/auto-title"
    assert json.loads(requests[0].content) == {"title": "Debug auth timeout"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_error"),
    [
        (httpx.Response(503, text="server unavailable"), "returned 503"),
        (httpx.Response(200, text="not-json"), "returned invalid JSON"),
        (httpx.Response(200, json=["unexpected"]), "returned a non-object response"),
    ],
)
async def test_session_rename_server_failures_are_tool_results(
    response: httpx.Response,
    expected_error: str,
) -> None:
    """Rename metadata failures never escape into the active session turn."""

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "Debug auth timeout"}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert expected_error in json.loads(output)["error"]


@pytest.mark.asyncio
async def test_session_rename_transport_failure_is_delivered_to_harness() -> None:
    """A failed rename still resolves the harness tool call so the turn continues."""
    delivered: list[dict[str, object]] = []

    def server_handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("server unavailable")

    def harness_handler(request: httpx.Request) -> httpx.Response:
        delivered.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(server_handler),
            base_url="http://server",
        ) as server_client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(harness_handler),
            base_url="http://harness",
        ) as harness_client,
    ):
        output = await dispatch_tool_locally(
            tool_name="sys_session_rename",
            call_id="call_rename",
            arguments=json.dumps({"title": "Debug auth timeout"}),
            response_id="response_1",
            harness_client=harness_client,
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert "sys_session_rename failed" in json.loads(output)["error"]
    assert delivered == [
        {
            "type": "tool_result",
            "call_id": "call_rename",
            "output": output,
        }
    ]
