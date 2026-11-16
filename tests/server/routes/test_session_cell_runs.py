"""``POST /v1/sessions/{id}/cell-runs``: user-clicked Python cells.

A cell run is the agent's ``sys_os_shell`` started by the user, so these tests
pin that it never bypasses the path agent shell calls take: the feature flag,
edit access, TOOL_CALL policy (deny, and a server-held approval on ask), and
the runner's ``/mcp/execute``. The command itself is run through a real OS
environment to prove the code reaches Python verbatim.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import ElicitationDeclinedError, ErrorCode, OmnigentError, WrongReplicaError
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import create_os_environment
from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.runner.routing import RoutedRunner
from omnigent.runtime import _globals, set_runner_client, set_runner_router
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes.sessions import create_sessions_router, routes_cell_runs
from omnigent.server.routes.sessions.routes_cell_runs import (
    CELL_OUTPUT_MAX_CHARS,
    cell_run_result,
    python_cell_command,
)
from omnigent.spec.types import Phase, PolicyAction
from tests.server.routes.test_shell_permission_gate import (
    _StubAgentStore,
    _StubConversationStore,
    _StubPermissionStore,
)

_RUN_PATH = "/v1/sessions/conv_share/cell-runs"
_EDITOR = {"X-Forwarded-Email": "editor@example.com"}
_SHELL_OUTPUT = json.dumps({"stdout": "4\n", "stderr": "", "exit_code": 0, "timed_out": False})


# ── Command and result shaping ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_python_cell_command_runs_code_verbatim(tmp_path: Path) -> None:
    """Shell syntax in the cell is Python source, not something the shell expands."""
    code = (
        "import os, sys\n"
        "print('$HOME `id` $(echo no)')\n"
        'print("it\'s")\n'
        "print('EOF')\n"
        "print(os.getcwd())\n"
        "sys.stderr.write('warned\\n')\n"
        "sys.exit(3)"
    )
    # Unconfined: this pins the shell quoting; sandbox reach has its own suites.
    env = create_os_environment(
        OSEnvSpec(type="caller_process", cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert env is not None
    try:
        result = await env.shell(python_cell_command(code), timeout=30)
    finally:
        env.close()

    assert result.get("exit_code") == 3, result
    assert result.get("stdout") == (f"$HOME `id` $(echo no)\nit's\nEOF\n{tmp_path.resolve()}\n"), (
        result
    )
    assert result.get("stderr") == "warned\n", result


def test_python_cell_command_is_stable_and_refuses_unsafe_input() -> None:
    """A retried run builds the identical command; NUL and the delimiter line are refused."""
    command = python_cell_command("print(1)")
    assert command == python_cell_command("print(1)")
    delimiter = command.splitlines()[-1]
    assert command == f"python3 - <<'{delimiter}'\nprint(1)\n{delimiter}\n"

    with pytest.raises(ValueError, match="NUL"):
        python_cell_command("print(1)\x00")


def test_python_cell_command_refuses_a_line_that_would_end_the_heredoc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes_cell_runs, "_heredoc_delimiter", lambda code: "END")
    with pytest.raises(ValueError, match="reserved"):
        python_cell_command("print(1)\nEND\nprint(2)")
    # Only a whole line ends a heredoc.
    assert python_cell_command("x = 'END'\n") == "python3 - <<'END'\nx = 'END'\nEND\n"


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            {"stdout": "", "stderr": "Traceback\n", "exit_code": 1, "error": "exited 1"},
            {"status": "completed", "exit_code": 1, "stderr": "Traceback\n", "error": None},
        ),
        (
            {"stdout": "partial", "stderr": "", "exit_code": None, "timed_out": True},
            {"status": "timed_out", "exit_code": None, "stdout": "partial", "error": None},
        ),
        (
            {"error": "tool 'sys_os_shell' is not enabled for this agent"},
            {
                "status": "failed",
                "exit_code": None,
                "error": "tool 'sys_os_shell' is not enabled for this agent",
            },
        ),
        (
            "[Result suppressed by policy: secrets]",
            {"status": "failed", "stdout": "", "error": "[Result suppressed by policy: secrets]"},
        ),
    ],
)
def test_cell_run_result_shapes_outcomes(output: object, expected: dict[str, object]) -> None:
    text = output if isinstance(output, str) else json.dumps(output)
    result = cell_run_result(text, duration_ms=5).model_dump()
    assert {key: result[key] for key in expected} == expected
    assert result["duration_ms"] == 5


def test_cell_run_result_caps_each_stream() -> None:
    long = "x" * (CELL_OUTPUT_MAX_CHARS + 10)
    result = cell_run_result(
        json.dumps({"stdout": long, "stderr": "short", "exit_code": 0}), duration_ms=1
    )
    assert len(result.stdout) == CELL_OUTPUT_MAX_CHARS
    assert result.stdout_truncated is True
    assert (result.stderr, result.stderr_truncated) == ("short", False)


# ── Route ──────────────────────────────────────────────────────────────────


@dataclass
class _PolicyEngine:
    """Answers TOOL_CALL with *tool_calls* in order (the last repeats); records calls."""

    tool_calls: list[PolicyResult]
    tool_result: PolicyResult = field(default_factory=lambda: PolicyResult(PolicyAction.ALLOW))
    contexts: list[EvaluationContext] = field(default_factory=list)
    label_writes: list[dict[str, str]] = field(default_factory=list)

    async def evaluate(self, ctx: EvaluationContext) -> PolicyResult:
        self.contexts.append(ctx)
        if ctx.phase != Phase.TOOL_CALL:
            return self.tool_result
        return self.tool_calls.pop(0) if len(self.tool_calls) > 1 else self.tool_calls[0]

    def apply_label_writes(self, set_labels: dict[str, str]) -> None:
        self.label_writes.append(set_labels)

    def apply_state_updates(self, updates: list[Any], *, approved: bool = False) -> None:
        del updates, approved


class _McpRunnerClient:
    """Runner client answering ``/mcp/execute`` with a canned shell result."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, Any]] = []

    async def post(self, url: str, *, json: Any = None, timeout: float | None = None) -> Any:
        del timeout
        self.posts.append((url, json))
        return httpx.Response(
            200,
            json={"result": {"output": _SHELL_OUTPUT}},
            request=httpx.Request("POST", url),
        )


