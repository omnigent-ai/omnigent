"""Server routes that start and stop chat code runs.

Mirrors ``test_shell_permission_gate.py``: a code run executes on the
owner's runner, so only the owner (or an admin) may start or stop one, and
every refused caller is turned away before the runner is called.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.entities import CodeRunData, ConversationItem, MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.runtime import _globals, set_runner_client, set_runner_router
from omnigent.runtime import session_stream as session_stream_mod
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.feature_flags import FEATURES_ENV_VAR, resolve_feature_flags
from omnigent.server.routes.sessions import create_sessions_router, routes_code_runs
from omnigent.server.routes.sessions.routes_code_runs import (
    derive_code_run_command,
    find_code_fence,
)
from omnigent.spec.types import PolicyAction
from tests.server.routes.test_shell_permission_gate import (
    _StubAgentStore,
    _StubConversationStore,
    _StubPermissionStore,
)

_SESSION = "conv_share"
_RUNS = f"/v1/sessions/{_SESSION}/code-runs"
_OWNER = {"X-Forwarded-Email": "real-owner@example.com"}
_CODE = "$ echo hi\n$ ls"
_MESSAGE_TEXT = (
    f"Try this:\n\n```bash\n{_CODE}\n```\n\nand this:\n\n```console\n$ pwd\n/tmp\n```\n"
    "\nThen:\n\n~~~python title=demo\nprint(1)\n~~~\n\n```\n$ make\n```\n"
    "\n1. Steps:\n\n   ```bash\n   $ echo a\n   $ echo b\n   ```\n"
    "\n- Outer\n  - Inner:\n\n      ```python\n      print(2)\n      ```\n"
    "\n~~~~bash\ncat <<'EOF'\n```\nx\n```\nEOF\n~~~~\n"
    "\n````markdown\n```bash\n$ echo nested\n```\n````\n"
)


def _phase(item: NewConversationItem) -> str:
    assert isinstance(item.data, CodeRunData)
    return item.data.phase


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class _ItemStore(_StubConversationStore):
    """Conversation stub that also stores items."""

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[str, ConversationItem] = {}
        self.appended: list[NewConversationItem] = []

    def add_message(self, item_id: str, role: str, text: str) -> None:
        self.items[item_id] = ConversationItem(
            id=item_id,
            type="message",
            status="completed",
            response_id="resp_1",
            created_at=0,
            data=MessageData(
                role=role,  # type: ignore[arg-type]
                agent="coder" if role == "assistant" else None,
                content=[{"type": "output_text", "text": text}],
            ),
        )

    def get_item(self, conversation_id: str, item_id: str) -> ConversationItem | None:
        del conversation_id
        return self.items.get(item_id)

    def append(
        self, conversation_id: str, items: list[NewConversationItem]
    ) -> list[ConversationItem]:
        del conversation_id
        persisted: list[ConversationItem] = []
        for item in items:
            # Like the real store, a known stable id returns the stored item.
            existing = self.items.get(item.stable_id or "")
            if existing is not None:
                persisted.append(existing.model_copy(update={"deduplicated": True}))
                continue
            self.appended.append(item)
            stored = ConversationItem(
                id=item.stable_id or f"ci_{len(self.items)}",
                type=item.type,
                status="completed",
                response_id=item.response_id,
                created_at=0,
                data=item.data,
                created_by=item.created_by,
            )
            self.items[stored.id] = stored
            persisted.append(stored)
        return persisted


# What the runner's code-run route itself answers when it refuses before starting.
_RUNNER_START_BODIES: dict[int, dict[str, str]] = {
    400: {"code": "invalid_input", "reason": "Malformed code run request"},
    409: {"code": "copy_on_write", "reason": "copy-on-write sandbox"},
    429: {"code": "too_many_runs", "reason": "At most 4 code runs per session"},
}


class _RunnerClient:
    """Runner client that records calls and serves a scripted run listing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.running: list[dict[str, Any]] = []
        self.copy_on_write = False
        self.start_status = 202
        self.start_body: Any = None
        self.start_content: bytes | None = None
        self.start_error: Exception | None = None
        self.stop_error: Exception | None = None
        self.store: _ItemStore | None = None
        self.saved_before_post: list[list[str]] = []

    def _record(self, method: str, url: str, body: Any = None) -> None:
        self.calls.append((method, url, body))

    async def get(
        self, url: str, *, params: Any = None, timeout: float | None = None
    ) -> httpx.Response:
        del timeout
        self._record("GET", url, params)
        return httpx.Response(
            200,
            json={"object": "list", "data": self.running, "copy_on_write": self.copy_on_write},
            request=httpx.Request("GET", url),
        )

    async def post(
        self, url: str, *, json: Any = None, timeout: float | None = None
    ) -> httpx.Response:
        del timeout
        self._record("POST", url, json)
        if self.store is not None:
            self.saved_before_post.append([_phase(item) for item in self.store.appended])
        if self.start_error is not None and not url.endswith("/stop"):
            raise self.start_error
        if self.stop_error is not None and url.endswith("/stop"):
            raise self.stop_error
        if url.endswith("/stop"):
            return httpx.Response(200, json={"stopped": True}, request=httpx.Request("POST", url))
        request = httpx.Request("POST", url)
        if self.start_content is not None:
            return httpx.Response(self.start_status, content=self.start_content, request=request)
        body = self.start_body
        if body is None:
            body = _RUNNER_START_BODIES.get(self.start_status, {"detail": "upstream error"})
            if self.start_status == 202:
                body = {"run_id": (json or {}).get("run_id")}
        return httpx.Response(self.start_status, json=body, request=request)


