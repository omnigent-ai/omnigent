"""Built-in tools dispatch locally instead of taking a server ``/mcp`` round trip.

A runner-dispatched session hands every locally dispatched tool a
``ProxyMcpManager``. Routing a built-in through ``call_tool`` would
``POST /v1/sessions/{id}/mcp`` and tunnel straight back to the same runner,
and built-in ``sys_*`` tools run on the runner either way, so they dispatch
locally instead and enforce
TOOL_CALL/TOOL_RESULT policy through the server's ``/policies/evaluate``
endpoint (which short-circuits when no policy applies to the phase).
Spec-declared MCP-server tools, the server-intercepted ``sys_advise_models``,
and ``sys_read_inbox`` (whose /mcp route runs subagent-result recovery) keep
the ``/mcp`` round trip.
"""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.runner import tool_dispatch
from omnigent.runner.mcp_manager import RunnerMcpManager
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.spec.types import AgentSpec, ExecutorSpec

pytestmark = pytest.mark.asyncio

_SESSION_ID = "conv_omni12775"
_LOCAL_OUTPUT = "ran locally on the runner"
_TUNNELED_OUTPUT = "served by the server /mcp round trip"
_SUPPORTED_SERVER_VERSION = "0.19.0"
_UNSUPPORTED_SERVER_VERSION = "0.18.0"


def _recording_proxy_client(paths: list[str]) -> httpx.AsyncClient:
    def _handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": _TUNNELED_OUTPUT}],
                    "isError": False,
                },
            },
        )

    return httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://ap-server",
    )


def _claude_sdk_spec() -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        os_env=OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="none")),
    )


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("sys_os_read", json.dumps({"path": "notes.txt"})),
        ("sys_os_shell", json.dumps({"command": "true"})),
    ],
)
async def test_builtin_tool_does_not_round_trip_through_server_mcp(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: str,
) -> None:
    async def _fake_os_env(name: str, *_a: Any, **_kw: Any) -> str:
        del name
        return _LOCAL_OUTPUT

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _fake_os_env)

    paths: list[str] = []
    policy_calls: list[tuple[str, str]] = []
    client = _recording_proxy_client(paths)
    policy_client = _recording_policy_client(policy_calls)
    proxy = ProxyMcpManager(_SESSION_ID, client)
    try:
        output = await execute_tool(
            tool_name=tool_name,
            arguments=arguments,
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            server_client=policy_client,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
            server_version=_SUPPORTED_SERVER_VERSION,
        )
    finally:
        await client.aclose()
        await policy_client.aclose()

    mcp_posts = [p for p in paths if p == f"/v1/sessions/{_SESSION_ID}/mcp"]
    assert mcp_posts == [], (
        f"built-in {tool_name!r} took a server /mcp round trip "
        f"(POSTed to {mcp_posts}); it must dispatch locally on the runner"
    )
    assert output == _LOCAL_OUTPUT, (
        f"built-in {tool_name!r} output came from the server proxy "
        f"({output!r}); it must execute locally"
    )


def _recording_policy_client(
    calls: list[tuple[str, str]],
    *,
    tool_call: dict[str, Any] | None = None,
    tool_result: dict[str, Any] | None = None,
    tool_call_body: str | None = None,
    tool_result_body: str | None = None,
    bodies: list[dict[str, Any]] | None = None,
) -> httpx.AsyncClient:
    """Server client that records ``/policies/evaluate`` calls by phase.

    ``calls`` receives ``(path, phase)`` per request. ``tool_call`` and
    ``tool_result`` override the verdict for that phase; both default to ALLOW.
    ``tool_call_body``/``tool_result_body`` return a raw 200 body for that phase,
    used to exercise a malformed, non-JSON policy response. ``bodies`` receives
    each decoded request envelope so a test can assert the exact payload.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        phase = body["event"]["type"]
        calls.append((request.url.path, phase))
        if bodies is not None:
            bodies.append(body)
        if phase == "PHASE_TOOL_CALL":
            if tool_call_body is not None:
                return httpx.Response(200, content=tool_call_body)
            verdict = tool_call or {"result": "POLICY_ACTION_ALLOW"}
        else:
            if tool_result_body is not None:
                return httpx.Response(200, content=tool_result_body)
            verdict = tool_result or {"result": "POLICY_ACTION_ALLOW"}
        return httpx.Response(200, json=verdict)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://ap-server",
    )


async def test_builtin_policy_enforced_via_policies_evaluate_not_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local dispatch still gates built-ins — through /policies/evaluate, not /mcp."""
    executed: list[str] = []

    async def _fake_os_env(name: str, *_a: Any, **_kw: Any) -> str:
        executed.append(name)
        return _LOCAL_OUTPUT

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _fake_os_env)

    mcp_paths: list[str] = []
    policy_calls: list[tuple[str, str]] = []
    proxy_client = _recording_proxy_client(mcp_paths)
    policy_client = _recording_policy_client(policy_calls)
    proxy = ProxyMcpManager(_SESSION_ID, proxy_client)
    try:
        output = await execute_tool(
            tool_name="sys_os_read",
            arguments=json.dumps({"path": "notes.txt"}),
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            server_client=policy_client,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
            server_version=_SUPPORTED_SERVER_VERSION,
        )
    finally:
        await proxy_client.aclose()
        await policy_client.aclose()

    assert mcp_paths == [], "a built-in must not round trip through server /mcp"
    assert output == _LOCAL_OUTPUT
    assert executed == ["sys_os_read"], "the built-in must execute locally on ALLOW"
    assert [phase for _path, phase in policy_calls] == [
        "PHASE_TOOL_CALL",
        "PHASE_TOOL_RESULT",
    ], f"built-in policy must be enforced via /policies/evaluate; saw {policy_calls}"
    assert all(
        path == f"/v1/sessions/{_SESSION_ID}/policies/evaluate" for path, _phase in policy_calls
    )


