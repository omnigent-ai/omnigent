"""Agent discovery distinguishes catalog membership from host readiness."""

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import execute_tool


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report", [None, {}, {"codex-native": True, "jcode": False}, {"codex-native": True}]
)
async def test_agent_list_inherits_host_and_reports_availability(report: dict | None) -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/agents":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "ag_jcode", "name": "jcode", "harness": "jcode"},
                        {"id": "ag_codex", "name": "codex", "harness": "codex-native"},
                    ]
                },
            )
        if path == "/v1/sessions":
            return httpx.Response(200, json={"data": []})
        if path == "/v1/sessions/child":
            return httpx.Response(200, json={"parent_session_id": "parent", "host_id": None})
        if path == "/v1/sessions/parent":
            return httpx.Response(200, json={"host_id": "host_test"})
        if path == "/v1/hosts/host_test":
            return httpx.Response(200, json={"configured_harnesses": report})
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_agent_list",
                arguments="{}",
                server_client=client,
                conversation_id="child",
            )
        )
    jcode, codex = result["builtins"]
    assert jcode["available_on_host"] is (False if report else None)
    assert jcode["unavailable_reason"] == ("unconfigured" if report else None)
    assert codex["available_on_host"] is (True if report else None)


@pytest.fixture(autouse=True)
def isolate_runner_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.runner.identity import RUNNER_SLICE_KEY_ENV_VAR

    monkeypatch.delenv(RUNNER_SLICE_KEY_ENV_VAR, raising=False)


@pytest.mark.asyncio
async def test_runner_identity_skips_session_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.runner.identity import RUNNER_SLICE_KEY_ENV_VAR
    from omnigent.runner.tool_dispatch import _agent_list_host_readiness

    monkeypatch.setenv(RUNNER_SLICE_KEY_ENV_VAR, "host_test")
    paths = []

    async def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"configured_harnesses": {"jcode": False}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://server"
    ) as client:
        assert await _agent_list_host_readiness(client, "nested-child") == {"jcode": False}
    assert paths == ["/v1/hosts/host_test"]


@pytest.mark.asyncio
async def test_legacy_walk_has_depth_limit() -> None:
    from omnigent.runner.tool_dispatch import (
        _AGENT_READINESS_MAX_DEPTH,
        _agent_list_host_readiness,
    )

    paths = []

    async def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"parent_session_id": str(len(paths))})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://server"
    ) as client:
        assert await _agent_list_host_readiness(client, "0") is None
    assert len(paths) == _AGENT_READINESS_MAX_DEPTH


@pytest.mark.asyncio
async def test_readiness_has_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from omnigent.runner import tool_dispatch

    monkeypatch.setattr(tool_dispatch, "_AGENT_READINESS_TIMEOUT_S", 0.01)
    cancelled = asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return httpx.Response(200)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://server"
    ) as client:
        assert await tool_dispatch._agent_list_host_readiness(client, "child") is None
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_discovery_reads_run_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from omnigent.runner.identity import RUNNER_SLICE_KEY_ENV_VAR

    monkeypatch.setenv(RUNNER_SLICE_KEY_ENV_VAR, "host_test")
    arrived: set[str] = set()
    all_arrived = asyncio.Event()
    timeouts: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        arrived.add(request.url.path)
        if len(arrived) == 3:
            all_arrived.set()
        try:
            await asyncio.wait_for(all_arrived.wait(), timeout=1.0)
        except TimeoutError:
            timeouts.append(request.url.path)
            raise
        if request.url.path == "/v1/hosts/host_test":
            return httpx.Response(200, json={"configured_harnesses": {"jcode": False}})
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://server"
    ) as client:
        await execute_tool(
            tool_name="sys_agent_list",
            arguments="{}",
            server_client=client,
            conversation_id="child",
        )
    assert not timeouts
    assert arrived == {"/v1/agents", "/v1/sessions", "/v1/hosts/host_test"}