class _RoutedRunner:
    def __init__(self, client: _RunnerClient) -> None:
        self.runner_id = "runner_one"
        self.client = client


class _FakeRunnerRouter:
    def __init__(self, client: _RunnerClient) -> None:
        self.client = client

    def client_for_session_resources(
        self, session_id: str, *, conversation: Any = None
    ) -> _RoutedRunner:
        del session_id, conversation
        return _RoutedRunner(self.client)


@pytest.fixture
def runner() -> Iterator[_RunnerClient]:
    prior_client = _globals._runner_client
    prior_router = _globals._runner_router
    client = _RunnerClient()
    set_runner_client(None)
    set_runner_router(_FakeRunnerRouter(client))  # type: ignore[arg-type]
    yield client
    set_runner_client(prior_client)
    set_runner_router(prior_router)


@pytest.fixture
def store() -> _ItemStore:
    conv_store = _ItemStore()
    conv_store.add(_SESSION)
    conv_store.add_message("msg_1", "assistant", _MESSAGE_TEXT)
    conv_store.add_message("msg_user", "user", _MESSAGE_TEXT)
    return conv_store


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        session_stream_mod, "publish", lambda session_id, event: events.append(event)
    )
    return events


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the policy evaluation with a scripted verdict; records its inputs."""
    state: dict[str, Any] = {"result": None, "calls": []}

    async def evaluate(
        session_id: str,
        conversation: Any,
        agent_store: Any,
        conversation_store: Any,
        **kwargs: Any,
    ) -> Any:
        state["calls"].append(kwargs)
        return state["result"], state.get("engine")

    monkeypatch.setattr(routes_code_runs, "_evaluate_run_policy", evaluate)
    return state


def _app(store: _ItemStore, *, flag_on: bool = True) -> FastAPI:
    perm_store = _StubPermissionStore()
    perm_store.add_grant("owner@example.com", _SESSION, 2)
    perm_store.add_grant("viewer@example.com", _SESSION, 1)
    perm_store.add_grant("real-owner@example.com", _SESSION, 4)
    perm_store.add_admin("admin@example.com")
    application = FastAPI()
    application.state.feature_flags = resolve_feature_flags(
        {FEATURES_ENV_VAR: "code_block_run" if flag_on else ""}
    )

    @application.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    application.include_router(
        create_sessions_router(
            store,  # type: ignore[arg-type]
            _StubAgentStore(),  # type: ignore[arg-type]
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
            permission_store=perm_store,  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    return application


@pytest.fixture
async def client(store: _ItemStore, runner: _RunnerClient) -> AsyncIterator[httpx.AsyncClient]:
    del runner
    transport = httpx.ASGITransport(app=_app(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as c:
        yield c


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "message_id": "msg_1",
        "block_hash": _hash(_CODE),
        "language": "shell",
        "code": _CODE,
    }
    body.update(overrides)
    if "code" in overrides and "block_hash" not in overrides:
        body["block_hash"] = _hash(overrides["code"])
    return body


def _runner_starts(runner: _RunnerClient) -> list[tuple[str, str, Any]]:
    return [call for call in runner.calls if call[0] == "POST" and call[1] == _RUNS]


# ── Command derivation ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("language", "code", "console", "expected"),
    [
        ("shell", "$ echo hi\n% ls -la\nplain", False, "echo hi\nls -la\nplain"),
        ("shell", "$ echo hi\nhi\n$ ls\nfile.txt", True, "echo hi\nls"),
        ("python", "$ not a prompt\nprint(1)", False, "$ not a prompt\nprint(1)"),
        ("node", "% x", False, "% x"),
    ],
    ids=["shell-prompts", "console-output", "python", "node"],
)
def test_derive_code_run_command(language: str, code: str, console: bool, expected: str) -> None:
    assert derive_code_run_command(language, code, console=console) == expected


# Shared with web/src/lib/codeRuns.test.ts, which checks the client's copy.
_DERIVATION_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "code_runs" / "command_derivation.json"
)


@pytest.mark.parametrize(
    "case", json.loads(_DERIVATION_FIXTURE.read_text())["cases"], ids=lambda case: case["name"]
)
def test_derive_code_run_command_matches_shared_fixture(case: dict[str, Any]) -> None:
    """The route's cleaning of a block gives the command the web shows for it."""
    code = routes_code_runs._normalize_block_text(case["code"])
    assert (
        derive_code_run_command(case["language"], code, console=case["console"])
        == (case["command"])
    )


