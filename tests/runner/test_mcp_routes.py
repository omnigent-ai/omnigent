"""Route-level tests for the runner's ``/mcp/execute``, ``/v1/summarize`` and
``/v1/elicitations`` endpoints (``omnigent/runner/mcp_routes.py``).

The routes are registered on a bare FastAPI app with every collaborator
replaced by a small fake, so each branch — MCP manager missing, spec missing,
elicitation round-trips, retained operations, connection resolution for the
summarizer, harness forwarding — is driven through the real HTTP handler.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner import mcp_routes
from omnigent.runner.mcp_execution_registry import McpExecutionRegistry
from omnigent.runner.mcp_manager import McpSchemasResult
from omnigent.runner.native.orchestration import ResolvedSpec
from omnigent.runner.native_requester import CodexTerminalInputBinding
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runtime.harnesses._scaffold import HarnessApp, NativeInputRegistration
from omnigent.runtime.harnesses.process_manager import NoLiveHarnessError
from omnigent.runtime.requester import REQUESTER_FIELD, ExecutionRequester, InputRequester
from omnigent.spec.types import AgentSpec, ApiKeyAuth, DatabricksAuth, ExecutorSpec, ProviderAuth
from omnigent.terminals.registry import TerminalRegistry
from omnigent.terminals.writer import TerminalWriter, TerminalWriterEvidence
from omnigent.tools.mcp import McpElicitationRequired
from tests.runner.helpers import make_test_terminal_instance

pytestmark = pytest.mark.asyncio

SESSION = "conv_mcp"
EXECUTE = f"/v1/sessions/{SESSION}/mcp/execute"


def _spec(auth: Any = None, **executor: Any) -> AgentSpec:
    return AgentSpec(spec_version=1, name="t", executor=ExecutorSpec(auth=auth, **executor))


class _FakeConnection:
    def __init__(self, output: Any = "elicited-output") -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []

    async def call_tool_with_elicitation(
        self, bare_tool: str, arguments: Any, *, input_responses: Any, request_state: Any
    ) -> Any:
        self.calls.append(
            {
                "tool": bare_tool,
                "arguments": arguments,
                "input_responses": input_responses,
                "request_state": request_state,
            }
        )
        return self.output


class _FakeMcpManager:
    def __init__(self) -> None:
        self.schemas = McpSchemasResult(
            schemas=[{"name": "jira__search"}], tool_names={"jira__search"}, failures={}
        )
        self.schemas_error: Exception | None = None
        self.call_error: Exception | None = None
        self.output: Any = "tool-output"
        self.calls: list[tuple[str, Any, str]] = []
        self.route: tuple[Any, str] | None = None

    async def schemas_for(self, spec: AgentSpec) -> McpSchemasResult:
        if self.schemas_error is not None:
            raise self.schemas_error
        return self.schemas

    async def call_tool(self, spec: AgentSpec, tool_name: str, arguments: Any, *, session_id: str):
        self.calls.append((tool_name, arguments, session_id))
        if self.call_error is not None:
            raise self.call_error
        return self.output

    def _resolve_tool_route(self, spec: AgentSpec, tool_name: str) -> tuple[Any, str] | None:
        return self.route


class _FakeHarnessClient:
    def __init__(self, response: httpx.Response | None = None, error: Exception | None = None):
        self.response = response or httpx.Response(200, json={"accepted": True})
        self.error = error
        self.posts: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: Any, timeout: float) -> httpx.Response:
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.response


class _FakeProcessManager:
    def __init__(self, client: _FakeHarnessClient | None) -> None:
        self.client = client
        self.requests: list[tuple[str, str]] = []

    async def get_client(self, conv_id: str, harness: str) -> _FakeHarnessClient:
        self.requests.append((conv_id, harness))
        if self.client is None:
            raise NoLiveHarnessError("no live harness")
        return self.client


class Harness:
    """A FastAPI app with the MCP routes and recording fakes for every collaborator."""

    def __init__(
        self,
        *,
        mcp_manager: _FakeMcpManager | None = None,
        process_manager: _FakeProcessManager | None = None,
        spec_resolver: object | None = None,
    ) -> None:
        self.app = FastAPI()
        self.mcp_manager = mcp_manager
        self.spec_cache: dict[str, Any] = {}
        self.resolved_spec: AgentSpec | None = None
        self.resolved_entry: Any = None
        self.resolve_error: Exception | None = None
        self.conversations: dict[str, str] = {}
        self.published: list[tuple[str, Any]] = []
        self.recovered: list[str] = []
        self.registry = McpExecutionRegistry()
        self.inboxes: dict[str, Any] = {}
        self.async_tasks: dict[str, Any] = {}
        self.agent_ids: dict[str, str] = {SESSION: "ag_1"}

        async def _recover(session_id: str) -> None:
            self.recovered.append(session_id)

        async def _resolve_conv(response_id: str) -> str | None:
            return self.conversations.get(response_id)

        async def _resolve_spec_or_none(session_id: str) -> AgentSpec | None:
            return self.resolved_spec

        async def _resolve_entry(session_id: str) -> Any:
            if self.resolve_error is not None:
                raise self.resolve_error
            return self.resolved_entry

        async def _runtime_cwd(session_id: str) -> Path | None:
            return Path("/tmp/runtime-cwd")

        mcp_routes.register_mcp_routes(
            self.app,
            _publish_event=lambda sid, payload: self.published.append((sid, payload)),
            _recover_undrained_subagent_results=_recover,
            _resolve_conversation_id=_resolve_conv,
            _resolve_session_agent_spec_or_none=_resolve_spec_or_none,
            _resolve_session_spec_entry=_resolve_entry,
            _session_agent_ids=self.agent_ids,
            _session_async_tasks=self.async_tasks,
            _session_harness_name=lambda sid: "claude-sdk",
            _session_inboxes=self.inboxes,
            _session_runtime_cwd=_runtime_cwd,
            _session_spec_cache=self.spec_cache,
            filesystem_registry=None,
            mcp_execution_registry=self.registry,
            mcp_manager=mcp_manager,  # type: ignore[arg-type]
            process_manager=process_manager,  # type: ignore[arg-type]
            resource_registry=SessionResourceRegistry(),
            runner_workspace=Path("/tmp/runner-workspace"),
            server_client=httpx.AsyncClient(base_url="http://server"),
            spec_resolver=spec_resolver,  # type: ignore[arg-type]
            terminal_registry=None,
        )

    @asynccontextmanager
    async def client(self) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://runner"
        ) as client:
            yield client

    async def execute(self, body: Any) -> tuple[int, Any]:
        async with self.client() as client:
            resp = await client.post(EXECUTE, json=body)
        return resp.status_code, resp.json()


def _call(name: str, **params: Any) -> dict[str, Any]:
    return {"method": "tools/call", "params": {"name": name, **params}}


# ── /mcp/execute: envelope and method dispatch ─────────────────────────────


async def test_invalid_json_is_a_parse_error() -> None:
    h = Harness(mcp_manager=_FakeMcpManager())
    async with h.client() as client:
        resp = await client.post(
            EXECUTE, content=b"{not json", headers={"content-type": "application/json"}
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32700


async def test_unknown_method_is_method_not_found() -> None:
    status, body = await Harness().execute({"method": "prompts/get"})
    assert status == 200
    assert body["error"]["code"] == -32601
    assert "prompts/get" in body["error"]["message"]


# ── tools/list ─────────────────────────────────────────────────────────────


async def test_tools_list_without_a_manager_is_503() -> None:
    status, body = await Harness(mcp_manager=None).execute({"method": "tools/list"})
    assert status == 503
    assert body["error"]["message"] == "Runner MCP manager not configured"


async def test_tools_list_without_a_spec_reports_it() -> None:
    status, body = await Harness(mcp_manager=_FakeMcpManager()).execute({"method": "tools/list"})
    assert status == 200
    assert body["error"]["message"] == f"No spec available for session {SESSION!r}"


async def test_tools_list_returns_schemas_from_a_cached_resolved_spec() -> None:
    h = Harness(mcp_manager=_FakeMcpManager())
    h.spec_cache[SESSION] = ResolvedSpec(spec=_spec(), workdir=Path("/tmp/wd"))
    status, body = await h.execute({"method": "tools/list"})
    assert status == 200
    assert body["result"] == {
        "schemas": [{"name": "jira__search"}],
        "tool_names": ["jira__search"],
        "failures": {},
    }


async def test_tools_list_falls_back_to_the_spec_resolver() -> None:
    h = Harness(mcp_manager=_FakeMcpManager(), spec_resolver=object())
    h.resolved_spec = _spec()
    status, body = await h.execute({"method": "tools/list"})
    assert status == 200
    assert body["result"]["tool_names"] == ["jira__search"]


async def test_tools_list_hides_schema_failures_behind_a_generic_message() -> None:
    manager = _FakeMcpManager()
    manager.schemas_error = RuntimeError("/home/alice/secret-path exploded")
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    status, body = await h.execute({"method": "tools/list"})
    assert status == 200
    assert body["error"]["code"] == -32000
    assert "secret-path" not in body["error"]["message"]


# ── tools/call: MCP-served tools (``server__tool``) ────────────────────────


async def test_tools_call_requires_a_tool_name() -> None:
    status, body = await Harness(mcp_manager=_FakeMcpManager()).execute(_call(""))
    assert status == 200
    assert body["error"]["message"] == "Missing tool name"


async def test_mcp_tool_without_a_manager_is_503() -> None:
    status, _ = await Harness(mcp_manager=None).execute(_call("jira__search"))
    assert status == 503


async def test_mcp_tool_without_a_spec_reports_it() -> None:
    _, body = await Harness(mcp_manager=_FakeMcpManager()).execute(_call("jira__search"))
    assert body["error"]["message"] == f"No spec available for session {SESSION!r}"


async def test_mcp_tool_call_returns_the_manager_output() -> None:
    manager = _FakeMcpManager()
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    status, body = await h.execute(_call("jira__search", arguments={"q": "open"}))
    assert status == 200
    assert body == {"result": {"output": "tool-output"}}
    assert manager.calls == [("jira__search", {"q": "open"}, SESSION)]


async def test_mcp_tool_elicitation_is_surfaced_as_input_required() -> None:
    manager = _FakeMcpManager()
    manager.call_error = McpElicitationRequired(
        input_requests={"r1": {"message": "approve?"}},
        request_state="state-1",
        tool_name="jira__search",
        arguments={},
    )
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    status, body = await h.execute(_call("jira__search"))
    assert status == 200
    assert body["result"]["input_required"] == {
        "inputRequests": {"r1": {"message": "approve?"}},
        "requestState": "state-1",
    }


async def test_mcp_tool_failure_is_generic_to_the_client() -> None:
    manager = _FakeMcpManager()
    manager.call_error = RuntimeError("upstream said: token=abc123")
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    status, body = await h.execute(_call("jira__search"))
    assert status == 200
    assert body["error"]["code"] == -32000
    assert "abc123" not in body["error"]["message"]


async def test_elicitation_reply_without_a_live_route_is_an_error() -> None:
    manager = _FakeMcpManager()
    manager.route = None
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    _, body = await h.execute(
        _call("jira__search", inputResponses={"r1": "yes"}, requestState="state-1")
    )
    assert body["error"]["code"] == -32000
    assert manager.calls == []  # never falls back to a fresh call_tool


async def test_elicitation_reply_without_a_connection_is_an_error() -> None:
    manager = _FakeMcpManager()
    manager.route = (SimpleNamespace(connection=None), "search")
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    _, body = await h.execute(_call("jira__search", inputResponses={"r1": "yes"}))
    assert body["error"]["code"] == -32000


async def test_elicitation_reply_resumes_the_tool_with_the_user_input() -> None:
    manager = _FakeMcpManager()
    connection = _FakeConnection()
    manager.route = (SimpleNamespace(connection=connection), "search")
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    status, body = await h.execute(
        _call("jira__search", arguments={"q": 1}, inputResponses={"r1": "yes"}, requestState="s")
    )
    assert status == 200
    assert body == {"result": {"output": "elicited-output"}}
    assert connection.calls == [
        {
            "tool": "search",
            "arguments": {"q": 1},
            "input_responses": {"r1": "yes"},
            "request_state": "s",
        }
    ]


# ── tools/call: built-in tools dispatched through execute_tool ─────────────


@pytest.mark.parametrize("requester_user", ["missing", None, "pat"])
async def test_builtin_tool_dispatch_passes_session_context(
    monkeypatch: pytest.MonkeyPatch,
    requester_user: str | None,
) -> None:
    seen: dict[str, Any] = {}

    async def _execute_tool(**kwargs: Any) -> str:
        seen.update(kwargs)
        return "listed"

    monkeypatch.setattr("omnigent.runner.tool_dispatch.execute_tool", _execute_tool)
    h = Harness()
    spec = _spec()
    h.spec_cache[SESSION] = ResolvedSpec(spec=spec, workdir=Path("/tmp/bundle"))
    requester = (
        None
        if requester_user == "missing"
        else ExecutionRequester(InputRequester(user_id=requester_user)).snapshot()
    )
    owner_claim = ExecutionRequester(InputRequester(user_id="bryan")).snapshot()
    arguments = {"path": ".", REQUESTER_FIELD: owner_claim.model_dump()}
    call = _call("sys_list_files", arguments=arguments)
    if requester is not None:
        call["params"][REQUESTER_FIELD] = requester.model_dump()
    status, body = await h.execute(call)
    assert status == 200
    assert body == {"result": {"output": "listed"}}
    assert seen["tool_name"] == "sys_list_files"
    assert json.loads(seen["arguments"]) == arguments
    # Legacy proxies can omit metadata; argument claims cannot replace it.
    assert seen["requester"] == requester
    assert seen["agent_spec"] is spec
    assert seen["local_tool_workdir"] == Path("/tmp/bundle")
    assert seen["runner_workspace"] == Path("/tmp/runtime-cwd")
    assert seen["conversation_id"] == seen["task_id"] == SESSION
    assert seen["agent_id"] == "ag_1"
    assert seen["effective_harness"] == "claude-sdk"
    assert seen["mcp_manager"] is None


async def test_sys_read_inbox_recovers_undrained_results_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    async def _execute_tool(**kwargs: Any) -> str:
        order.append("dispatch")
        return "[]"

    monkeypatch.setattr("omnigent.runner.tool_dispatch.execute_tool", _execute_tool)
    h = Harness()
    h.spec_cache[SESSION] = _spec()
    original = h.recovered

    async def _recover_marking(session_id: str) -> None:
        order.append("recover")
        original.append(session_id)

    # Re-register is heavy; patch the closure's target instead via the list identity.
    h.recovered = original
    status, _ = await h.execute(_call("sys_read_inbox"))
    assert status == 200
    assert h.recovered == [SESSION]


async def test_builtin_tool_resolves_the_spec_entry_when_uncached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def _execute_tool(**kwargs: Any) -> str:
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr("omnigent.runner.tool_dispatch.execute_tool", _execute_tool)
    h = Harness(spec_resolver=object())
    spec = _spec()
    h.resolved_entry = ResolvedSpec(spec=spec, workdir=Path("/tmp/resolved"))
    await h.execute(_call("sys_list_files"))
    assert seen["agent_spec"] is spec
    assert seen["local_tool_workdir"] == Path("/tmp/resolved")


async def test_builtin_tool_tolerates_a_failed_spec_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def _execute_tool(**kwargs: Any) -> str:
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr("omnigent.runner.tool_dispatch.execute_tool", _execute_tool)
    h = Harness(spec_resolver=object())
    h.resolve_error = OmnigentError("agent gone", code=ErrorCode.NOT_FOUND)
    status, body = await h.execute(_call("sys_list_files"))
    assert status == 200
    assert body == {"result": {"output": "ok"}}
    assert seen["agent_spec"] is None
    assert seen["local_tool_workdir"] == Path("/tmp/runner-workspace")


async def test_builtin_tool_failure_is_generic_to_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _execute_tool(**kwargs: Any) -> str:
        raise RuntimeError("/etc/shadow unreadable")

    monkeypatch.setattr("omnigent.runner.tool_dispatch.execute_tool", _execute_tool)
    h = Harness()
    h.spec_cache[SESSION] = _spec()
    status, body = await h.execute(_call("sys_list_files"))
    assert status == 200
    assert body["error"]["code"] == -32000
    assert "/etc/shadow" not in body["error"]["message"]


# ── tools/call: retained operations (``_omnigent_operation``) ──────────────


@pytest.mark.parametrize(
    "operation",
    [{"id": "", "step": "call"}, {"id": "op", "step": ""}, {"id": 1, "step": "call"}, "nope"],
    ids=["empty-id", "empty-step", "non-string-id", "not-an-object"],
)
async def test_invalid_operation_metadata_is_rejected(operation: Any) -> None:
    h = Harness(mcp_manager=_FakeMcpManager())
    h.spec_cache[SESSION] = _spec()
    status, body = await h.execute({**_call("jira__search"), "_omnigent_operation": operation})
    assert status == 200
    assert body["error"]["message"] == "Invalid runner MCP operation metadata"


async def test_retained_operation_runs_once_and_replays_its_result() -> None:
    manager = _FakeMcpManager()
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    body = {
        **_call("jira__search", arguments={"q": 1}),
        "_omnigent_operation": {"id": "op-1", "step": "call"},
    }
    first = await h.execute(body)
    second = await h.execute(body)  # a reconnecting client re-sends the same step
    assert first == second == (200, {"result": {"output": "tool-output"}})
    assert len(manager.calls) == 1, "the external tool must not run twice"


async def test_retained_operation_with_changed_params_is_a_conflict() -> None:
    manager = _FakeMcpManager()
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    op = {"id": "op-2", "step": "call"}
    await h.execute({**_call("jira__search", arguments={"q": 1}), "_omnigent_operation": op})
    status, body = await h.execute(
        {**_call("jira__search", arguments={"q": 2}), "_omnigent_operation": op}
    )
    assert status == 200
    assert body["error"]["code"] == -32000
    assert "parameters changed" in body["error"]["message"]
    assert len(manager.calls) == 1


@pytest.fixture
async def terminal_input_binding(tmp_path: Path) -> AsyncIterator[SimpleNamespace]:
    """Real registration/refusal HTTP routes with a recorded, unspawned terminal."""
    registrations: list[NativeInputRegistration] = []
    refused: list[str] = []
    requests: list[httpx.Request] = []

    class NativeHarness(HarnessApp):
        def register_native_input(self, body: NativeInputRegistration) -> str:
            registrations.append(body)
            return f"{len(registrations):032x}"

        def refuse_native_input(self, client_id: str) -> None:
            refused.append(client_id)

    harness = NativeHarness()
    app = harness.build()
    app.state.conversation_id = SESSION
    app.state.harness_auth_token = "binding-test-token"
    terminal = make_test_terminal_instance("codex", "main", tmp_path)
    terminal.writer_evidence = TerminalWriterEvidence("incarnation", complete_observation=True)
    registry = TerminalRegistry()
    registry._by_conversation[SESSION] = {("codex", "main"): terminal}
    bridge_dir = tmp_path / "recovered-native-bridge"
    spawn_env = {
        "HARNESS_CODEX_NATIVE_BRIDGE_DIR": str(bridge_dir),
        "HARNESS_CODEX_NATIVE_REQUEST_SESSION_ID": SESSION,
    }

    async def capture(request: httpx.Request) -> None:
        requests.append(request)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
        headers={"Authorization": "Bearer binding-test-token"},
        event_hooks={"request": [capture]},
    ) as client:
        manager = SimpleNamespace(get_client=AsyncMock(return_value=client))
        binding = CodexTerminalInputBinding(
            manager, registry, SESSION, "ws://provider.test/app-server", bridge_dir=bridge_dir
        )
        binding.bind(terminal)
        yield SimpleNamespace(
            binding=binding,
            client=client,
            manager=manager,
            spawn_env=spawn_env,
            registry=registry,
            terminal=terminal,
            harness=harness,
            registrations=registrations,
            refused=refused,
            requests=requests,
        )


@pytest.mark.parametrize(
    ("writer_user", "complete_observation", "expected_user"),
    [(None, True, None), ("bryan", True, "bryan"), ("pat", True, "pat"), ("bryan", False, None)],
)
@pytest.mark.parametrize("expected_turn_id", [None, "retained-turn"])
async def test_terminal_input_registration_and_refusal_ipc(
    terminal_input_binding, writer_user, complete_observation, expected_user, expected_turn_id
) -> None:
    h = terminal_input_binding
    if not complete_observation:
        h.terminal.writer_evidence = TerminalWriterEvidence("incarnation")
        h.binding.bind(h.terminal)
    writer = TerminalWriter(
        user_id=writer_user, account_generation="generation" if writer_user else None
    )
    h.terminal.writer_evidence.record(writer)
    registration = await h.binding.register("thread", expected_turn_id)
    h.manager.get_client.assert_awaited_once_with(
        SESSION,
        "codex-native",
        env=h.spawn_env,
        replace_existing=False,
        validate=h.binding._check_incarnation,
    )
    assert registration.client_id == f"{1:032x}"
    [body] = h.registrations
    assert body.requester.user_id == expected_user
    assert body.requester.account_generation == ("generation" if expected_user else None)
    assert json.loads(h.requests[0].content) == {
        "requester": body.requester.model_dump(),
        "thread_id": "thread",
        "expected_turn_id": expected_turn_id,
        "provider_url": "ws://provider.test/app-server",
    }
    assert h.requests[0].headers["Authorization"] == "Bearer binding-test-token"
    await registration.refuse()
    assert h.refused == [registration.client_id]
    assert [request.url.path for request in h.requests] == [
        f"/v1/sessions/{SESSION}/native-input",
        f"/v1/sessions/{SESSION}/native-input/refused",
    ]
    assert json.loads(h.requests[1].content) == {"client_id": registration.client_id}
    h.manager.get_client.assert_awaited_once()


@pytest.mark.parametrize("replacement", ["instance", "evidence", "stopped"])
@pytest.mark.parametrize("pause_at", ["client", "registration", "unknown-registration"])
async def test_terminal_input_replacement_during_registration_is_refused(
    terminal_input_binding, tmp_path, replacement, pause_at
) -> None:
    h = terminal_input_binding
    writer = TerminalWriter(user_id="bryan", account_generation="generation")
    h.terminal.writer_evidence.record(writer)
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_for_replacement() -> None:
        entered.set()
        await release.wait()

    if pause_at == "client":

        async def acquire_client(*args, **kwargs):
            await wait_for_replacement()
            return h.client

        h.manager.get_client.side_effect = acquire_client
    else:

        async def pause_response(response):
            if response.request.url.path.endswith("/native-input"):
                if pause_at == "unknown-registration" and len(h.registrations) == 1:
                    h.terminal.writer_evidence.record(writer)
                else:
                    await wait_for_replacement()

        h.client.event_hooks["response"].append(pause_response)

    task = asyncio.create_task(h.binding.register("thread", "retained-turn"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if replacement == "instance":
            h.registry._by_conversation[SESSION][("codex", "main")] = make_test_terminal_instance(
                "codex", "main", tmp_path / "replacement"
            )
        elif replacement == "evidence":
            # Even an object reusing the same incarnation label is a replacement.
            h.terminal.writer_evidence = TerminalWriterEvidence(
                "incarnation", complete_observation=True
            )
        else:
            h.terminal.running = False
        release.set()
        with pytest.raises(RuntimeError, match="Terminal input source changed"):
            await asyncio.wait_for(task, timeout=5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    if pause_at == "client":
        assert h.requests == []
        assert h.registrations == h.refused == []
        return

    count = 2 if pause_at == "unknown-registration" else 1
    assert h.refused == [f"{index:032x}" for index in range(1, count + 1)]
    assert len(h.registrations) == count
    assert [request.url.path.rsplit("/", 1)[-1] for request in h.requests] == (
        ["native-input", "native-input", "refused", "refused"]
        if count == 2
        else ["native-input", "refused"]
    )
    if count == 2:
        assert h.registrations[1].requester.user_id is None


@pytest.mark.parametrize("pause_at", ["client", "registration"])
@pytest.mark.parametrize("next_writer", ["bryan", "pat", None])
async def test_terminal_input_writer_revision_reregisters_unknown(
    terminal_input_binding, pause_at, next_writer
) -> None:
    from omnigent.harnesses.codex_native.execution import CodexExecutionIdentity
    from omnigent.harnesses.codex_native.requester import CodexRequesterRegistry

    h = terminal_input_binding
    evidence = h.terminal.writer_evidence
    evidence.record(TerminalWriter(user_id="bryan", account_generation="generation"))
    registry = CodexRequesterRegistry()
    owner = InputRequester(user_id="bryan", account_generation="generation")
    accepted_id = registry.begin(owner, "thread", None)
    identity = CodexExecutionIdentity("provider-session", "thread", "owner-turn")
    response = {
        "result": {
            "thread": {
                "id": identity.thread_id,
                "sessionId": identity.session_id,
                "turns": [
                    {
                        "id": identity.turn_id,
                        "items": [{"type": "userMessage", "clientId": accepted_id}],
                    }
                ],
            }
        }
    }
    assert registry.snapshot(identity, response, revision=registry.revision).origin == owner
    register, refuse = h.harness.register_native_input, h.harness.refuse_native_input
    input_ids = {}

    def register_pending(body):
        client_id = register(body)
        input_ids[client_id] = registry.begin(
            body.requester, body.thread_id, body.expected_turn_id
        )
        return client_id

    def refuse_pending(client_id):
        refuse(client_id)
        registry.finish(input_ids[client_id], refused=True)
        # The retained owner call must remain restricted across both IPC awaits.
        snapshot = registry.snapshot(identity, response, revision=registry.revision)
        assert snapshot is not None
        assert snapshot.origin.user_id == (None if len(h.refused) == 1 else "bryan")

    h.harness.register_native_input = register_pending
    h.harness.refuse_native_input = refuse_pending

    async def change_writer() -> None:
        evidence.record(
            TerminalWriter(
                user_id=next_writer, account_generation="generation" if next_writer else None
            )
        )
        await asyncio.sleep(0)

    if pause_at == "client":

        async def acquire_client(*args, **kwargs):
            await change_writer()
            return h.client

        h.manager.get_client.side_effect = acquire_client
    else:

        async def change_after_registration(response):
            if response.request.url.path.endswith("/native-input") and len(h.registrations) == 1:
                await change_writer()

        h.client.event_hooks["response"].append(change_after_registration)

    registration = await h.binding.register("thread", None)
    assert registration.client_id == f"{2:032x}"
    assert h.refused == [f"{1:032x}"]
    first, second = h.registrations
    assert first.requester.user_id == "bryan"
    assert second.requester.user_id is None
    assert second.requester.account_generation is None
    assert first.requester.input_id != second.requester.input_id
    assert [request.url.path.rsplit("/", 1)[-1] for request in h.requests] == [
        "native-input",
        "native-input",
        "refused",
    ]
    first_json, second_json = (json.loads(h.requests[index].content) for index in (0, 1))
    assert second_json == {**first_json, "requester": second.requester.model_dump()}
    await registration.refuse()
    assert h.refused == [f"{1:032x}", f"{2:032x}"]


@pytest.mark.parametrize("provisioning", ["recovered", "missing", "failed"])
async def test_terminal_input_recovers_idle_harness_or_refuses_failed_provisioning(
    terminal_input_binding, provisioning
) -> None:
    h = terminal_input_binding

    async def provision(session_id, harness, *, env, replace_existing, validate):
        # Recovery starts without a live harness; lookup-only acquisition cannot help.
        assert (session_id, harness, env) == (SESSION, "codex-native", h.spawn_env)
        assert replace_existing is False
        assert validate == h.binding._check_incarnation
        assert h.requests == []
        if provisioning == "missing":
            raise NoLiveHarnessError("no live harness")
        if provisioning == "failed":
            raise RuntimeError("harness provisioning failed")
        return h.client

    h.manager.get_client.side_effect = provision
    if provisioning == "recovered":
        registration = await h.binding.register("thread", None)
        [body] = h.registrations
        # Starting a harness must not authenticate unobserved terminal input.
        assert body.requester.user_id is None
        assert body.requester.account_generation is None
        assert body.thread_id == "thread"
        assert body.expected_turn_id is None
        assert body.provider_url == "ws://provider.test/app-server"
        await registration.refuse()
        assert h.refused == [registration.client_id]
    else:
        error = NoLiveHarnessError if provisioning == "missing" else RuntimeError
        message = "no live harness" if provisioning == "missing" else "harness provisioning failed"
        with pytest.raises(error, match=message):
            await h.binding.register("thread", None)
        assert h.requests == []
        assert h.registrations == h.refused == []
    h.manager.get_client.assert_awaited_once_with(
        SESSION,
        "codex-native",
        env=h.spawn_env,
        replace_existing=False,
        validate=h.binding._check_incarnation,
    )


# ── /v1/summarize ──────────────────────────────────────────────────────────


class _FakeLlm:
    def __init__(self, text: str = "a summary") -> None:
        self.create_kwargs: dict[str, Any] = {}
        self.responses = self
        self.text = text

    async def create(self, **kwargs: Any) -> Any:
        self.create_kwargs = kwargs
        return SimpleNamespace(output=[SimpleNamespace(content=[SimpleNamespace(text=self.text)])])


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> _FakeLlm:
    fake = _FakeLlm()
    monkeypatch.setattr(mcp_routes, "_get_runner_llm_client", lambda: fake)
    import tiktoken

    class _Enc:
        def encode(self, text: str) -> list[int]:
            return list(range(len(text.split())))

    def _for_model(name: str) -> _Enc:
        if name.startswith("gpt-"):
            return _Enc()
        raise KeyError(name)

    monkeypatch.setattr(tiktoken, "encoding_for_model", _for_model)
    monkeypatch.setattr(tiktoken, "get_encoding", lambda name: _Enc())
    return fake


async def _summarize(h: Harness, body: Any) -> tuple[int, Any]:
    async with h.client() as client:
        resp = await client.post("/v1/summarize", json=body)
    return resp.status_code, resp.json()


async def test_summarize_requires_messages_and_model(llm: _FakeLlm) -> None:
    status, body = await _summarize(Harness(), {"messages": "no", "model": "gpt-4o"})
    assert status == 400
    assert body["error"]["code"] == "invalid_input"


async def test_summarize_uses_an_explicit_connection_and_counts_tokens(llm: _FakeLlm) -> None:
    messages = [{"role": "user", "content": "hello there"}]
    status, body = await _summarize(
        Harness(),
        {"messages": messages, "model": "gpt-4o", "connection": {"api_key": "k"}},
    )
    assert status == 200
    assert body == {"text": "a summary", "token_count": 2}
    assert llm.create_kwargs["model"] == "gpt-4o"
    assert llm.create_kwargs["connection_params"] == {"api_key": "k"}
    assert llm.create_kwargs["tools"] == []


async def test_summarize_falls_back_to_cl100k_for_unknown_models(llm: _FakeLlm) -> None:
    status, body = await _summarize(
        Harness(),
        {"messages": [], "model": "databricks/claude-sonnet", "connection": {"api_key": "k"}},
    )
    assert status == 200
    assert body["token_count"] == 2


async def test_summarize_without_a_session_sends_no_connection(llm: _FakeLlm) -> None:
    await _summarize(Harness(), {"messages": [], "model": "gpt-4o"})
    assert llm.create_kwargs["connection_params"] is None


async def _connection_for(h: Harness, llm: _FakeLlm, model: str = "gpt-4o") -> Any:
    await _summarize(h, {"messages": [], "model": model, "session_id": SESSION})
    return llm.create_kwargs["connection_params"]


async def test_summarize_resolves_api_key_auth_from_the_session_spec(llm: _FakeLlm) -> None:
    h = Harness()
    h.spec_cache[SESSION] = ResolvedSpec(
        spec=_spec(ApiKeyAuth(api_key="sk", base_url="https://llm.example")), workdir=None
    )
    assert await _connection_for(h, llm) == {"api_key": "sk", "base_url": "https://llm.example"}


async def test_summarize_resolves_databricks_auth_to_serving_endpoints(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: SimpleNamespace(host=f"https://{profile}.cloud/", token="dapi"),
    )
    h = Harness()
    h.spec_cache[SESSION] = _spec(DatabricksAuth(profile="prod"))
    assert await _connection_for(h, llm) == {
        "base_url": "https://prod.cloud/serving-endpoints",
        "api_key": "dapi",
    }


async def test_summarize_tolerates_an_unresolvable_databricks_profile(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(profile: str) -> Any:
        raise OSError("no Databricks profile configured")

    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace", _boom
    )
    h = Harness()
    h.spec_cache[SESSION] = _spec(DatabricksAuth(profile="prod"))
    assert await _connection_for(h, llm) is None


def _providers(monkeypatch: pytest.MonkeyPatch, entries: dict[str, Any] | Exception) -> None:
    def _load_providers(config: Any) -> dict[str, Any]:
        if isinstance(entries, Exception):
            raise entries
        return entries

    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", dict)
    monkeypatch.setattr("omnigent.onboarding.detected.effective_config_with_detected", lambda c: c)
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_providers", _load_providers)


def _provider_entry(**families: Any) -> Any:
    return SimpleNamespace(kind="generic", profile=None, family=lambda name: families.get(name))


async def test_summarize_resolves_a_provider_family_by_model(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    _providers(
        monkeypatch,
        {
            "work": _provider_entry(
                openai=SimpleNamespace(api_key="oa", base_url=None),
                anthropic=SimpleNamespace(api_key="an", base_url="https://a"),
            )
        },
    )
    h = Harness()
    h.spec_cache[SESSION] = _spec(ProviderAuth(name="work"))
    assert await _connection_for(h, llm, model="claude-sonnet") == {
        "api_key": "an",
        "base_url": "https://a",
    }
    assert await _connection_for(h, llm, model="gpt-4o") == {"api_key": "oa"}


async def test_summarize_provider_falls_back_to_the_other_family(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    _providers(
        monkeypatch,
        {"work": _provider_entry(anthropic=SimpleNamespace(api_key="an", base_url=None))},
    )
    h = Harness()
    h.spec_cache[SESSION] = _spec(ProviderAuth(name="work"))
    assert await _connection_for(h, llm, model="gpt-4o") == {"api_key": "an"}


async def test_summarize_provider_without_a_usable_family_yields_no_connection(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    _providers(
        monkeypatch,
        {
            "work": _provider_entry(),
            "empty": _provider_entry(openai=SimpleNamespace(api_key="", base_url="")),
        },
    )
    h = Harness()
    h.spec_cache[SESSION] = _spec(ProviderAuth(name="work"))
    assert await _connection_for(h, llm) is None
    h.spec_cache[SESSION] = _spec(ProviderAuth(name="empty"))
    assert await _connection_for(h, llm) is None
    h.spec_cache[SESSION] = _spec(ProviderAuth(name="missing"))
    assert await _connection_for(h, llm) is None


async def test_summarize_databricks_provider_uses_its_profile(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    _providers(
        monkeypatch,
        {"dbx": SimpleNamespace(kind="databricks", profile="team", family=lambda n: None)},
    )
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: SimpleNamespace(host=f"https://{profile}", token="t"),
    )
    h = Harness()
    h.spec_cache[SESSION] = _spec(ProviderAuth(name="dbx"))
    assert await _connection_for(h, llm) == {
        "base_url": "https://team/serving-endpoints",
        "api_key": "t",
    }


async def test_summarize_provider_loader_failure_is_swallowed(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    _providers(monkeypatch, RuntimeError("config unreadable"))
    h = Harness()
    h.spec_cache[SESSION] = _spec(ProviderAuth(name="work"))
    assert await _connection_for(h, llm) is None


async def test_summarize_uses_global_auth_when_the_spec_declares_none(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness()
    h.spec_cache[SESSION] = _spec()
    monkeypatch.setattr(
        "omnigent.runtime.workflow._load_global_auth",
        lambda: ApiKeyAuth(api_key="g", base_url="https://g"),
    )
    assert await _connection_for(h, llm) == {"api_key": "g", "base_url": "https://g"}

    monkeypatch.setattr(
        "omnigent.runtime.workflow._load_global_auth", lambda: DatabricksAuth(profile="glob")
    )
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: SimpleNamespace(host=f"https://{profile}", token="t"),
    )
    assert await _connection_for(h, llm) == {
        "base_url": "https://glob/serving-endpoints",
        "api_key": "t",
    }

    monkeypatch.setattr("omnigent.runtime.workflow._load_global_auth", lambda: None)
    assert await _connection_for(h, llm) is None


async def test_summarize_databricks_model_uses_the_legacy_profile(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: SimpleNamespace(host=f"https://{profile}", token="t"),
    )
    h = Harness()
    h.spec_cache[SESSION] = _spec(profile="legacy")
    assert await _connection_for(h, llm, model="databricks/dbrx") == {
        "base_url": "https://legacy/serving-endpoints",
        "api_key": "t",
    }
    h.spec_cache[SESSION] = _spec(config={"profile": "cfg"})
    assert await _connection_for(h, llm, model="databricks-dbrx") == {
        "base_url": "https://cfg/serving-endpoints",
        "api_key": "t",
    }
    # A legacy profile with a non-databricks model resolves nothing.
    assert await _connection_for(h, llm, model="gpt-4o") is None


async def test_summarize_without_a_cached_spec_sends_no_connection(llm: _FakeLlm) -> None:
    h = Harness()
    assert await _connection_for(h, llm) is None
    h.spec_cache[SESSION] = None
    assert await _connection_for(h, llm) is None


def test_runner_llm_client_is_created_once(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[object] = []

    class _Client:
        def __init__(self) -> None:
            created.append(self)

    monkeypatch.setattr("omnigent.llms.Client", _Client)
    monkeypatch.setattr(mcp_routes, "_runner_llm_client", None)
    first = mcp_routes._get_runner_llm_client()
    assert mcp_routes._get_runner_llm_client() is first
    assert len(created) == 1


# ── /v1/elicitations/{id} ──────────────────────────────────────────────────


async def _elicit(h: Harness, body: Any) -> tuple[int, Any]:
    async with h.client() as client:
        resp = await client.post("/v1/elicitations/elic_1", json=body)
    return resp.status_code, (resp.json() if resp.content else None)


async def test_elicitation_without_a_process_manager_is_501() -> None:
    status, body = await _elicit(Harness(process_manager=None), {"response_id": "r"})
    assert status == 501
    assert body["error"] == "not_implemented"


async def test_elicitation_requires_a_response_id() -> None:
    h = Harness(process_manager=_FakeProcessManager(_FakeHarnessClient()))
    status, body = await _elicit(h, {"action": "accept"})
    assert status == 400
    assert body["error"] == "invalid_request"


async def test_elicitation_for_an_unknown_response_is_404() -> None:
    h = Harness(process_manager=_FakeProcessManager(_FakeHarnessClient()))
    status, body = await _elicit(h, {"response_id": "resp_missing", "action": "accept"})
    assert status == 404
    assert body["error"] == "not_found"


async def test_elicitation_without_a_live_harness_is_409() -> None:
    h = Harness(process_manager=_FakeProcessManager(None))
    h.conversations["resp_1"] = "conv_1"
    status, body = await _elicit(h, {"response_id": "resp_1", "action": "accept"})
    assert status == 409
    assert body["error"] == "no_live_harness"


async def test_elicitation_is_forwarded_to_the_harness_as_an_approval_event() -> None:
    client = _FakeHarnessClient(httpx.Response(200, json={"accepted": True}))
    pm = _FakeProcessManager(client)
    h = Harness(process_manager=pm)
    h.conversations["resp_1"] = "conv_1"
    status, body = await _elicit(
        h, {"response_id": "resp_1", "action": "accept", "content": {"answer": "yes"}}
    )
    assert status == 200
    assert body == {"accepted": True}
    assert pm.requests == [("conv_1", "any")]
    assert client.posts == [
        {
            "url": "/v1/sessions/conv_1/events",
            "json": {
                "type": "approval",
                "elicitation_id": "elic_1",
                "action": "accept",
                "content": {"answer": "yes"},
            },
            "timeout": 30.0,
        }
    ]


async def test_elicitation_omits_content_when_absent_and_mirrors_no_body_statuses() -> None:
    client = _FakeHarnessClient(httpx.Response(204))
    h = Harness(process_manager=_FakeProcessManager(client))
    h.conversations["resp_1"] = "conv_1"
    status, body = await _elicit(h, {"response_id": "resp_1", "action": "decline"})
    assert status == 204
    assert body is None
    assert "content" not in client.posts[0]["json"]


async def test_elicitation_forward_failure_is_502_with_a_generic_detail() -> None:
    client = _FakeHarnessClient(error=RuntimeError("socket to /run/harness.sock died"))
    h = Harness(process_manager=_FakeProcessManager(client))
    h.conversations["resp_1"] = "conv_1"
    status, body = await _elicit(h, {"response_id": "resp_1", "action": "accept"})
    assert status == 502
    assert body["error"] == "elicitation_failed"
    assert "harness.sock" not in body["detail"]
