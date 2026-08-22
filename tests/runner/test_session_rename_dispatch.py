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
async def test_session_rename_dispatches_repeatable_patch_to_current_session() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "conv_current", **json.loads(request.content)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        outputs = [
            await execute_tool(
                tool_name="sys_session_rename",
                arguments=json.dumps({"title": title}),
                server_client=server_client,
                conversation_id="conv_current",
                agent_spec=AgentSpec(spec_version=1),
            )
            for title in ("Debug auth timeout", "Verify auth timeout fix")
        ]

    assert [json.loads(output) for output in outputs] == [
        {"renamed": True, "title": "Debug auth timeout", "reason": None},
        {"renamed": True, "title": "Verify auth timeout fix", "reason": None},
    ]
    assert len(requests) == 2
    assert all(request.method == "PATCH" for request in requests)
    assert all(request.url.path == "/v1/sessions/conv_current" for request in requests)
    assert [json.loads(request.content) for request in requests] == [
        {"title": "Debug auth timeout"},
        {"title": "Verify auth timeout fix"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "expected_error"),
    [
        ("x", "2-60 characters"),
        ("  ", "2-60 characters"),
        ("x" * 61, "2-60 characters"),
        ("Debug auth\ntimeout", "single line"),
    ],
)
async def test_session_rename_rejects_invalid_titles_before_request(
    title: str,
    expected_error: str,
) -> None:
    requests: list[httpx.Request] = []

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(500)
        ),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": title}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert expected_error in json.loads(output)["error"]
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_error"),
    [
        (httpx.Response(503, text="server unavailable"), "returned 503"),
        (httpx.Response(200, text="not-json"), "returned invalid JSON"),
        (httpx.Response(200, json=["unexpected"]), "returned a non-object response"),
        (
            httpx.Response(200, json={"id": "conv_current", "title": None}),
            "response omitted the updated title",
        ),
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