_ELIGIBILITY_FIXTURE = _DERIVATION_FIXTURE.with_name("untagged_eligibility.json")


@pytest.mark.parametrize(
    "case", json.loads(_ELIGIBILITY_FIXTURE.read_text())["cases"], ids=lambda case: case["name"]
)
def test_untagged_block_eligibility_matches_shared_fixture(case: dict[str, Any]) -> None:
    """An untagged block runs as shell exactly when the web offers Run for it."""
    fence = find_code_fence(f"```\n{case['code']}\n```", case["code"], "shell")
    assert (fence is not None) == case["runnable"]


@pytest.mark.parametrize(
    ("code", "language", "expected"),
    [
        (_CODE, "shell", ("shell", False)),
        (_CODE + "\n", "shell", ("shell", False)),
        (_CODE.replace("\n", "\r\n"), "shell", ("shell", False)),
        ("$ pwd\n/tmp", "shell", ("shell", True)),
        ("print(1)", "python", ("python", False)),
        ("$ make", "shell", ("shell", False)),
        ("$ echo a\n$ echo b", "shell", ("shell", False)),
        ("print(2)", "python", ("python", False)),
        ("cat <<'EOF'\n```\nx\n```\nEOF", "shell", ("shell", False)),
        ("$ echo nested", "shell", None),
        ("$ pwd", "shell", None),
        ("$ echo hi", "shell", None),
        ("Try this:", "shell", None),
        ("```bash\n" + _CODE, "shell", None),
        (_CODE, "python", None),
    ],
    ids=[
        "whole-fence",
        "trailing-newline",
        "crlf",
        "console",
        "tilde-python",
        "untagged-prompts",
        "list-item",
        "nested-list-item",
        "outer-fence-with-inner-fences",
        "fence-nested-inside-a-longer-fence",
        "console-prefix",
        "partial-fence",
        "prose",
        "fence-with-opener",
        "language-mismatch",
    ],
)
def test_find_code_fence_requires_one_whole_fence_body(
    code: str, language: str, expected: tuple[str, bool] | None
) -> None:
    fence = find_code_fence(_MESSAGE_TEXT, code, language)
    assert (None if fence is None else (fence.language, fence.console)) == expected


# ── Gate: refused callers never reach the runner ──────────────────


@pytest.mark.asyncio
async def test_flag_off_returns_404_before_anything_else(
    store: _ItemStore, runner: _RunnerClient
) -> None:
    transport = httpx.ASGITransport(app=_app(store, flag_on=False))
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as off:
        start = await off.post(_RUNS, json=_body())
        stop = await off.post(f"{_RUNS}/run_1/stop", json={})
        unauthenticated = await off.post(
            _RUNS, content="x", headers={"Content-Type": "text/plain"}
        )
    assert (start.status_code, stop.status_code, unauthenticated.status_code) == (404, 404, 404)
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 401),
        ({"X-Forwarded-Email": "viewer@example.com"}, 403),
        ({"X-Forwarded-Email": "owner@example.com"}, 403),
    ],
    ids=["unauthenticated", "read-only", "edit"],
)
async def test_non_owners_are_refused_before_the_runner(
    client: httpx.AsyncClient, runner: _RunnerClient, headers: dict[str, str], status: int
) -> None:
    start = await client.post(_RUNS, json=_body(), headers=headers)
    stop = await client.post(f"{_RUNS}/run_1/stop", json={}, headers=headers)

    assert (start.status_code, stop.status_code) == (status, status), start.text
    assert runner.calls == []


# ── Start: checks in order ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        _body(language="ruby"),
        _body(code=""),
        _body(code="x" * (64 * 1024 + 1)),
        _body(block_hash="0" * 64),
        _body(code="echo not in the message"),
        _body(code="Try this:"),
        _body(code="$ echo hi"),
        _body(code="$ pwd"),
        _body(language="python"),
        _body(message_id="msg_user"),
        _body(message_id="msg_missing"),
        _body(timeout=0),
        _body(timeout=601),
        _body(timeout="60"),
    ],
    ids=[
        "language",
        "empty",
        "oversize",
        "hash",
        "not-verbatim",
        "prose-snippet",
        "partial-fence",
        "console-prefix",
        "language-mismatch",
        "user-message",
        "unknown-message",
        "timeout-low",
        "timeout-high",
        "timeout-type",
    ],
)
async def test_invalid_starts_return_400_without_starting(
    client: httpx.AsyncClient, runner: _RunnerClient, policy: dict[str, Any], body: dict[str, Any]
) -> None:
    response = await client.post(_RUNS, json=body, headers=_OWNER)

    assert response.status_code == 400, response.text
    assert _runner_starts(runner) == []
    assert policy["calls"] == []