async def test_builtin_tool_call_policy_deny_blocks_local_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TOOL_CALL deny must stop the built-in before it runs on the runner."""
    executed: list[str] = []

    async def _fake_os_env(name: str, *_a: Any, **_kw: Any) -> str:
        executed.append(name)
        return _LOCAL_OUTPUT

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _fake_os_env)

    mcp_paths: list[str] = []
    policy_calls: list[tuple[str, str]] = []
    proxy_client = _recording_proxy_client(mcp_paths)
    policy_client = _recording_policy_client(
        policy_calls,
        tool_call={"result": "POLICY_ACTION_DENY", "reason": "blocked by test policy"},
    )
    proxy = ProxyMcpManager(_SESSION_ID, proxy_client)
    try:
        output = await execute_tool(
            tool_name="sys_os_read",
            arguments=json.dumps({"path": "secret.txt"}),
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            server_client=policy_client,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
            server_version=_SUPPORTED_SERVER_VERSION,
        )
    finally:
        await proxy_client.aclose()
        await policy_client.aclose()

    assert executed == [], "a denied built-in must not execute locally"
    assert json.loads(output) == {"error": "Denied by policy: blocked by test policy"}
    assert mcp_paths == []
    assert [phase for _path, phase in policy_calls] == ["PHASE_TOOL_CALL"], (
        "a TOOL_CALL deny must short-circuit before TOOL_RESULT"
    )


async def test_server_intercepted_builtin_still_round_trips_through_mcp() -> None:
    """sys_advise_models has no local branch, so it keeps the server /mcp round trip."""
    mcp_paths: list[str] = []
    policy_calls: list[tuple[str, str]] = []
    proxy_client = _recording_proxy_client(mcp_paths)
    policy_client = _recording_policy_client(policy_calls)
    proxy = ProxyMcpManager(_SESSION_ID, proxy_client)
    try:
        output = await execute_tool(
            tool_name="sys_advise_models",
            arguments=json.dumps({"question": "which model?"}),
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            server_client=policy_client,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
            server_version=_SUPPORTED_SERVER_VERSION,
        )
    finally:
        await proxy_client.aclose()
        await policy_client.aclose()

    assert mcp_paths == [f"/v1/sessions/{_SESSION_ID}/mcp"], (
        "sys_advise_models is intercepted server-side and must keep the /mcp round trip"
    )
    assert output == _TUNNELED_OUTPUT
    assert policy_calls == [], (
        "a proxied built-in enforces policy on the /mcp path, not /policies/evaluate"
    )


async def test_builtin_tool_call_policy_non_json_body_denies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed (non-JSON) TOOL_CALL policy body is fail-closed: deny."""
    executed: list[str] = []

    async def _fake_os_env(name: str, *_a: Any, **_kw: Any) -> str:
        executed.append(name)
        return _LOCAL_OUTPUT

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _fake_os_env)

    mcp_paths: list[str] = []
    policy_calls: list[tuple[str, str]] = []
    proxy_client = _recording_proxy_client(mcp_paths)
    policy_client = _recording_policy_client(policy_calls, tool_call_body="<html>502</html>")
    proxy = ProxyMcpManager(_SESSION_ID, proxy_client)
    try:
        output = await execute_tool(
            tool_name="sys_os_read",
            arguments=json.dumps({"path": "secret.txt"}),
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            server_client=policy_client,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
            server_version=_SUPPORTED_SERVER_VERSION,
        )
    finally:
        await proxy_client.aclose()
        await policy_client.aclose()

    assert executed == [], "a malformed TOOL_CALL verdict must not execute the built-in"
    assert json.loads(output) == {
        "error": "Denied by policy: policy evaluation returned no verdict"
    }
    assert mcp_paths == []
    assert [phase for _path, phase in policy_calls] == ["PHASE_TOOL_CALL"]


