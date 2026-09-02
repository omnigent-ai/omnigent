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
from omnigent.tools.builtins.session_rename import SysSessionRenameTool

_TITLE_MAX_CHARS: int = SysSessionRenameTool().get_schema()["function"]["parameters"][
    "properties"
]["title"]["maxLength"]


def _top_level_session_handler(request: httpx.Request) -> httpx.Response | None:
    """Answer the dispatcher's top-level check for a parentless session.

    :param request: The intercepted request.
    :returns: A session snapshot for the GET probe, ``None`` for other
        requests (so the caller's handler decides).
    """
    if request.method == "GET" and request.url.path == "/v1/sessions/conv_current":
        return httpx.Response(200, json={"id": "conv_current", "parent_session_id": None})
    return None


@pytest.mark.parametrize("spec", [AgentSpec(spec_version=1), None])
def test_native_relay_exposes_session_rename(spec: AgentSpec | None) -> None:
    schemas = build_native_relay_tool_schemas(spec)

    rename = next(schema for schema in schemas if schema["name"] == "sys_session_rename")

    assert rename["parameters"]["required"] == ["title"]
    assert rename["parameters"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_session_rename_dispatches_repeatable_patch_to_current_session() -> None:
    patch_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        probe = _top_level_session_handler(request)
        if probe is not None:
            return probe
        patch_requests.append(request)
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
    assert len(patch_requests) == 2
    assert all(request.method == "PATCH" for request in patch_requests)
    assert all(request.url.path == "/v1/sessions/conv_current" for request in patch_requests)
    assert [json.loads(request.content) for request in patch_requests] == [
        {"title": "Debug auth timeout"},
        {"title": "Verify auth timeout fix"},
    ]


@pytest.mark.asyncio
async def test_session_rename_refuses_child_sessions() -> None:
    """A sub-agent must not rename itself — its title is its address.

    A child's ``(parent, title)`` pair is how ``sys_session_send``
    continuations find it, so a self-rename would corrupt sibling
    addressing. The dispatcher refuses before issuing any PATCH.
    """
    patch_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_child":
            return httpx.Response(
                200,
                json={"id": "conv_child", "parent_session_id": "conv_parent"},
            )
        patch_requests.append(request)
        return httpx.Response(200, json={"id": "conv_child", **json.loads(request.content)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "Debug auth timeout"}),
            server_client=server_client,
            conversation_id="conv_child",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {
        "renamed": False,
        "title": None,
        "reason": "not_top_level",
    }
    assert patch_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "info_payload",
    [
        ["unexpected"],
        {"id": "conv_current"},
        {"id": "conv_current", "parent_session_id": ""},
        {"id": "conv_current", "parent_session_id": 0},
    ],
    ids=["non-dict", "missing-parent-field", "empty-string-parent", "non-string-parent"],
)
async def test_session_rename_fails_closed_on_unverifiable_session(
    info_payload: object,
) -> None:
    """A snapshot that can't prove the session is top-level blocks the PATCH.

    A malformed or version-skewed GET payload must not be read as "no
    parent" — failing open here would let a child rename slip through and
    corrupt its continuation address.
    """
    patch_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=info_payload)
        patch_requests.append(request)
        return httpx.Response(200, json={"id": "conv_current", **json.loads(request.content)})

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

    assert "could not verify the session is top-level" in json.loads(output)["error"]
    assert patch_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "expected_error"),
    [
        ("x", f"2-{_TITLE_MAX_CHARS} characters"),
        ("  ", f"2-{_TITLE_MAX_CHARS} characters"),
        ("x" * (_TITLE_MAX_CHARS + 1), f"2-{_TITLE_MAX_CHARS} characters"),
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
async def test_session_rename_accepts_titles_up_to_the_generated_cap() -> None:
    """The dispatcher enforces exactly the cap the tool schema advertises."""
    title = "T" * _TITLE_MAX_CHARS

    def handler(request: httpx.Request) -> httpx.Response:
        probe = _top_level_session_handler(request)
        if probe is not None:
            return probe
        return httpx.Response(200, json={"id": "conv_current", **json.loads(request.content)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": title}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {"renamed": True, "title": title, "reason": None}


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

    def handler(request: httpx.Request) -> httpx.Response:
        probe = _top_level_session_handler(request)
        if probe is not None:
            return probe
        return response

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
