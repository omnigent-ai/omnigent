"""
Tests for the ``sys_worktree_*`` tools' runner-side dispatch.

These tools exist so an agent stops inventing worktree directories with a raw
``git worktree add``, so the contract that matters here is that they reach the
SERVER (which owns the project's worktree location and lifecycle scripts) rather
than running git locally, that no repository path crosses the boundary, and that
the scripts' outcome comes back in the tool result — the one place the agent can
actually see it.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.runner.tool_dispatch import (
    _NATIVE_RELAY_BUILTIN_TOOLS,
    execute_tool,
    should_dispatch_locally,
)


def _recording_client(
    response: httpx.Response,
    seen: list[httpx.Request],
) -> httpx.AsyncClient:
    """Build a client that records each request and replies with ``response``.

    :param response: The canned reply.
    :param seen: List the handler appends every request to.
    :returns: An httpx client over a mock transport.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response

    return httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://server",
    )


async def _call(
    tool_name: str,
    args: dict[str, Any],
    response: httpx.Response,
) -> tuple[dict[str, Any], list[httpx.Request]]:
    """Run one worktree tool against a canned server response.

    :param tool_name: The tool to dispatch.
    :param args: Tool arguments.
    :param response: What the server replies.
    :returns: The parsed tool output and the requests the runner made.
    """
    seen: list[httpx.Request] = []
    async with _recording_client(response, seen) as server_client:
        output = await execute_tool(
            tool_name=tool_name,
            arguments=json.dumps(args),
            server_client=server_client,
            conversation_id="conv_caller",
        )
    return json.loads(output), seen


def test_worktree_tools_dispatch_locally() -> None:
    """Both must be in the runner's local dispatch table.

    Otherwise a native harness's call falls through to spec-callable
    resolution and the agent is told the tool does not exist — and goes
    back to shelling out to git.
    """
    assert should_dispatch_locally("sys_worktree_create") is True
    assert should_dispatch_locally("sys_worktree_remove") is True


def test_worktree_tools_reach_native_harnesses() -> None:
    """The native relay surface must carry them.

    The orchestrator that fans out worktrees (polly) runs under a native
    harness, which sees only this surface.
    """
    assert {"sys_worktree_create", "sys_worktree_remove"} <= _NATIVE_RELAY_BUILTIN_TOOLS


@pytest.mark.asyncio
async def test_create_posts_to_the_session_and_sends_no_repo_path() -> None:
    """The repo is never a parameter — the server takes it from the session."""
    body, seen = await _call(
        "sys_worktree_create",
        {"branch_name": "polly/task-1", "base_branch": "main"},
        httpx.Response(
            200,
            json={
                "object": "worktree",
                "worktree_path": "/repo/.worktrees/polly-task-1",
                "branch": "polly/task-1",
                "setup": None,
            },
        ),
    )
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/sessions/conv_caller/worktrees"
    sent = json.loads(request.content)
    assert sent == {"branch_name": "polly/task-1", "base_branch": "main"}
    assert body["worktree_path"] == "/repo/.worktrees/polly-task-1"


@pytest.mark.asyncio
async def test_create_returns_the_setup_script_result_verbatim() -> None:
    """The setup outcome is the reason this tool result matters to the agent."""
    setup = {
        "ran": True,
        "ok": False,
        "exit_code": 1,
        "timed_out": False,
        "output_tail": "error: lockfile conflict\n",
        "error": None,
    }
    body, _seen = await _call(
        "sys_worktree_create",
        {"branch_name": "task"},
        httpx.Response(
            200,
            json={
                "object": "worktree",
                "worktree_path": "/repo/.worktrees/task",
                "branch": "task",
                "setup": setup,
            },
        ),
    )
    assert body["setup"] == setup


@pytest.mark.asyncio
async def test_create_omits_a_blank_base_branch() -> None:
    """A blank base means "branch from HEAD", not a ref named ""."""
    _body, seen = await _call(
        "sys_worktree_create",
        {"branch_name": "task", "base_branch": "   "},
        httpx.Response(200, json={"worktree_path": "/repo/wt/task", "branch": "task"}),
    )
    assert json.loads(seen[0].content)["base_branch"] is None


@pytest.mark.asyncio
async def test_create_requires_a_branch_name_before_calling_the_server() -> None:
    """A malformed call must not produce a request at all."""
    body, seen = await _call(
        "sys_worktree_create",
        {},
        httpx.Response(200, json={}),
    )
    assert "branch_name" in body["error"]
    assert seen == []


@pytest.mark.asyncio
async def test_remove_sends_a_delete_with_the_path_and_default_keeps_the_branch() -> None:
    """Removing a worktree must not silently drop unpushed work."""
    body, seen = await _call(
        "sys_worktree_remove",
        {"worktree_path": "/repo/.worktrees/task"},
        httpx.Response(
            200,
            json={
                "object": "worktree.deleted",
                "worktree_path": "/repo/.worktrees/task",
                "deleted": True,
                "teardown": None,
            },
        ),
    )
    assert len(seen) == 1
    assert seen[0].method == "DELETE"
    assert seen[0].url.path == "/v1/sessions/conv_caller/worktrees"
    assert json.loads(seen[0].content) == {
        "worktree_path": "/repo/.worktrees/task",
        "delete_branch": False,
    }
    assert body["deleted"] is True


@pytest.mark.asyncio
async def test_remove_requires_a_path_before_calling_the_server() -> None:
    """No path means nothing to remove — and nothing to send."""
    body, seen = await _call(
        "sys_worktree_remove",
        {"delete_branch": True},
        httpx.Response(200, json={}),
    )
    assert "worktree_path" in body["error"]
    assert seen == []


@pytest.mark.asyncio
async def test_server_refusal_surfaces_its_own_explanation() -> None:
    """The agent needs the reason (bad branch, not a worktree), not a status code."""
    body, _seen = await _call(
        "sys_worktree_remove",
        {"worktree_path": "/etc"},
        httpx.Response(
            400,
            json={
                "error": {
                    "code": "invalid_input",
                    "message": "'/etc' is not a removable worktree of this session's repository",
                }
            },
        ),
    )
    assert body["error"] == ("'/etc' is not a removable worktree of this session's repository")


@pytest.mark.asyncio
async def test_missing_session_is_reported_as_such() -> None:
    """A 404 is mapped to a name the agent can act on."""
    body, _seen = await _call(
        "sys_worktree_create",
        {"branch_name": "task"},
        httpx.Response(404, json={}),
    )
    assert body["error"] == "session_not_found"


@pytest.mark.asyncio
async def test_denied_access_is_reported_as_such() -> None:
    """A 403 means this session may not branch its repo, not that it failed."""
    body, _seen = await _call(
        "sys_worktree_remove",
        {"worktree_path": "/repo/wt/task"},
        httpx.Response(403, json={}),
    )
    assert body["error"] == "access_denied"
