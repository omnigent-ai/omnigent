"""Compatibility and pagination contracts for discovery tools."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omnigent.runner.tool_dispatch import execute_tool

_ROW_COUNT = 12


def _agent_rows(*, large: bool) -> list[dict[str, object]]:
    return [
        {
            "id": f"ag_{index:02d}",
            "name": f"agent-{index:02d}",
            "description": "x" * 10_000 if large else f"Agent {index}",
            "harness": "claude-sdk",
        }
        for index in range(_ROW_COUNT)
    ]


def _session_rows(*, large: bool) -> list[dict[str, object]]:
    return [
        {
            "id": f"conv_{index:02d}",
            "agent_id": f"ag_{index:02d}",
            "agent_name": "researcher",
            "title": f"{'x' * 10_000}{index:02d}" if large else f"Session {index}",
            "status": "idle",
            "runner_id": None,
            "parent_session_id": None,
        }
        for index in range(_ROW_COUNT)
    ]


async def _agent_list_with_empty_server(
    tmp_path: Path, arguments: dict[str, object] | None = None
) -> str:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/v1/agents", "/v1/sessions"}:
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/v1/sessions/conv_caller":
            return httpx.Response(404)
        raise AssertionError(f"unexpected path {request.url.path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        return await execute_tool(
            tool_name="sys_agent_list",
            arguments=json.dumps(arguments or {}),
            server_client=client,
            conversation_id="conv_caller",
            runner_workspace=tmp_path,
        )


async def _session_list_with_rows(
    *, sessions: list[dict[str, object]], children: list[dict[str, object]]
) -> str:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions/conv_caller/child_sessions":
            return httpx.Response(200, json={"data": children})
        if request.url.path == "/v1/sessions/conv_caller":
            return httpx.Response(200, json={"id": "conv_caller", "parent_session_id": None})
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"data": sessions})
        raise AssertionError(f"unexpected path {request.url.path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        return await execute_tool(
            tool_name="sys_session_list",
            arguments="{}",
            server_client=client,
            conversation_id="conv_caller",
        )


@pytest.mark.asyncio
async def test_sys_agent_list_preserves_small_default_then_pages(tmp_path: Path) -> None:
    """Only a result above the output budget changes the parameterless response."""
    configs_dir = tmp_path / ".omnigent" / "agent-configs"
    configs_dir.mkdir(parents=True)
    for index in range(_ROW_COUNT):
        (configs_dir / f"local-{index:02d}.yaml").write_text(
            f"name: local-{index:02d}\ndescription: Local agent {index}\n",
            encoding="utf-8",
        )

    state = {"large": False}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": _agent_rows(large=state["large"])})
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"data": _session_rows(large=False)})
        if request.url.path == "/v1/sessions/conv_caller":
            return httpx.Response(404)
        raise AssertionError(f"unexpected path {request.url.path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        complete = json.loads(
            await execute_tool(
                tool_name="sys_agent_list",
                arguments="{}",
                server_client=client,
                conversation_id="conv_caller",
                runner_workspace=tmp_path,
            )
        )
        later = json.loads(
            await execute_tool(
                tool_name="sys_agent_list",
                arguments=json.dumps({"limit": 5, "offset": 5}),
                server_client=client,
                conversation_id="conv_caller",
                runner_workspace=tmp_path,
            )
        )
        state["large"] = True
        large_raw = await execute_tool(
            tool_name="sys_agent_list",
            arguments="{}",
            server_client=client,
            conversation_id="conv_caller",
            runner_workspace=tmp_path,
        )

    assert len(complete["builtins"]) == _ROW_COUNT
    assert len(complete["session_agents"]) == _ROW_COUNT
    assert len(complete["local_configs"]) == _ROW_COUNT
    assert "page" not in complete
    assert [row["agent_id"] for row in later["builtins"]] == [
        f"ag_{index:02d}" for index in range(5, 10)
    ]
    assert later["page"] == {
        "limit": 5,
        "offset": 5,
        "has_more": {"builtins": True, "session_agents": True, "local_configs": True},
    }

    large = json.loads(large_raw)
    assert len(large_raw) <= 100_000
    assert 0 < len(large["builtins"]) < _ROW_COUNT
    assert large["page"]["limit"] == len(large["builtins"])
    assert large["page"]["has_more"]["builtins"] is True


@pytest.mark.asyncio
async def test_sys_session_list_preserves_small_default_then_pages() -> None:
    """A large global view pages without hiding the caller's direct children."""
    state = {"large": False}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions/conv_caller/child_sessions":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "conv_child",
                            "title": "researcher:catalog",
                            "tool": "researcher",
                            "session_name": "catalog",
                        }
                    ]
                },
            )
        if request.url.path == "/v1/sessions/conv_caller":
            return httpx.Response(200, json={"id": "conv_caller", "parent_session_id": None})
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"data": _session_rows(large=state["large"])})
        raise AssertionError(f"unexpected path {request.url.path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        complete = json.loads(
            await execute_tool(
                tool_name="sys_session_list",
                arguments="{}",
                server_client=client,
                conversation_id="conv_caller",
            )
        )
        later = json.loads(
            await execute_tool(
                tool_name="sys_session_list",
                arguments=json.dumps({"limit": 5, "offset": 5}),
                server_client=client,
                conversation_id="conv_caller",
            )
        )
        state["large"] = True
        large_raw = await execute_tool(
            tool_name="sys_session_list",
            arguments="{}",
            server_client=client,
            conversation_id="conv_caller",
        )

    child = {"agent": "researcher", "title": "catalog", "conversation_id": "conv_child"}
    assert len(complete["sessions"]) == _ROW_COUNT
    assert complete["sub_agents"] == [child]
    assert "page" not in complete
    assert [row["session_id"] for row in later["sessions"]] == [
        f"conv_{index:02d}" for index in range(5, 10)
    ]
    assert later["sub_agents"] == [child]
    assert later["page"] == {
        "limit": 5,
        "offset": 5,
        "has_more": {"sessions": True},
    }

    large = json.loads(large_raw)
    assert len(large_raw) <= 100_000
    assert large["sub_agents"] == [child]
    assert 0 < len(large["sessions"]) < _ROW_COUNT
    assert large["page"]["limit"] == len(large["sessions"])
    assert large["page"]["has_more"] == {"sessions": True}


