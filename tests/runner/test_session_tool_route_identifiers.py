"""Session tool arguments remain one REST route identifier."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner import subagent_work
from omnigent.runner.tool_dispatch import (
    _execute_agent_tool,
    _execute_rest_tool,
    _execute_session_query_tool,
    _execute_task_lifecycle_tool,
    _inherited_parent_model,
    _post_session_stop,
    _send_to_existing_session,
    _session_turn_actor,
)
from omnigent.spec.types import AgentSpec, SharePolicy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    [
        "sys_session_get_info",
        "sys_session_get_history",
        "sys_session_close",
        "sys_session_share",
        "sys_session_send",
        "sys_agent_get",
        "sys_agent_download",
        "sys_cancel_async",
        "sys_cancel_task",
    ],
)
@pytest.mark.parametrize("identifier", ["notes?draft", "notes#draft", "two/parts", ".", "%20"])
async def test_session_tool_refuses_non_identifier_before_http(
    tool_name: str,
    identifier: str,
) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(404)

    args = {
        "session_id": identifier,
        "conversation_id": identifier,
        "handle_id": identifier,
        "task_id": identifier,
        "user_id": "reader@example.com",
        "level": "read",
    }
    async with httpx.AsyncClient(
        base_url="https://server.example",
        transport=httpx.MockTransport(respond),
    ) as client:
        if tool_name.startswith("sys_agent_"):
            result = await _execute_agent_tool(
                tool_name,
                args,
                server_client=client,
                agent_spec=None,
                conversation_id="conv_parent",
                runner_workspace=None,
            )
        elif tool_name == "sys_session_send":
            result = await _send_to_existing_session(
                identifier,
                "Continue the task",
                server_client=client,
                conversation_id="conv_parent",
            )
        elif tool_name == "sys_cancel_async":
            result = await _execute_rest_tool(tool_name, args, client)
        elif tool_name == "sys_cancel_task":
            result = await _execute_task_lifecycle_tool(
                args,
                session_async_tasks={},
                conversation_id="conv_parent",
                server_client=client,
            )
        else:
            result = await _execute_session_query_tool(
                tool_name,
                json.dumps(args),
                conversation_id="conv_parent",
                server_client=client,
                agent_spec=AgentSpec(spec_version=1, agent_session_sharing=SharePolicy.NON_PUBLIC),
            )
    assert "Session id must contain" in result
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["a" * 32, "conv_legacy-1"])
async def test_session_tool_preserves_supported_identifiers(identifier: str) -> None:
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(404)

    async with httpx.AsyncClient(
        base_url="https://server.example",
        transport=httpx.MockTransport(respond),
    ) as client:
        result = await _execute_session_query_tool(
            "sys_session_get_info",
            json.dumps({"session_id": identifier}),
            conversation_id="conv_parent",
            server_client=client,
        )
    assert json.loads(result)["error"] == "session_not_found"
    assert paths == [f"/v1/sessions/{identifier}"]


@pytest.mark.asyncio
async def test_optional_parent_metadata_ignores_invalid_identifier() -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(404)

    async with httpx.AsyncClient(
        base_url="https://server.example", transport=httpx.MockTransport(respond)
    ) as client:
        assert await _session_turn_actor(server_client=client, conversation_id="a.b") is None
        assert (
            await _inherited_parent_model(
                server_client=client,
                conversation_id="a.b",
                sub_agent_name="worker",
                agent_spec=None,
                child_harness="claude-sdk",
            )
            is None
        )
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent_id", "child_id"),
    [("a.b", "conv_child"), ("conv_parent", "a.b"), ("conv_parent", "conv_child")],
)
async def test_async_creation_preserves_optional_binding(parent_id: str, child_id: str) -> None:
    calls: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/v1/sessions":
            return httpx.Response(201, json={"id": child_id})
        if request.method == "GET":
            return httpx.Response(200, json={"runner_id": "runner_test"})
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        base_url="https://server.example", transport=httpx.MockTransport(respond)
    ) as client:
        result = await _execute_rest_tool(
            "sys_call_async",
            {"prompt": "hello"},
            client,
            agent_id="agent_test",
            conversation_id=parent_id,
        )
    expected = [("POST", "/v1/sessions")]
    if parent_id == "conv_parent":
        expected.append(("GET", "/v1/sessions/conv_parent"))
        if child_id == "conv_child":
            expected.append(("PATCH", "/v1/sessions/conv_child"))
    if child_id == "conv_child":
        expected.append(("POST", "/v1/sessions/conv_child/events"))
        assert json.loads(result)["handle_id"] == child_id
    else:
        assert "Session id must contain" in result
    assert calls == expected


@pytest.mark.asyncio
async def test_registered_cancel_and_hard_stop_refuse_invalid_identifier() -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(404)

    subagent_work.register_subagent_work(
        parent_session_id="conv_parent", child_session_id="a.b", agent="worker", title="work"
    )
    try:
        async with httpx.AsyncClient(
            base_url="https://server.example", transport=httpx.MockTransport(respond)
        ) as client:
            result = await _execute_task_lifecycle_tool(
                {"task_id": "a.b"},
                session_async_tasks={},
                conversation_id="conv_parent",
                server_client=client,
            )
            assert "Session id must contain" in result
            status, error = await _post_session_stop(client, "a.b")
            assert status is None
            assert error is not None and "Session id must contain" in error
    finally:
        subagent_work.unregister_subagent_work("a.b")
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["download_file", "update_comment"])
@pytest.mark.parametrize(
    "identifier", ["notes?draft", "notes#draft", "two/parts", ".", "%20", "file_ok-1"]
)
async def test_file_and_comment_ids_remain_single_segments(tool: str, identifier: str) -> None:
    from omnigent.runner.tool_dispatch import _execute_comment_tool, _execute_file_tool
    from omnigent.tools.builtins.download_file import DownloadFileTool

    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"id": identifier})

    async with httpx.AsyncClient(
        base_url="https://server.example", transport=httpx.MockTransport(respond)
    ) as client:
        if tool == "download_file":
            result = await _execute_file_tool(
                DownloadFileTool.name(),
                {"file_id": identifier},
                client,
                conversation_id="conv_parent",
            )
            expected = ("GET", f"/v1/sessions/conv_parent/resources/files/{identifier}/content")
        else:
            result = await _execute_comment_tool(
                "update_comment",
                json.dumps({"comment_id": identifier, "status": "addressed"}),
                conversation_id="conv_parent",
                server_client=client,
            )
            expected = ("PATCH", f"/v1/sessions/conv_parent/comments/{identifier}")
    if identifier == "file_ok-1":
        assert calls == [expected]
        assert identifier in result
    else:
        assert calls == []
        assert "id must contain" in result