@pytest.mark.asyncio
async def test_copy_on_write_sessions_get_409(
    client: httpx.AsyncClient, runner: _RunnerClient, policy: dict[str, Any]
) -> None:
    runner.copy_on_write = True

    response = await client.post(_RUNS, json=_body(timeout=0), headers=_OWNER)

    assert response.status_code == 409
    assert response.json()["code"] == "copy_on_write"
    assert _runner_starts(runner) == []


@pytest.mark.asyncio
async def test_a_fifth_running_run_gets_429(
    client: httpx.AsyncClient, runner: _RunnerClient, policy: dict[str, Any]
) -> None:
    runner.running = [{"run_id": f"run_{index}", "offset": 0, "output": []} for index in range(4)]

    response = await client.post(_RUNS, json=_body(), headers=_OWNER)

    assert response.status_code == 429
    assert response.json()["code"] == "too_many_runs"
    assert _runner_starts(runner) == []
    assert policy["calls"] == []


@pytest.mark.asyncio
async def test_the_runner_limit_still_reaches_the_client_as_429(
    client: httpx.AsyncClient, runner: _RunnerClient, policy: dict[str, Any], store: _ItemStore
) -> None:
    runner.start_status = 429

    response = await client.post(_RUNS, json=_body(), headers=_OWNER)

    assert response.status_code == 429
    assert [_phase(item) for item in store.appended] == ["started", "finished"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_status", "start_error", "client_status"),
    [
        (429, None, 429),
        (409, None, 409),
        (400, None, 400),
    ],
    ids=["runner-limit", "runner-copy-on-write", "runner-invalid-input"],
)
async def test_a_refused_forward_saves_a_failed_finished_item(
    client: httpx.AsyncClient,
    runner: _RunnerClient,
    policy: dict[str, Any],
    store: _ItemStore,
    published: list[dict[str, Any]],
    start_status: int,
    start_error: Exception | None,
    client_status: int,
) -> None:
    from omnigent.entities.conversation import code_run_source_id
    from omnigent.server.routes._sessions.helpers import _external_item_stable_id

    runner.start_status = start_status
    runner.start_error = start_error

    response = await client.post(_RUNS, json=_body(), headers=_OWNER)

    assert response.status_code == client_status
    started, finished = store.appended
    assert isinstance(started.data, CodeRunData) and isinstance(finished.data, CodeRunData)
    assert finished.data.run_id == started.data.run_id
    assert (finished.data.phase, finished.data.status) == ("finished", "failed")
    assert finished.data.exit_code is None
    assert finished.data.output and finished.data.output[0].stream == "stderr"
    # The same id the runner's own finished post would get, so a late one dedupes.
    assert finished.stable_id == _external_item_stable_id(
        _SESSION, code_run_source_id(started.data.run_id, "finished")
    )
    assert [event["type"] for event in published] == [
        "session.code_run.started",
        "session.code_run.finished",
    ]


@pytest.mark.asyncio
async def test_started_is_saved_before_the_run_is_forwarded(
    client: httpx.AsyncClient,
    runner: _RunnerClient,
    policy: dict[str, Any],
    store: _ItemStore,
    published: list[dict[str, Any]],
) -> None:
    runner.store = store

    response = await client.post(_RUNS, json=_body(), headers=_OWNER)

    assert response.status_code == 202
    assert runner.saved_before_post == [["started"]]
    [started] = store.appended
    assert isinstance(started.data, CodeRunData)
    assert runner.calls[-1][2]["run_id"] == started.data.run_id == response.json()["run_id"]


# ── Policy ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_policy_deny_returns_403(
    client: httpx.AsyncClient, runner: _RunnerClient, policy: dict[str, Any], store: _ItemStore
) -> None:
    policy["result"] = PolicyResult(action=PolicyAction.DENY, reason="No shell here.")

    response = await client.post(_RUNS, json=_body(), headers=_OWNER)

    assert (response.status_code, response.json()) == (
        403,
        {"code": "policy_denied", "reason": "No shell here."},
    )
    assert _runner_starts(runner) == []
    assert store.appended == []


@pytest.mark.asyncio
async def test_policy_ask_needs_approval(
    client: httpx.AsyncClient, runner: _RunnerClient, policy: dict[str, Any]
) -> None:
    policy["result"] = PolicyResult(action=PolicyAction.ASK, reason="Confirm network access.")

    response = await client.post(_RUNS, json=_body(), headers=_OWNER)

    assert (response.status_code, response.json()) == (
        409,
        {"code": "needs_approval", "reason": "Confirm network access."},
    )
    assert _runner_starts(runner) == []


@pytest.mark.asyncio
async def test_approved_ask_runs_and_records_the_approver(
    client: httpx.AsyncClient,
    runner: _RunnerClient,
    policy: dict[str, Any],
    store: _ItemStore,
    published: list[dict[str, Any]],
) -> None:
    policy["result"] = PolicyResult(action=PolicyAction.ASK, reason="Confirm.")

    response = await client.post(_RUNS, json=_body(approved=True), headers=_OWNER)

    assert response.status_code == 202, response.text
    data = store.appended[0].data
    assert isinstance(data, CodeRunData)
    assert data.approved_by == "real-owner@example.com"
    assert policy["calls"] == [
        {
            "command": "echo hi\nls",
            "timeout": 120,
            "actor": {"run_as": "real-owner@example.com", "initiated_by": "user"},
        }
    ]