class _RunnerRouter:
    """Routes the session to *client*, or fails the lookup with *error* when set."""

    def __init__(self, client: _McpRunnerClient) -> None:
        self.client = client
        self.error: OmnigentError | None = None

    def client_for_session_resources(self, conversation_id: str, **kwargs: Any) -> RoutedRunner:
        del conversation_id, kwargs
        if self.error is not None:
            raise self.error
        return RoutedRunner(runner_id="runner_cells", client=self.client)  # type: ignore[arg-type]


@pytest.fixture
def runner_router() -> Iterator[_RunnerRouter]:
    prior_client = _globals._runner_client
    prior_router = _globals._runner_router
    router = _RunnerRouter(_McpRunnerClient())
    set_runner_client(None)
    set_runner_router(router)  # type: ignore[arg-type]
    yield router
    set_runner_client(prior_client)
    set_runner_router(prior_router)


@pytest.fixture
def runner_client(runner_router: _RunnerRouter) -> _McpRunnerClient:
    return runner_router.client


@pytest.fixture
def conv_store() -> _StubConversationStore:
    store = _StubConversationStore()
    store.add("conv_share")
    return store


@pytest.fixture
def app(conv_store: _StubConversationStore, runner_router: _RunnerRouter) -> FastAPI:
    perm_store = _StubPermissionStore()
    perm_store.add_grant("editor@example.com", "conv_share", LEVEL_EDIT)
    perm_store.add_grant("viewer@example.com", "conv_share", LEVEL_READ)
    application = FastAPI()
    application.state.feature_flags = FeatureFlags(frozenset({Feature.CODE_CELL_RUNS}))

    @application.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    application.include_router(
        create_sessions_router(
            conv_store,  # type: ignore[arg-type]
            _StubAgentStore(),  # type: ignore[arg-type]
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
            permission_store=perm_store,  # type: ignore[arg-type]
            runner_router=runner_router,  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as c:
        yield c


def _use_policy(
    monkeypatch: pytest.MonkeyPatch,
    *tool_calls: PolicyResult,
    tool_result: PolicyResult | None = None,
) -> _PolicyEngine:
    engine = _PolicyEngine(tool_calls=list(tool_calls))
    if tool_result is not None:
        engine.tool_result = tool_result
    monkeypatch.setattr(sessions_mod, "_load_agent_spec_for_session", lambda conv, store: "spec")
    monkeypatch.setattr(
        sessions_mod, "_build_policy_engine_from_spec", lambda *args, **kwargs: engine
    )
    return engine


def _record_ask_gate(monkeypatch: pytest.MonkeyPatch, outcome: bool | Exception) -> list[Any]:
    calls: list[Any] = []

    async def _gate(request: Request, **kwargs: Any) -> bool:
        del request
        calls.append(kwargs)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(sessions_mod, "_hold_native_ask_gate", _gate)
    return calls


async def _run(client: httpx.AsyncClient, headers: dict[str, str] = _EDITOR, **body: Any) -> Any:
    return await client.post(
        _RUN_PATH, json={"language": "python", "code": "print(2 + 2)", **body}, headers=headers
    )


@pytest.mark.asyncio
async def test_editor_run_is_the_users_shell_call_on_the_runner(
    client: httpx.AsyncClient,
    runner_client: _McpRunnerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _use_policy(monkeypatch, PolicyResult(action=PolicyAction.ALLOW))

    response = await _run(client, timeout_s=9)

    assert response.status_code == 200, response.text
    assert response.json() | {"duration_ms": 0} == {
        "status": "completed",
        "exit_code": 0,
        "stdout": "4\n",
        "stderr": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
        "duration_ms": 0,
        "error": None,
    }
    [(url, body)] = runner_client.posts
    assert url == "/v1/sessions/conv_share/mcp/execute"
    assert body["params"] == {
        "name": "sys_os_shell",
        "arguments": {"command": python_cell_command("print(2 + 2)"), "timeout": 9},
    }
    tool_call = engine.contexts[0]
    assert (tool_call.phase, tool_call.tool_name) == (Phase.TOOL_CALL, "sys_os_shell")
    assert tool_call.actor == {"run_as": "editor@example.com"}


@pytest.mark.asyncio
async def test_refused_runs_never_reach_the_runner(
    app: FastAPI,
    client: httpx.AsyncClient,
    conv_store: _StubConversationStore,
    runner_client: _McpRunnerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_policy(monkeypatch, PolicyResult(action=PolicyAction.DENY, reason="no python"))

    denied = await _run(client)
    assert denied.status_code == 403
    assert "no python" in denied.json()["detail"]

    viewer = await _run(client, headers={"X-Forwarded-Email": "viewer@example.com"})
    assert viewer.status_code == 403

    assert (await _run(client, language="bash")).status_code == 422
    assert (await _run(client, code="")).status_code == 422
    assert (await _run(client, timeout_s=301)).status_code == 422

    conv_store._conversations["conv_share"].archived = True
    assert (await _run(client)).status_code == 409
    conv_store._conversations["conv_share"].archived = False

    app.state.feature_flags = FeatureFlags()
    assert (await _run(client)).status_code == 404

    assert runner_client.posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "expected_status"),
    [(True, 200), (False, 403), (ElicitationDeclinedError("needs review"), 403)],
)
async def test_ask_holds_an_approval_before_running(
    client: httpx.AsyncClient,
    runner_client: _McpRunnerClient,
    monkeypatch: pytest.MonkeyPatch,
    verdict: bool | Exception,
    expected_status: int,
) -> None:
    _use_policy(
        monkeypatch,
        PolicyResult(action=PolicyAction.ASK, reason="needs review", deciding_policies=["gate"]),
    )
    gate_calls = _record_ask_gate(monkeypatch, verdict)

    response = await _run(client)

    assert response.status_code == expected_status, response.text
    [gate_call] = gate_calls
    assert gate_call["phase"] == Phase.TOOL_CALL
    assert gate_call["data"]["name"] == "sys_os_shell"
    assert len(runner_client.posts) == (1 if expected_status == 200 else 0)


_ASK = PolicyResult(
    action=PolicyAction.ASK,
    reason="needs review",
    deciding_policies=["gate"],
    set_labels={"reviewed": "yes"},
    data={"command": "python3 - <<'X'\nprint('reviewed')\nX\n", "timeout": 60},
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("routing_error", "expected_status", "expected_code"),
    [
        (
            WrongReplicaError("runner is on another replica", host_id="host_elsewhere"),
            400,
            ErrorCode.WRONG_REPLICA,
        ),
        (OmnigentError("not bound to a runner", code=ErrorCode.CONFLICT), 409, None),
    ],
)
async def test_an_unreachable_runner_is_refused_before_policy_or_approval(
    client: httpx.AsyncClient,
    runner_router: _RunnerRouter,
    monkeypatch: pytest.MonkeyPatch,
    routing_error: OmnigentError,
    expected_status: int,
    expected_code: str | None,
) -> None:
    """The client re-addresses a wrong-replica miss, so nothing may be asked or run first."""
    engine = _use_policy(monkeypatch, _ASK)
    gate_calls = _record_ask_gate(monkeypatch, True)
    runner_router.error = routing_error

    response = await _run(client)

    assert response.status_code == expected_status, response.text
    if expected_code is None:
        assert "No runner bound" in response.json()["detail"]
    else:
        assert response.json()["error"]["code"] == expected_code
    assert engine.contexts == []
    assert gate_calls == []
    assert runner_router.client.posts == []


@pytest.mark.asyncio
async def test_approved_ask_runs_the_reviewed_call_and_names_the_user(
    client: httpx.AsyncClient,
    runner_client: _McpRunnerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _use_policy(monkeypatch, _ASK)
    gate_calls = _record_ask_gate(monkeypatch, True)

    assert (await _run(client)).status_code == 200

    [gate_call] = gate_calls
    assert gate_call["result"].reason == "Python cell run by editor@example.com: needs review"
    [(_url, body)] = runner_client.posts
    assert body["params"]["arguments"] == _ASK.data
    # The gate owns an approved ASK's writes; the handler must not repeat them.
    assert engine.label_writes == []
    # Policy was evaluated again after the approval.
    assert [ctx.phase for ctx in engine.contexts].count(Phase.TOOL_CALL) == 2


@pytest.mark.asyncio
async def test_approval_never_overrides_a_later_denial(
    client: httpx.AsyncClient,
    runner_client: _McpRunnerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_policy(monkeypatch, _ASK, PolicyResult(action=PolicyAction.DENY, reason="tainted"))
    _record_ask_gate(monkeypatch, True)

    response = await _run(client)

    assert response.status_code == 403
    assert "tainted" in response.json()["detail"]
    assert runner_client.posts == []


@pytest.mark.asyncio
async def test_ask_refuses_code_the_approval_card_cannot_show(
    client: httpx.AsyncClient,
    runner_client: _McpRunnerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_policy(monkeypatch, _ASK)
    gate_calls = _record_ask_gate(monkeypatch, True)

    response = await _run(client, code="print('x')\n" * 200)

    assert response.status_code == 403
    assert gate_calls == []
    assert runner_client.posts == []


@pytest.mark.asyncio
async def test_result_review_withholds_output_and_byte_cap_refuses_large_cells(
    client: httpx.AsyncClient,
    runner_client: _McpRunnerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_policy(
        monkeypatch,
        PolicyResult(action=PolicyAction.ALLOW),
        tool_result=PolicyResult(action=PolicyAction.ASK, reason="review output"),
    )

    response = await _run(client)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    assert response.json()["stdout"] == ""
    assert "withheld" in response.json()["error"]

    # Within the character limit but over 64 KiB of UTF-8.
    too_big = await _run(client, code="\u00e9" * 40_000)
    assert too_big.status_code == 400
    assert len(runner_client.posts) == 1