async def test_builtin_tool_result_policy_non_json_body_keeps_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed (non-JSON) TOOL_RESULT policy body is fail-open: keep output."""
    executed: list[str] = []

    async def _fake_os_env(name: str, *_a: Any, **_kw: Any) -> str:
        executed.append(name)
        return _LOCAL_OUTPUT

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _fake_os_env)

    mcp_paths: list[str] = []
    policy_calls: list[tuple[str, str]] = []
    proxy_client = _recording_proxy_client(mcp_paths)
    policy_client = _recording_policy_client(policy_calls, tool_result_body="<html>502</html>")
    proxy = ProxyMcpManager(_SESSION_ID, proxy_client)
    try:
        output = await execute_tool(
            tool_name="sys_os_read",
            arguments=json.dumps({"path": "notes.txt"}),
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            server_client=policy_client,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
            server_version=_SUPPORTED_SERVER_VERSION,
        )
    finally:
        await proxy_client.aclose()
        await policy_client.aclose()

    assert executed == ["sys_os_read"], "the built-in runs before the TOOL_RESULT gate"
    assert output == _LOCAL_OUTPUT, "a malformed TOOL_RESULT verdict must keep the result"
    assert mcp_paths == []
    assert [phase for _path, phase in policy_calls] == [
        "PHASE_TOOL_CALL",
        "PHASE_TOOL_RESULT",
    ]


async def test_builtin_tool_result_policy_coerces_non_str_transform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-str TOOL_RESULT transform is coerced via ``str()``, mirroring /mcp."""

    async def _fake_os_env(name: str, *_a: Any, **_kw: Any) -> str:
        del name
        return _LOCAL_OUTPUT

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _fake_os_env)

    transform = {"redacted": True}
    mcp_paths: list[str] = []
    policy_calls: list[tuple[str, str]] = []
    proxy_client = _recording_proxy_client(mcp_paths)
    policy_client = _recording_policy_client(
        policy_calls,
        tool_result={"result": "POLICY_ACTION_ALLOW", "data": transform},
    )
    proxy = ProxyMcpManager(_SESSION_ID, proxy_client)
    try:
        output = await execute_tool(
            tool_name="sys_os_read",
            arguments=json.dumps({"path": "notes.txt"}),
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            server_client=policy_client,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
            server_version=_SUPPORTED_SERVER_VERSION,
        )
    finally:
        await proxy_client.aclose()
        await policy_client.aclose()

    assert output == str(transform), (
        "a non-str TOOL_RESULT transform must be coerced via str(), not dropped"
    )
    assert mcp_paths == []