# ── Success ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["real-owner@example.com", "admin@example.com"])
async def test_owner_start_forwards_saves_and_publishes(
    client: httpx.AsyncClient,
    runner: _RunnerClient,
    policy: dict[str, Any],
    store: _ItemStore,
    published: list[dict[str, Any]],
    caller: str,
) -> None:
    response = await client.post(
        _RUNS, json=_body(timeout=30), headers={"X-Forwarded-Email": caller}
    )

    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert run_id.startswith("run_")
    assert [call[0] for call in runner.calls] == ["GET", "POST"]
    assert runner.calls[1][2] == {
        "run_id": run_id,
        "language": "shell",
        "command": "echo hi\nls",
        "timeout": 30,
    }
    [item] = store.appended
    assert item.type == "code_run"
    assert isinstance(item.data, CodeRunData)
    assert item.data.model_dump(exclude_none=True) == {
        "run_id": run_id,
        "phase": "started",
        "message_id": "msg_1",
        "block_hash": _hash(_CODE),
        "language": "shell",
        "command": "echo hi\nls",
        "timeout": 30,
        "started_at": item.data.started_at,
    }
    assert [event["type"] for event in published] == ["session.code_run.started"]
    assert published[0]["item"]["run_id"] == run_id


@pytest.mark.asyncio
async def test_console_block_runs_only_its_prompt_lines(
    client: httpx.AsyncClient,
    runner: _RunnerClient,
    policy: dict[str, Any],
    published: list[dict[str, Any]],
) -> None:
    response = await client.post(_RUNS, json=_body(code="$ pwd\n/tmp"), headers=_OWNER)

    assert response.status_code == 202, response.text
    assert runner.calls[-1][2]["command"] == "pwd"


