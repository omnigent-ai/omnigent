"""Integration tests for the MCP proxy endpoint ``POST /v1/sessions/{id}/mcp``.

Covers JSON-RPC validation, routing and error handling with the shared server
client. Background-shell cases connect the real runner MCP routes and execute
a local shell; other cases need no runner.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runtime import get_caps, get_conversation_store
from omnigent.server.routes import sessions as sessions_mod
from omnigent.spec.parser import parse
from omnigent.spec.types import FunctionPolicySpec, FunctionRef
from tests._helpers.runner_mcp import RunnerMcpHarness
from tests._helpers.session import bundle_files, post_session_bundle
from tests.server.helpers import create_test_session

pytestmark = pytest.mark.asyncio


@pytest.mark.posix_only
@pytest.mark.parametrize(("result_action", "times_out"), [("ALLOW", False), ("DENY", True)])
async def test_background_shell_keeps_execution_and_policies_across_http(
    app,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    result_action: str,
    times_out: bool,
) -> None:
    monkeypatch.setattr(app.state, "_shell_result_signing_key", b"test-shell-key".ljust(32, b"_"))
    script = (
        "import time; from pathlib import Path; "
        "Path('starts').open('a').write('started\\n'); "
        "deadline = time.monotonic() + 10\n"
        "while not Path('release').exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
        "print('PRIVATE OUTPUT')"
    )
    approved = {
        "command": f"exec {shlex.quote(sys.executable)} -c {shlex.quote(script)}",
        "timeout": 1 if times_out else 15,
    }
    shell_events = []

    def policy(event: dict[str, Any]) -> dict[str, Any]:
        if event.get("target") == "sys_os_shell":
            shell_events.append(event)
            if event["type"] == "tool_call":
                return {"result": "ALLOW", "data": approved}
            return {"result": result_action, "data": "redacted output", "reason": "private"}
        return {"result": "ALLOW"}

    monkeypatch.setattr(sys.modules[__name__], "_shell_policy", policy, raising=False)
    caps = replace(
        get_caps(),
        default_policies=[
            FunctionPolicySpec(
                name="shell-policy",
                on=None,
                function=FunctionRef(path=f"{__name__}._shell_policy"),
            )
        ],
    )
    monkeypatch.setattr(sessions_mod, "get_caps", lambda: caps)
    config = yaml.safe_dump(
        {
            "spec_version": 1,
            "name": "shell-yield",
            "executor": {"config": {"harness": "claude-sdk"}},
            "os_env": {"shell_yield_after": 0.01, "sandbox": {"type": "none"}},
        }
    ).encode()
    (tmp_path / "config.yaml").write_bytes(config)
    created = await post_session_bundle(
        client.post,
        "/v1/sessions",
        bundle_files({"config.yaml": config}),
    )
    assert created.status_code == 201, created.text
    sid = created.json()["session_id"]
    binding = "test-shell-runner-binding"
    get_conversation_store().set_runner_id(sid, token_bound_runner_id(binding))
    client.headers[RUNNER_TUNNEL_TOKEN_HEADER] = binding
    runner = RunnerMcpHarness(session_id=sid, workspace=tmp_path, server_client=client)
    runner.spec_cache[sid] = parse(tmp_path)
    runner.inboxes[sid] = asyncio.Queue()
    runner.async_tasks[sid] = {}
    async with runner.client() as runner_client:

        async def get_runner(*args):
            return runner_client

        monkeypatch.setattr(sessions_mod, "_get_runner_client", get_runner)
        try:
            async with asyncio.timeout(2):
                response = await _post_mcp(
                    client,
                    sid,
                    _jsonrpc(
                        "tools/call",
                        params={
                            "name": "sys_os_shell",
                            "arguments": {"command": "must be rewritten"},
                            "_omnigent_operation_id": "shell-once",
                        },
                    ),
                )
            handle = json.loads(response.json()["result"]["content"][0]["text"])
            collection = runner.async_tasks[sid][handle["handle_id"]][0]
            assert [event["type"] for event in shell_events] == ["tool_call"]
        finally:
            if not times_out:
                (tmp_path / "release").touch()
        raw_output = json.loads(await asyncio.wait_for(collection, timeout=5))
        assert raw_output["timed_out"] is times_out
        result = await _post_mcp(
            client, sid, _jsonrpc("tools/call", params={"name": "sys_read_inbox"})
        )
    delivered = result.json()["result"]["content"][0]["text"]
    assert "PRIVATE OUTPUT" not in delivered
    assert (
        "redacted output" if result_action == "ALLOW" else "suppressed by policy: private"
    ) in delivered
    assert [event["type"] for event in shell_events] == ["tool_call", "tool_result"]
    assert shell_events[1]["request_data"] == {"name": "sys_os_shell", "arguments": approved}
    assert (tmp_path / "starts").read_text() == "started\n"
    assert runner.inboxes[sid].empty() and runner.async_tasks[sid] == {}


# ── Helpers ───────────────────────────────────────────────


def _jsonrpc(
    method: str,
    rpc_id: int = 1,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 request envelope.

    :param method: JSON-RPC method name, e.g. ``"tools/list"``.
    :param rpc_id: Request id echoed in the response, e.g. ``1``.
    :param params: Optional method parameters.
    :returns: A dict ready to POST as JSON.
    """
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