@pytest.mark.asyncio
async def test_sys_agent_list_reports_oversized_local_config_row(tmp_path: Path) -> None:
    """A single local row cannot escape the discovery output budget."""
    configs_dir = tmp_path / ".omnigent" / "agent-configs"
    configs_dir.mkdir(parents=True)
    (configs_dir / "oversized.yaml").write_text(
        f"name: oversized\ndescription: {'x' * 110_000}\n",
        encoding="utf-8",
    )

    raw = await _agent_list_with_empty_server(tmp_path)

    result = json.loads(raw)
    assert len(raw) <= 100_000
    assert result["oversized"] == {
        "kind": "paginated_row",
        "sections": ["local_configs"],
    }


@pytest.mark.asyncio
async def test_sys_session_list_reports_oversized_session_row() -> None:
    """A single session row cannot escape the discovery output budget."""

    row = _session_rows(large=False)[0]
    row["title"] = "x" * 110_000
    raw = await _session_list_with_rows(sessions=[row], children=[])

    result = json.loads(raw)
    assert len(raw) <= 100_000
    assert result["oversized"] == {
        "kind": "paginated_row",
        "sections": ["sessions"],
    }


@pytest.mark.asyncio
async def test_sys_session_list_reports_oversized_fixed_sub_agents() -> None:
    """An unpaged child view cannot escape the discovery output budget."""

    raw = await _session_list_with_rows(
        sessions=[],
        children=[
            {
                "id": "conv_child",
                "title": f"researcher:{'x' * 110_000}",
                "tool": "researcher",
                "session_name": "x" * 110_000,
            }
        ],
    )

    result = json.loads(raw)
    assert len(raw) <= 100_000
    assert result["oversized"] == {
        "kind": "fixed_section",
        "sections": ["sub_agents"],
    }


@pytest.mark.asyncio
async def test_sys_agent_list_pages_local_configs_beyond_server_fetch_limit(
    tmp_path: Path,
) -> None:
    """The server fetch size does not cap local-config pagination."""
    configs_dir = tmp_path / ".omnigent" / "agent-configs"
    configs_dir.mkdir(parents=True)
    for index in range(1_001):
        (configs_dir / f"local-{index:04d}.yaml").write_text(
            f"name: local-{index:04d}\n",
            encoding="utf-8",
        )

    result = json.loads(
        await _agent_list_with_empty_server(tmp_path, {"limit": 100, "offset": 1_000})
    )

    assert [row["name"] for row in result["local_configs"]] == ["local-1000"]
    assert result["page"] == {
        "limit": 100,
        "offset": 1_000,
        "has_more": {"builtins": False, "session_agents": False, "local_configs": False},
    }


@pytest.mark.parametrize(
    ("tool_name", "arguments", "error"),
    [
        ("sys_agent_list", {"limit": 0}, "'limit' must be an integer between 1 and 100"),
        ("sys_agent_list", {"limit": None}, "'limit' must be an integer between 1 and 100"),
        ("sys_session_list", {"offset": -1}, "'offset' must be a non-negative integer"),
    ],
)
@pytest.mark.asyncio
async def test_discovery_list_rejects_invalid_windows(
    tool_name: str,
    arguments: dict[str, object],
    error: str,
) -> None:
    """Invalid windows fail before a server request."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name=tool_name,
                arguments=json.dumps(arguments),
                server_client=client,
                conversation_id="conv_caller",
            )
        )

    assert error in result["error"]