@pytest.mark.asyncio
async def test_real_policy_path_evaluates_sys_os_shell_as_the_user(
    client: httpx.AsyncClient,
    runner: _RunnerClient,
    store: _ItemStore,
    published: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The policy context carries the tool, arguments and user-initiated actor."""
    from omnigent.server.routes import sessions as sessions_facade

    seen: list[EvaluationContext] = []

    class _Agent:
        id = "ag_test"
        bundle_location = "/bundle"
        operator_authored = False

    class _Loaded:
        spec = object()

    class _Caps:
        default_policies: list[object] = []
        policy_llm_connection_factory = None
        llm = None

    class _Engine:
        async def evaluate(
            self, ctx: EvaluationContext, *, read_only: bool = False
        ) -> PolicyResult:
            seen.append(ctx)
            return PolicyResult(action=PolicyAction.DENY, reason="denied by test")

    class _AgentStore:
        def get(self, agent_id: str) -> _Agent:
            return _Agent()

    class _AgentCache:
        def load(self, *args: Any, **kwargs: Any) -> _Loaded:
            return _Loaded()

    monkeypatch.setattr(sessions_facade, "get_agent_cache", lambda: _AgentCache())
    monkeypatch.setattr(sessions_facade, "get_caps", lambda: _Caps())
    monkeypatch.setattr(routes_code_runs, "any_policies_apply", lambda **kwargs: True)
    monkeypatch.setattr(routes_code_runs, "build_policy_engine", lambda **kwargs: _Engine())

    conversation = store.get_conversation(_SESSION)
    assert conversation is not None
    result, _ = await routes_code_runs._evaluate_run_policy(
        _SESSION,
        conversation,
        _AgentStore(),  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        command="echo hi",
        timeout=120,
        actor={"run_as": "real-owner@example.com", "initiated_by": "user"},
    )

    assert result is not None and result.action == PolicyAction.DENY
    [ctx] = seen
    assert ctx.tool_name == "sys_os_shell"
    assert ctx.content == {
        "name": "sys_os_shell",
        "arguments": {"command": "echo hi", "timeout": 120},
    }
    assert ctx.actor == {"run_as": "real-owner@example.com", "initiated_by": "user"}


@pytest.mark.asyncio
async def test_no_agent_means_no_policy_and_the_run_starts(
    client: httpx.AsyncClient, runner: _RunnerClient, published: list[dict[str, Any]]
) -> None:
    response = await client.post(_RUNS, json=_body(), headers=_OWNER)

    assert response.status_code == 202, response.text


# ── Stop ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_stop_is_proxied_and_unknown_runs_return_200(
    client: httpx.AsyncClient, runner: _RunnerClient
) -> None:
    response = await client.post(f"{_RUNS}/run_unknown/stop", json={}, headers=_OWNER)

    assert response.status_code == 200
    assert runner.calls == [("POST", f"{_RUNS}/run_unknown/stop", {})]


@pytest.mark.asyncio
async def test_stop_after_a_handoff_reaches_the_new_runner(
    client: httpx.AsyncClient,
    runner: _RunnerClient,
    policy: dict[str, Any],
    published: list[dict[str, Any]],
) -> None:
    """Each call resolves the session's runner afresh, so a rebind is followed."""
    started = await client.post(_RUNS, json=_body(), headers=_OWNER)
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    new_runner = _RunnerClient()
    set_runner_router(_FakeRunnerRouter(new_runner))  # type: ignore[arg-type]

    stopped = await client.post(f"{_RUNS}/{run_id}/stop", json={}, headers=_OWNER)

    assert stopped.status_code == 200
    assert new_runner.calls == [("POST", f"{_RUNS}/{run_id}/stop", {})]
    assert [call[0] for call in runner.calls] == ["GET", "POST"]


# ── Snapshot on subscribe ─────────────────────────────────────────


class _StateRequest:
    def __init__(self, flag_on: bool) -> None:
        app = FastAPI()
        app.state.feature_flags = resolve_feature_flags(
            {FEATURES_ENV_VAR: "code_block_run" if flag_on else ""}
        )
        self.app = app


@pytest.mark.asyncio
async def test_subscribe_snapshot_sends_one_event_per_running_run(runner: _RunnerClient) -> None:
    runner.running = [
        {"run_id": "run_a", "offset": 3, "output": [{"stream": "stdout", "text": "abc"}]},
        {"run_id": "run_b", "offset": 0, "output": []},
    ]

    events = await routes_code_runs.code_run_snapshot_events(
        _StateRequest(flag_on=True),  # type: ignore[arg-type]
        runner,  # type: ignore[arg-type]
        _SESSION,
    )

    assert events == [
        {
            "type": "session.code_run.snapshot",
            "sequence_number": None,
            "run_id": "run_a",
            "offset": 3,
            "output": [{"stream": "stdout", "text": "abc"}],
        },
        {
            "type": "session.code_run.snapshot",
            "sequence_number": None,
            "run_id": "run_b",
            "offset": 0,
            "output": [],
        },
    ]
    assert runner.calls == [("GET", _RUNS, {"status": "running"})]


@pytest.mark.asyncio
async def test_subscribe_snapshot_skips_the_runner_when_the_flag_is_off(
    runner: _RunnerClient,
) -> None:
    runner.running = [{"run_id": "run_a", "offset": 0, "output": []}]

    events = await routes_code_runs.code_run_snapshot_events(
        _StateRequest(flag_on=False),  # type: ignore[arg-type]
        runner,  # type: ignore[arg-type]
        _SESSION,
    )

    assert events == []
    assert runner.calls == []


class _LabelEngine:
    """Engine stand-in that records label writes."""

    def __init__(self) -> None:
        self.label_writes: list[dict[str, str]] = []

    def apply_label_writes(self, labels: dict[str, str]) -> None:
        self.label_writes.append(labels)


def _shell_call(command: str = "echo hi\nls", timeout: int = 120) -> dict[str, Any]:
    return {"name": "sys_os_shell", "arguments": {"command": command, "timeout": timeout}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "approved"),
    [(PolicyAction.ALLOW, False), (PolicyAction.ASK, True)],
    ids=["allow", "approved-ask"],
)
async def test_a_policy_rewrite_of_the_command_is_refused(
    client: httpx.AsyncClient,
    runner: _RunnerClient,
    policy: dict[str, Any],
    store: _ItemStore,
    action: PolicyAction,
    approved: bool,
) -> None:
    """The native shell path never applies rewrites, so a rewritten run is refused."""
    policy["result"] = PolicyResult(
        action=action, reason="redacted", data=_shell_call(command="echo [redacted]")
    )

    response = await client.post(_RUNS, json=_body(approved=approved), headers=_OWNER)

    assert response.status_code == 403
    assert response.json()["code"] == "policy_modified_unsupported"
    assert _runner_starts(runner) == []
    assert store.appended == []


@pytest.mark.asyncio
async def test_an_unchanged_policy_payload_still_runs(
    client: httpx.AsyncClient,
    runner: _RunnerClient,
    policy: dict[str, Any],
    published: list[dict[str, Any]],
) -> None:
    policy["result"] = PolicyResult(action=PolicyAction.ALLOW, data=_shell_call())

    response = await client.post(_RUNS, json=_body(), headers=_OWNER)

    assert response.status_code == 202, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [PolicyAction.ALLOW, PolicyAction.DENY])