async def _post_mcp(
    client: httpx.AsyncClient,
    session_id: str,
    body: Any,
) -> httpx.Response:
    """POST to the MCP proxy endpoint with a JSON body.

    :param client: The test HTTP client.
    :param session_id: Target session id.
    :param body: JSON-serialisable request body.
    :returns: The HTTP response.
    """
    return await client.post(f"/v1/sessions/{session_id}/mcp", json=body)


async def _post_mcp_raw(
    client: httpx.AsyncClient,
    session_id: str,
    raw: str | bytes,
    content_type: str = "application/json",
) -> httpx.Response:
    """POST raw bytes to the MCP proxy endpoint.

    :param client: The test HTTP client.
    :param session_id: Target session id.
    :param raw: Raw body content.
    :param content_type: Content-Type header value.
    :returns: The HTTP response.
    """
    data = raw if isinstance(raw, bytes) else raw.encode()
    return await client.post(
        f"/v1/sessions/{session_id}/mcp",
        content=data,
        headers={"Content-Type": content_type},
    )


# ── Tests: JSON-RPC validation layer ─────────────────────


async def test_mcp_invalid_json_returns_parse_error(
    client: httpx.AsyncClient,
) -> None:
    """Malformed JSON body returns a JSON-RPC parse error (-32700)."""
    session = await create_test_session(client, name="mcp-bad-json")
    resp = await _post_mcp_raw(client, session["id"], "not valid json{{{")
    assert resp.status_code == 200  # JSON-RPC errors travel in the body
    payload = resp.json()
    assert payload["error"]["code"] == -32700
    assert "Parse error" in payload["error"]["message"]


async def test_mcp_non_object_body_returns_invalid_request(
    client: httpx.AsyncClient,
) -> None:
    """A JSON array body returns a JSON-RPC invalid request error (-32600)."""
    session = await create_test_session(client, name="mcp-non-object")
    resp = await _post_mcp(client, session["id"], [1, 2, 3])
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["error"]["code"] == -32600
    assert "expected JSON object" in payload["error"]["message"]


# ── Tests: method dispatch ────────────────────────────────


async def test_mcp_initialize_returns_capabilities(
    client: httpx.AsyncClient,
) -> None:
    """The ``initialize`` method returns protocol version and capabilities."""
    session = await create_test_session(client, name="mcp-init")
    resp = await _post_mcp(client, session["id"], _jsonrpc("initialize"))
    assert resp.status_code == 200
    payload = resp.json()
    assert "result" in payload
    result = payload["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "omnigent-mcp-proxy"
    # Echoes the request id.
    assert payload["id"] == 1


async def test_mcp_initialize_echoes_rpc_id(
    client: httpx.AsyncClient,
) -> None:
    """The response echoes the caller's JSON-RPC id."""
    session = await create_test_session(client, name="mcp-init-id")
    resp = await _post_mcp(client, session["id"], _jsonrpc("initialize", rpc_id=99))
    assert resp.status_code == 200
    assert resp.json()["id"] == 99


async def test_mcp_unknown_method_returns_method_not_found(
    client: httpx.AsyncClient,
) -> None:
    """An unknown JSON-RPC method returns -32601 (method not found)."""
    session = await create_test_session(client, name="mcp-unknown")
    resp = await _post_mcp(client, session["id"], _jsonrpc("resources/list", rpc_id=42))
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == 42
    assert payload["error"]["code"] == -32601
    assert "Method not found" in payload["error"]["message"]
    assert "resources/list" in payload["error"]["message"]


# ── Tests: tools/list without runner ──────────────────────


async def test_mcp_tools_list_no_runner_returns_error(
    client: httpx.AsyncClient,
) -> None:
    """``tools/list`` with no runner bound returns a -32000 application error."""
    session = await create_test_session(client, name="mcp-list-no-runner")
    resp = await _post_mcp(client, session["id"], _jsonrpc("tools/list", rpc_id=5))
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == 5
    assert payload["error"]["code"] == -32000
    assert "No runner bound" in payload["error"]["message"]


# ── Tests: tools/call validation ──────────────────────────


async def test_mcp_tools_call_missing_name_returns_error(
    client: httpx.AsyncClient,
) -> None:
    """``tools/call`` without a ``name`` param returns an application error."""
    session = await create_test_session(client, name="mcp-call-no-name")
    resp = await _post_mcp(
        client,
        session["id"],
        _jsonrpc("tools/call", rpc_id=9, params={}),
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["error"]["code"] == -32000
    assert "Missing tool name" in payload["error"]["message"]


# ── Tests: nonexistent session ────────────────────────────


async def test_mcp_nonexistent_session_returns_error(
    client: httpx.AsyncClient,
) -> None:
    """MCP on a nonexistent session returns an error.

    The session lookup (``_require_access``) runs before the JSON-RPC
    dispatch.  Without a permission store the access check is a no-op,
    so the request reaches the JSON-RPC layer.  ``tools/list`` then
    fails with "No runner bound" because the tunnel registry has no
    binding for the nonexistent session.
    """
    resp = await _post_mcp(
        client,
        "3628cfc9b00f747da23485e2534458a6",
        _jsonrpc("tools/list", rpc_id=10),
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == 10
    assert payload["error"]["code"] == -32000
    assert "No runner bound" in payload["error"]["message"]