async def test_builtin_policy_request_envelopes_match_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local policy evaluation uses the established {name, arguments} envelope.

    A result policy reads ``request_data["arguments"]``; the TOOL_RESULT request
    must match the server /mcp and native-hook contract, not a bespoke shape.
    """

    async def _fake_os_env(name: str, *_a: Any, **_kw: Any) -> str:
        del name
        return _LOCAL_OUTPUT

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _fake_os_env)

    arguments = {"path": "notes.txt"}
    mcp_paths: list[str] = []
    policy_calls: list[tuple[str, str]] = []
    policy_bodies: list[dict[str, Any]] = []
    proxy_client = _recording_proxy_client(mcp_paths)
    policy_client = _recording_policy_client(policy_calls, bodies=policy_bodies)
    proxy = ProxyMcpManager(_SESSION_ID, proxy_client)
    try:
        await execute_tool(
            tool_name="sys_os_read",
            arguments=json.dumps(arguments),
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            server_client=policy_client,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
            server_version=_SUPPORTED_SERVER_VERSION,
        )
    finally:
        await proxy_client.aclose()
        await policy_client.aclose()

    phases = [body["event"]["type"] for body in policy_bodies]
    assert phases == ["PHASE_TOOL_CALL", "PHASE_TOOL_RESULT"]
    call_event = policy_bodies[0]["event"]
    assert call_event["data"] == {"name": "sys_os_read", "arguments": arguments}
    result_event = policy_bodies[1]["event"]
    assert result_event["request_data"] == {"name": "sys_os_read", "arguments": arguments}, (
        "the result-policy envelope must use the established {name, arguments} keys"
    )


async def test_builtin_tool_call_policy_applies_transformed_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TOOL_CALL ALLOW carrying transformed data runs the approved arguments.

    This is the sequence an approved-ASK transform produces once the endpoint
    surfaces the policy data; the local path must execute the approved form.
    """
    captured_args: list[dict[str, Any]] = []

    async def _fake_os_env(name: str, args: dict[str, Any], *_a: Any, **_kw: Any) -> str:
        del name
        captured_args.append(args)
        return _LOCAL_OUTPUT

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _fake_os_env)

    transformed = {"path": "redacted.txt"}
    mcp_paths: list[str] = []
    policy_calls: list[tuple[str, str]] = []
    proxy_client = _recording_proxy_client(mcp_paths)
    policy_client = _recording_policy_client(
        policy_calls,
        tool_call={"result": "POLICY_ACTION_ALLOW", "data": transformed},
    )
    proxy = ProxyMcpManager(_SESSION_ID, proxy_client)
    try:
        output = await execute_tool(
            tool_name="sys_os_read",
            arguments=json.dumps({"path": "notes.txt"}),
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            server_client=policy_client,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
            server_version=_SUPPORTED_SERVER_VERSION,
        )
    finally:
        await proxy_client.aclose()
        await policy_client.aclose()

    assert captured_args == [transformed], (
        "the built-in must execute the policy-transformed arguments, not the original"
    )
    assert output == _LOCAL_OUTPUT
    assert mcp_paths == []


async def test_sys_read_inbox_keeps_server_mcp_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sys_read_inbox keeps the /mcp route so its recovery hook still runs.

    Dispatching it locally would skip the server-side undrained-subagent-result
    recovery, so it must not short-circuit even with a ``ProxyMcpManager``.
    """

    async def _fail_local(name: str, *_a: Any, **_kw: Any) -> str:
        del name
        raise AssertionError("sys_read_inbox must not run on the local path")

    monkeypatch.setattr(tool_dispatch, "_execute_async_inbox_tool", _fail_local)

    mcp_paths: list[str] = []
    policy_calls: list[tuple[str, str]] = []
    proxy_client = _recording_proxy_client(mcp_paths)
    policy_client = _recording_policy_client(policy_calls)
    proxy = ProxyMcpManager(_SESSION_ID, proxy_client)
    try:
        output = await execute_tool(
            tool_name="sys_read_inbox",
            arguments=json.dumps({}),
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            server_client=policy_client,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
            server_version=_SUPPORTED_SERVER_VERSION,
        )
    finally:
        await proxy_client.aclose()
        await policy_client.aclose()

    assert mcp_paths == [f"/v1/sessions/{_SESSION_ID}/mcp"], (
        "sys_read_inbox must keep the /mcp round trip for subagent-result recovery"
    )
    assert output == _TUNNELED_OUTPUT
    assert policy_calls == [], (
        "a proxied built-in enforces policy on the /mcp path, not /policies/evaluate"
    )


async def test_ungranted_builtin_refused_locally_with_proxy_manager() -> None:
    """An ungranted built-in is refused on the local arm, before any round trip.

    A locally dispatched built-in is gated by ``execute_tool`` itself rather
    than by the /mcp manager that resolves the granted surface, so an
    ungranted built-in
    with a ``ProxyMcpManager`` present must be refused without a /mcp dispatch
    or a TOOL_CALL policy round trip.
    """
    mcp_paths: list[str] = []
    policy_calls: list[tuple[str, str]] = []
    proxy_client = _recording_proxy_client(mcp_paths)
    policy_client = _recording_policy_client(policy_calls)
    proxy = ProxyMcpManager(_SESSION_ID, proxy_client)
    try:
        output = await execute_tool(
            tool_name="sys_session_share",
            arguments=json.dumps({"session_id": _SESSION_ID}),
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            server_client=policy_client,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
            server_version=_SUPPORTED_SERVER_VERSION,
        )
    finally:
        await proxy_client.aclose()
        await policy_client.aclose()

    error = json.loads(output).get("error", "")
    assert "sys_session_share" in error and "not enabled" in error, (
        f"an ungranted built-in must be refused locally; got {output!r}"
    )
    assert mcp_paths == [], "the grant refusal must not fall back to the /mcp round trip"
    assert policy_calls == [], (
        "the grant check must short-circuit before TOOL_CALL policy evaluation"
    )


async def test_builtin_without_policy_channel_falls_back_to_mcp_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A built-in with an mcp_manager but no server policy channel must not run
    ungated. With ``server_client=None`` local dispatch is unavailable, so the
    built-in falls back to the /mcp gate rather than executing without policy —
    the fail-closed TOOL_CALL contract.
    """

    async def _unexpected(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("a built-in without a policy channel must not run locally")

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _unexpected)

    mcp_paths: list[str] = []
    proxy_client = _recording_proxy_client(mcp_paths)
    proxy = ProxyMcpManager(_SESSION_ID, proxy_client)
    try:
        output = await execute_tool(
            tool_name="sys_os_read",
            arguments=json.dumps({"path": "notes.txt"}),
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
            server_version=_SUPPORTED_SERVER_VERSION,
        )
    finally:
        await proxy_client.aclose()

    assert mcp_paths == [f"/v1/sessions/{_SESSION_ID}/mcp"], (
        "without a server policy channel a built-in must fall back to the /mcp gate"
    )
    assert output == _TUNNELED_OUTPUT


