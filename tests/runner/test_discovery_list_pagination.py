"""Compatibility and pagination contracts for discovery tools."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from omnigent.runner import tool_dispatch
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


def _server_page(
    request: httpx.Request,
    rows: list[dict[str, object]],
) -> httpx.Response:
    """Return a cursor page from *rows*, honoring the server's ``after`` contract."""
    after = request.url.params.get("after")
    if after is not None:
        start = next(index + 1 for index, row in enumerate(rows) if row["id"] == after)
        rows = rows[start:]
    limit = int(request.url.params["limit"])
    page = rows[:limit]
    return httpx.Response(
        200,
        json={
            "data": page,
            "has_more": len(rows) > len(page),
            "last_id": page[-1]["id"] if page else None,
        },
    )


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


async def _call_agent(
    client: httpx.AsyncClient,
    tmp_path: Path,
    arguments: dict[str, object],
) -> dict[str, object]:
    return json.loads(
        await execute_tool(
            tool_name="sys_agent_list",
            arguments=json.dumps(arguments),
            server_client=client,
            conversation_id="conv_caller",
            runner_workspace=tmp_path,
        )
    )


async def _call_session(
    client: httpx.AsyncClient,
    arguments: dict[str, object],
) -> dict[str, object]:
    return json.loads(
        await execute_tool(
            tool_name="sys_session_list",
            arguments=json.dumps(arguments),
            server_client=client,
            conversation_id="conv_caller",
        )
    )


def _encode_cursor_payload(payload: object) -> str:
    return (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )


@pytest.mark.asyncio
async def test_size_fitter_checks_larger_page_after_smaller_cursor_does_not_fit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A long intermediate cursor must not hide a larger page that fits."""
    monkeypatch.setattr(
        tool_dispatch,
        "_scan_local_agent_configs",
        lambda _path: [
            {
                "name": "large",
                "path": "/workspace/a.yaml",
                "description": "d" * 96_000,
            },
            {"name": "small", "path": "/workspace/z.yaml", "description": "small"},
        ],
    )
    monkeypatch.setattr(
        tool_dispatch,
        "_encode_discovery_cursor",
        lambda *args: (
            "x" * 5_000 if args[-1]["local_configs"] in {1, ("at", "/workspace/a.yaml")} else "x"
        ),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            return _server_page(
                request,
                [
                    {
                        "id": "ag_fixed",
                        "name": "fixed",
                        "description": "f" * 1_000,
                        "harness": "claude-sdk",
                    }
                ],
            )
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/v1/sessions/conv_caller":
            return httpx.Response(404)
        raise AssertionError(f"unexpected path {request.url.path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        payload = await _call_agent(client, tmp_path, {"limit": 2})

    assert "error" not in payload
    assert payload["page"]["limit"] == 2
    assert [row["name"] for row in payload["local_configs"]] == ["large", "small"]
    assert len(json.dumps(payload)) <= 100_000


@pytest.mark.asyncio
async def test_failed_first_source_read_remains_retryable(tmp_path: Path) -> None:
    """A source that did not answer is pending, not exhausted."""
    agents_status = 503

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            if agents_status != 200:
                return httpx.Response(agents_status)
            return _server_page(request, _agent_rows(large=False))
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/v1/sessions/conv_caller":
            return httpx.Response(404)
        raise AssertionError(f"unexpected path {request.url.path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        first = await _call_agent(client, tmp_path, {"limit": 3})
        agents_status = 200
        second = await _call_agent(
            client,
            tmp_path,
            {"limit": 3, "cursor": first["page"]["next_cursor"]},
        )

    assert first["builtins"] == []
    assert first["page"]["has_more"]["builtins"] is True
    assert [row["agent_id"] for row in second["builtins"]] == [
        "ag_00",
        "ag_01",
        "ag_02",
    ]


@pytest.mark.asyncio
async def test_exhausted_source_is_not_refetched_while_other_source_continues(
    tmp_path: Path,
) -> None:
    """An exhausted section stays distinct from an unread failed section."""
    agent_reads = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal agent_reads
        if request.url.path == "/v1/agents":
            agent_reads += 1
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/v1/sessions":
            return _server_page(request, _session_rows(large=False))
        if request.url.path == "/v1/sessions/conv_caller":
            return httpx.Response(404)
        raise AssertionError(f"unexpected path {request.url.path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        first = await _call_agent(client, tmp_path, {"limit": 3})
        await _call_agent(
            client,
            tmp_path,
            {"limit": 3, "cursor": first["page"]["next_cursor"]},
        )

    assert agent_reads == 1


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
            return _server_page(request, _agent_rows(large=state["large"]))
        if request.url.path == "/v1/sessions":
            return _server_page(request, _session_rows(large=False))
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
        first_page = json.loads(
            await execute_tool(
                tool_name="sys_agent_list",
                arguments=json.dumps({"limit": 5}),
                server_client=client,
                conversation_id="conv_caller",
                runner_workspace=tmp_path,
            )
        )
        later = json.loads(
            await execute_tool(
                tool_name="sys_agent_list",
                arguments=json.dumps({"limit": 5, "cursor": first_page["page"]["next_cursor"]}),
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
    assert later["page"]["limit"] == 5
    assert later["page"]["has_more"] == {
        "builtins": True,
        "session_agents": True,
        "local_configs": True,
    }
    assert "next_cursor" in later["page"]

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
            return _server_page(request, _session_rows(large=state["large"]))
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
        first_page = json.loads(
            await execute_tool(
                tool_name="sys_session_list",
                arguments=json.dumps({"limit": 5}),
                server_client=client,
                conversation_id="conv_caller",
            )
        )
        later = json.loads(
            await execute_tool(
                tool_name="sys_session_list",
                arguments=json.dumps({"limit": 5, "cursor": first_page["page"]["next_cursor"]}),
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
    assert later["page"]["limit"] == 5
    assert later["page"]["has_more"] == {"sessions": True}
    assert "next_cursor" in later["page"]

    large = json.loads(large_raw)
    assert len(large_raw) <= 100_000
    assert large["sub_agents"] == [child]
    assert 0 < len(large["sessions"]) < _ROW_COUNT
    assert large["page"]["limit"] == len(large["sessions"])
    assert large["page"]["has_more"] == {"sessions": True}


@pytest.mark.asyncio
async def test_sys_session_list_continues_server_catalog_with_cursor() -> None:
    """The tool forwards the opaque continuation into the server's cursor API."""
    rows = [
        {
            "id": f"conv_{index:04d}",
            "agent_id": f"ag_{index:04d}",
            "agent_name": "researcher",
            "title": f"Session {index}",
            "status": "idle",
            "runner_id": None,
            "parent_session_id": None,
        }
        for index in range(1_001)
    ]
    received_afters: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions/conv_caller/child_sessions":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/v1/sessions/conv_caller":
            return httpx.Response(200, json={"id": "conv_caller", "parent_session_id": None})
        if request.url.path == "/v1/sessions":
            received_afters.append(request.url.params.get("after"))
            return _server_page(request, rows)
        raise AssertionError(f"unexpected path {request.url.path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        first = json.loads(
            await execute_tool(
                tool_name="sys_session_list",
                arguments=json.dumps({"limit": 100}),
                server_client=client,
                conversation_id="conv_caller",
            )
        )
        second = json.loads(
            await execute_tool(
                tool_name="sys_session_list",
                arguments=json.dumps({"limit": 100, "cursor": first["page"]["next_cursor"]}),
                server_client=client,
                conversation_id="conv_caller",
            )
        )

    assert first["sessions"][0]["session_id"] == "conv_0000"
    assert second["sessions"][0]["session_id"] == "conv_0100"
    assert received_afters == [None, "conv_0099"]


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

    cursor: str | None = None
    for _ in range(11):
        arguments: dict[str, object] = {"limit": 100}
        if cursor is not None:
            arguments["cursor"] = cursor
        result = json.loads(await _agent_list_with_empty_server(tmp_path, arguments))
        cursor = result["page"].get("next_cursor")
        if cursor is None:
            break

    assert [row["name"] for row in result["local_configs"]] == ["local-1000"]
    assert result["page"] == {
        "limit": 100,
        "has_more": {"builtins": False, "session_agents": False, "local_configs": False},
    }


@pytest.mark.asyncio
async def test_local_config_continuation_uses_last_returned_path(tmp_path: Path) -> None:
    """Edits before the resume path do not skip or repeat later configs."""
    configs_dir = tmp_path / ".omnigent" / "agent-configs"
    configs_dir.mkdir(parents=True)
    for index in range(6):
        (configs_dir / f"cfg-{index}.yaml").write_text(
            f"name: cfg-{index}\n",
            encoding="utf-8",
        )

    first = json.loads(await _agent_list_with_empty_server(tmp_path, {"limit": 3}))
    (configs_dir / "cfg-0.yaml").unlink()
    second = json.loads(
        await _agent_list_with_empty_server(
            tmp_path,
            {"limit": 3, "cursor": first["page"]["next_cursor"]},
        )
    )

    assert [row["name"] for row in first["local_configs"]] == ["cfg-0", "cfg-1", "cfg-2"]
    assert [row["name"] for row in second["local_configs"]] == ["cfg-3", "cfg-4", "cfg-5"]