async def test_label_writes_land_on_allow_and_deny_like_the_hook_route(
    client: httpx.AsyncClient,
    runner: _RunnerClient,
    policy: dict[str, Any],
    published: list[dict[str, Any]],
    action: PolicyAction,
) -> None:
    engine = _LabelEngine()
    policy["engine"] = engine
    policy["result"] = PolicyResult(action=action, reason="r", set_labels={"ran_code": "1"})

    await client.post(_RUNS, json=_body(), headers=_OWNER)

    assert engine.label_writes == [{"ran_code": "1"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [_RUNS, f"{_RUNS}/run_1/stop"], ids=["start", "stop"])
@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 401),
        ({"X-Forwarded-Email": "viewer@example.com"}, 403),
        ({"X-Forwarded-Email": "owner@example.com"}, 403),
        (_OWNER, 415),
    ],
    ids=["unauthenticated", "read-only", "edit", "owner"],
)
async def test_the_owner_check_runs_before_the_content_type_check(
    client: httpx.AsyncClient,
    runner: _RunnerClient,
    path: str,
    headers: dict[str, str],
    status: int,
) -> None:
    response = await client.post(
        path, content="{}", headers={**headers, "Content-Type": "text/plain"}
    )

    assert response.status_code == status, response.text
    assert runner.calls == []


@pytest.mark.asyncio
async def test_deny_wins_over_approved(
    client: httpx.AsyncClient, runner: _RunnerClient, policy: dict[str, Any], store: _ItemStore
) -> None:
    policy["result"] = PolicyResult(action=PolicyAction.DENY, reason="No.")

    response = await client.post(_RUNS, json=_body(approved=True), headers=_OWNER)

    assert (response.status_code, response.json()["code"]) == (403, "policy_denied")
    assert _runner_starts(runner) == []
    assert store.appended == []


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    recorded: dict[str, object] = {}

    def record(**attrs: object) -> None:
        recorded.update({key: value for key, value in attrs.items() if value is not None})

    monkeypatch.setattr(routes_code_runs, "add_audit_attrs", record)
    return recorded


_AUDIT_KEYS = {
    "code_run_id",
    "code_run_language",
    "code_run_command_length",
    "code_run_policy_decision",
    "code_run_approved_by",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "body", "status", "decision", "approved_by"),
    [
        (None, _body(), 202, "allow", ""),
        (PolicyResult(action=PolicyAction.DENY, reason="no"), _body(), 403, "deny", ""),
        (PolicyResult(action=PolicyAction.ASK, reason="ok?"), _body(), 409, "ask", ""),
        (
            PolicyResult(action=PolicyAction.ASK, reason="ok?"),
            _body(approved=True),
            202,
            "ask",
            "real-owner@example.com",
        ),
    ],
    ids=["allow", "deny", "ask", "approved-ask"],
)
async def test_every_policy_outcome_is_audited(
    client: httpx.AsyncClient,
    runner: _RunnerClient,
    policy: dict[str, Any],
    published: list[dict[str, Any]],
    audit: dict[str, object],
    result: PolicyResult | None,
    body: dict[str, Any],
    status: int,
    decision: str,
    approved_by: str,
) -> None:
    policy["result"] = result

    response = await client.post(_RUNS, json=body, headers=_OWNER)

    assert response.status_code == status
    assert set(audit) == _AUDIT_KEYS
    assert audit["code_run_language"] == "shell"
    assert audit["code_run_command_length"] == len("echo hi\nls")
    assert (audit["code_run_policy_decision"], audit["code_run_approved_by"]) == (
        decision,
        approved_by,
    )


@pytest.mark.asyncio
async def test_refusals_before_policy_are_audited_as_not_evaluated(
    client: httpx.AsyncClient,
    runner: _RunnerClient,
    policy: dict[str, Any],
    audit: dict[str, object],
) -> None:
    runner.copy_on_write = True

    response = await client.post(_RUNS, json=_body(), headers=_OWNER)

    assert response.status_code == 409
    assert audit["code_run_policy_decision"] == "not_evaluated"
    assert audit["code_run_approved_by"] == ""
    assert audit["code_run_language"] == "shell"
    assert str(audit["code_run_id"]).startswith("run_")


@pytest.mark.asyncio
async def test_stop_is_audited_with_its_outcome(
    client: httpx.AsyncClient, runner: _RunnerClient, audit: dict[str, object]
) -> None:
    response = await client.post(f"{_RUNS}/run_9/stop", json={}, headers=_OWNER)

    assert response.status_code == 200
    assert audit == {"code_run_id": "run_9", "code_run_stopped": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start_error",
    [httpx.ConnectError("runner down"), httpx.ReadTimeout("no answer")],
    ids=["connect-error", "read-timeout"],
)
@pytest.mark.parametrize("stop_fails", [False, True], ids=["stop-sent", "stop-fails"])
async def test_an_ambiguous_forward_saves_no_finished_and_stops_the_run(
    client: httpx.AsyncClient,
    runner: _RunnerClient,
    policy: dict[str, Any],
    store: _ItemStore,
    published: list[dict[str, Any]],
    start_error: Exception,
    stop_fails: bool,
) -> None:
    """The run may have started, so no synthetic result; a best-effort stop ends it."""
    runner.start_error = start_error
    if stop_fails:
        runner.stop_error = httpx.ConnectError("still down")

    response = await client.post(_RUNS, json=_body(), headers=_OWNER)

    assert response.status_code == 502
    [started] = store.appended
    assert isinstance(started.data, CodeRunData) and started.data.phase == "started"
    run_id = started.data.run_id
    assert runner.calls[-1] == ("POST", f"{_RUNS}/{run_id}/stop", {})
    assert [event["type"] for event in published] == ["session.code_run.started"]