async def test_builtin_tool_result_ask_passes_output_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TOOL_RESULT ASK passes the built-in's output through unchanged.

    The server /mcp bound-runner path suppresses only DENY on TOOL_RESULT and
    otherwise returns the result (applying a transform only when the verdict
    carries ``data``). The local path must mirror that: an ASK with no data is
    a pass-through, not a suppression.
    """
    executed: list[str] = []

    async def _fake_os_env(name: str, *_a: Any, **_kw: Any) -> str:
        executed.append(name)
        return _LOCAL_OUTPUT

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _fake_os_env)

    mcp_paths: list[str] = []
    policy_calls: list[tuple[str, str]] = []
    proxy_client = _recording_proxy_client(mcp_paths)
    policy_client = _recording_policy_client(
        policy_calls,
        tool_result={"result": "POLICY_ACTION_ASK", "reason": "needs review"},
    )
    proxy = ProxyMcpManager(_SESSION_ID, proxy_client)
    try:
        output = await execute_tool(
            tool_name="sys_os_read",
            arguments=json.dumps({"path": "notes.txt"}),
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            server_client=policy_client,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
            server_version=_SUPPORTED_SERVER_VERSION,
        )
    finally:
        await proxy_client.aclose()
        await policy_client.aclose()

    assert executed == ["sys_os_read"], "the built-in runs before the TOOL_RESULT gate"
    assert output == _LOCAL_OUTPUT, (
        "a TOOL_RESULT ASK with no data must pass the result through, not suppress it"
    )
    assert mcp_paths == []
    assert [phase for _path, phase in policy_calls] == [
        "PHASE_TOOL_CALL",
        "PHASE_TOOL_RESULT",
    ]


async def test_builtin_against_old_server_keeps_mcp_gate_not_local_transform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsupported server keeps the /mcp gate so approved transforms survive.

    A server too old to surface approved-ASK transforms parks a TOOL_CALL ASK
    and, once approved, returns an ALLOW without the transformed arguments.
    Dispatching locally there would run the original arguments and drop the
    approved transform, so local built-in dispatch is gated to supported
    servers; older ones keep the /mcp gate that applies the transform
    server-side. The original arguments must never execute on the local path.
    """

    async def _fail_local(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("a built-in must not dispatch locally against an unsupported server")

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _fail_local)

    mcp_paths: list[str] = []
    policy_calls: list[tuple[str, str]] = []
    proxy_client = _recording_proxy_client(mcp_paths)
    policy_client = _recording_policy_client(
        policy_calls,
        tool_call={"result": "POLICY_ACTION_ASK", "data": {"path": "redacted.txt"}},
    )
    proxy = ProxyMcpManager(_SESSION_ID, proxy_client)
    try:
        output = await execute_tool(
            tool_name="sys_os_read",
            arguments=json.dumps({"path": "notes.txt"}),
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            server_client=policy_client,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
            server_version=_UNSUPPORTED_SERVER_VERSION,
        )
    finally:
        await proxy_client.aclose()
        await policy_client.aclose()

    assert mcp_paths == [f"/v1/sessions/{_SESSION_ID}/mcp"], (
        "an unsupported server must keep the /mcp gate, not dispatch locally"
    )
    assert output == _TUNNELED_OUTPUT
    assert policy_calls == [], (
        "the /mcp path enforces policy server-side, not via /policies/evaluate"
    )