@pytest.mark.asyncio
async def test_cursor_is_bound_to_tool_sections_and_filters(tmp_path: Path) -> None:
    """A cursor cannot be replayed under a different listing contract."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            return _server_page(request, _agent_rows(large=False))
        if request.url.path == "/v1/sessions":
            return _server_page(request, _session_rows(large=False))
        if request.url.path == "/v1/sessions/conv_caller/child_sessions":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/v1/sessions/conv_caller":
            return httpx.Response(200, json={"id": "conv_caller", "parent_session_id": None})
        raise AssertionError(f"unexpected path {request.url.path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        agent_page = await _call_agent(client, tmp_path, {"limit": 3})
        wrong_tool = await _call_session(
            client,
            {"limit": 3, "cursor": agent_page["page"]["next_cursor"]},
        )
        session_page = await _call_session(
            client,
            {"limit": 3, "agent_name": "researcher"},
        )
        wrong_filter = await _call_session(
            client,
            {
                "limit": 3,
                "agent_name": "planner",
                "cursor": session_page["page"]["next_cursor"],
            },
        )

    assert "different tool" in wrong_tool["error"]
    assert "different filter" in wrong_filter["error"]


@pytest.mark.asyncio
async def test_cursor_envelope_is_strict_and_bounded(tmp_path: Path) -> None:
    """Unknown fields, malformed state, and oversized cursors are rejected."""
    payload = {
        "v": 1,
        "tool": "sys_agent_list",
        "filters": {},
        "sections": {
            "builtins": ["start", None],
            "session_agents": ["start", None],
            "local_configs": ["start", None],
        },
    }

    payload["unknown"] = True
    unknown = _encode_cursor_payload(payload)
    payload.pop("unknown")
    payload["sections"].pop("session_agents")
    missing_section = _encode_cursor_payload(payload)
    malformed = _encode_cursor_payload(
        {
            "v": 1,
            "tool": "sys_agent_list",
            "filters": {},
            "sections": {
                "builtins": [],
                "session_agents": None,
                "local_configs": None,
            },
        }
    )

    unknown_result = json.loads(await _agent_list_with_empty_server(tmp_path, {"cursor": unknown}))
    malformed_result = json.loads(
        await _agent_list_with_empty_server(tmp_path, {"cursor": malformed})
    )
    missing_section_result = json.loads(
        await _agent_list_with_empty_server(tmp_path, {"cursor": missing_section})
    )
    oversized_result = json.loads(
        await _agent_list_with_empty_server(tmp_path, {"cursor": "a" * 40_001})
    )

    assert "invalid pagination cursor" in unknown_result["error"]
    assert "invalid pagination cursor" in missing_section_result["error"]
    assert "invalid pagination cursor" in malformed_result["error"]
    assert "too long" in oversized_result["error"]


@pytest.mark.parametrize(
    "broken_response",
    [
        pytest.param(httpx.Response(503), id="failed-status"),
        pytest.param(httpx.Response(200, text="not json"), id="malformed-json"),
        pytest.param(
            httpx.Response(
                200,
                json={"data": [], "has_more": "false", "last_id": None},
            ),
            id="invalid-envelope",
        ),
    ],
)
@pytest.mark.asyncio
async def test_unusable_server_envelope_remains_retryable(
    tmp_path: Path,
    broken_response: httpx.Response,
) -> None:
    """An unusable 200 response does not falsely exhaust its source."""
    broken = False

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            return broken_response if broken else _server_page(request, _agent_rows(large=False))
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/v1/sessions/conv_caller":
            return httpx.Response(404)
        raise AssertionError(f"unexpected path {request.url.path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        first = await _call_agent(client, tmp_path, {"limit": 3})
        broken = True
        failed = await _call_agent(
            client,
            tmp_path,
            {"limit": 3, "cursor": first["page"]["next_cursor"]},
        )
        broken = False
        retried = await _call_agent(
            client,
            tmp_path,
            {"limit": 3, "cursor": failed["page"]["next_cursor"]},
        )

    assert failed["builtins"] == []
    assert failed["page"]["has_more"]["builtins"] is True
    assert [row["agent_id"] for row in retried["builtins"]] == ["ag_03", "ag_04", "ag_05"]


@pytest.mark.parametrize(
    ("tool_name", "arguments", "error"),
    [
        ("sys_agent_list", {"limit": 0}, "'limit' must be an integer between 1 and 100"),
        ("sys_agent_list", {"limit": None}, "'limit' must be an integer between 1 and 100"),
        ("sys_session_list", {"cursor": "not-a-cursor"}, "invalid pagination cursor"),
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