@pytest.mark.asyncio
async def test_the_runners_real_finished_lands_after_an_ambiguous_forward(
    client: httpx.AsyncClient,
    runner: _RunnerClient,
    policy: dict[str, Any],
    store: _ItemStore,
    published: list[dict[str, Any]],
) -> None:
    """The runner's own finished post is saved, not deduped behind a synthetic one."""
    from omnigent.entities.conversation import code_run_source_id
    from omnigent.server.routes._sessions.orchestration import (
        _persist_external_conversation_item,
    )
    from omnigent.server.schemas import SessionEventInput

    runner.start_error = httpx.ReadTimeout("no answer")
    response = await client.post(_RUNS, json=_body(), headers=_OWNER)
    assert response.status_code == 502
    [started] = store.appended
    assert isinstance(started.data, CodeRunData)
    run_id = started.data.run_id
    conversation = store.get_conversation(_SESSION)
    assert conversation is not None

    # The body the runner posts once the stopped run ends.
    item_id = await _persist_external_conversation_item(
        _SESSION,
        conversation,
        SessionEventInput(
            type="external_conversation_item",
            data={
                "item_type": "code_run",
                "item_data": {
                    "run_id": run_id,
                    "phase": "finished",
                    "status": "stopped",
                    "exit_code": None,
                    "duration_ms": 40,
                    "output": [{"stream": "stdout", "text": "partial"}],
                    "truncated": False,
                },
                "response_id": run_id,
                "source_id": code_run_source_id(run_id, "finished"),
            },
        ),
        store,  # type: ignore[arg-type]
    )

    finished = store.items[item_id].data
    assert isinstance(finished, CodeRunData)
    assert (finished.phase, finished.status) == ("finished", "stopped")
    assert [_phase(item) for item in store.appended] == ["started", "finished"]
    assert [event["type"] for event in published] == [
        "session.code_run.started",
        "session.code_run.finished",
    ]


async def _save_runner_finished(store: _ItemStore, run_id: str) -> str:
    """Persist the finished item the runner posts once a stopped run ends."""
    from omnigent.entities.conversation import code_run_source_id
    from omnigent.server.routes._sessions.orchestration import (
        _persist_external_conversation_item,
    )
    from omnigent.server.schemas import SessionEventInput

    conversation = store.get_conversation(_SESSION)
    assert conversation is not None
    return await _persist_external_conversation_item(
        _SESSION,
        conversation,
        SessionEventInput(
            type="external_conversation_item",
            data={
                "item_type": "code_run",
                "item_data": {
                    "run_id": run_id,
                    "phase": "finished",
                    "status": "stopped",
                    "exit_code": None,
                    "duration_ms": 40,
                    "output": [{"stream": "stdout", "text": "partial"}],
                    "truncated": False,
                },
                "response_id": run_id,
                "source_id": code_run_source_id(run_id, "finished"),
            },
        ),
        store,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_status", "start_body", "start_content"),
    [
        (500, {"detail": "Internal Server Error"}, None),
        (502, {"detail": "runner tunnel closed"}, None),
        (503, {"error": {"code": "runner_unavailable"}}, None),
        (409, {"detail": "conflict"}, None),
        (429, {"message": "rate limited by proxy"}, None),
        (409, None, b"<html>Conflict</html>"),
        (503, None, b""),
        (202, {"accepted": True}, None),
    ],
    ids=[
        "500",
        "502",
        "503",
        "409-not-runner",
        "429-proxy",
        "malformed-409",
        "empty-503",
        "202-without-run-id",
    ],
)
async def test_a_non_runner_error_response_is_ambiguous(
    client: httpx.AsyncClient,
    runner: _RunnerClient,
    policy: dict[str, Any],
    store: _ItemStore,
    published: list[dict[str, Any]],
    start_status: int,
    start_body: Any,
    start_content: bytes | None,
) -> None:
    """A response the runner route did not write may follow a started run."""
    runner.start_status = start_status
    runner.start_body = start_body
    runner.start_content = start_content

    response = await client.post(_RUNS, json=_body(), headers=_OWNER)

    assert response.status_code == 502
    [started] = store.appended
    assert isinstance(started.data, CodeRunData)
    run_id = started.data.run_id
    assert runner.calls[-1] == ("POST", f"{_RUNS}/{run_id}/stop", {})
    assert [event["type"] for event in published] == ["session.code_run.started"]

    # Nothing synthetic sits under the runner's stable id, so its real result lands.
    item_id = await _save_runner_finished(store, run_id)
    finished = store.items[item_id].data
    assert isinstance(finished, CodeRunData)
    assert (finished.phase, finished.status) == ("finished", "stopped")
