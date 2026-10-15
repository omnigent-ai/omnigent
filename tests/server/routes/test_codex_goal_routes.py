"""Tests for the Codex goal routes (``omnigent/server/routes/codex/sessions.py``).

Goal state lives in the Codex app-server, so every route is a thin forward
to the session's runner with careful handling of the ways that forward can
fail: no runner, a runner that must be woken first, structured vs malformed
runner errors, and payloads the response model rejects. The helpers are
exercised directly with fakes; the four HTTP routes run on the real server
app with the forward step staged.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi.responses import JSONResponse
from starlette.datastructures import State

from omnigent._wrapper_labels import CODEX_NATIVE_WRAPPER_VALUE, WRAPPER_LABEL_KEY
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.host_registry import RunnerExitReports
from omnigent.server.routes._sessions.helpers import _HostLaunchAttempt
from omnigent.server.routes.codex import sessions as codex
from omnigent.server.routes.sessions import _RunnerForwardResult
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_session_for_agent, create_test_agent

SESSION = "conv_goal"


def _conv(**overrides: Any) -> Conversation:
    base: dict[str, Any] = {
        "id": SESSION,
        "created_at": 0,
        "updated_at": 0,
        "root_conversation_id": SESSION,
        "labels": {WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE},
    }
    base.update(overrides)
    return Conversation(**base)


class _FakeStore:
    def __init__(self, conv: Conversation | None) -> None:
        self.conv = conv

    def get_conversation(self, session_id: str) -> Conversation | None:
        return self.conv


def _Store(conv: Conversation | None) -> ConversationStore:
    """A store that only knows one conversation, typed for the helpers' signatures."""
    return cast(ConversationStore, _FakeStore(conv))


def _result(status: int, body: Any) -> _RunnerForwardResult:
    return _RunnerForwardResult(
        status_code=status, body=body if isinstance(body, str) else json.dumps(body)
    )


def _goal(**overrides: Any) -> dict[str, Any]:
    goal = {
        "thread_id": "thr_1",
        "objective": "keep tests green",
        "status": "active",
        "tokens_used": 0,
        "time_used_seconds": 0,
    }
    goal.update(overrides)
    return goal


# ── _runner_error_payload / _require_codex_goal_runner_payload ─────────────


@pytest.mark.parametrize(
    "body",
    ["{not json", "[1, 2]", '{"error": "x"}', '{"error": 1, "detail": "d"}', '{"detail": "d"}'],
    ids=["invalid-json", "list", "missing-detail", "non-string-error", "missing-error"],
)
def test_runner_error_payload_rejects_unstructured_bodies(body: str) -> None:
    assert codex._runner_error_payload(body) is None


def test_runner_error_payload_extracts_only_error_and_detail() -> None:
    assert codex._runner_error_payload('{"error": "e", "detail": "d", "extra": 1}') == {
        "error": "e",
        "detail": "d",
    }


def _payload_or_response(result: _RunnerForwardResult | None) -> Any:
    return codex._require_codex_goal_runner_payload(SESSION, action="read", runner_result=result)


def _status_and_body(resp: Any) -> tuple[int, Any]:
    assert isinstance(resp, JSONResponse)
    return resp.status_code, json.loads(bytes(resp.body))


def test_unstructured_runner_error_becomes_a_502() -> None:
    status, body = _status_and_body(_payload_or_response(_result(500, "boom")))
    assert status == 502
    assert "malformed error response with status 500" in body["error"]["message"]


@pytest.mark.parametrize("body", ["{not json", "[]"], ids=["invalid-json", "not-an-object"])
def test_malformed_success_body_becomes_a_502(body: str) -> None:
    status, payload = _status_and_body(_payload_or_response(_result(200, body)))
    assert status == 502
    assert "malformed response" in payload["error"]["message"]


# ── _post_codex_goal_event_to_runner ───────────────────────────────────────


async def test_post_goal_event_returns_status_and_body() -> None:
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"goal": None})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="http://runner"
    ) as client:
        result = await codex._post_codex_goal_event_to_runner(
            SESSION, client, {"type": "goal_get"}
        )
    assert result == _RunnerForwardResult(status_code=201, body='{"goal":null}')
    assert seen[0].url.path == f"/v1/sessions/{SESSION}/events"
    assert json.loads(seen[0].content) == {"type": "goal_get"}


async def test_post_goal_event_swallows_transport_errors() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("runner gone")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="http://runner"
    ) as client:
        assert await codex._post_codex_goal_event_to_runner(SESSION, client, {"type": "x"}) is None


# ── _wait_for_existing_codex_goal_runner ───────────────────────────────────


async def test_wait_for_existing_runner_skips_without_a_binding_or_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _never(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("must not wait")

    monkeypatch.setattr(codex, "_wait_for_runner_client", _never)
    kwargs: dict[str, Any] = {
        "session_id": SESSION,
        "runner_router": None,
        "tunnel_registry": None,
        "runner_exit_reports": None,
    }
    assert (
        await codex._wait_for_existing_codex_goal_runner(conv=_conv(runner_id=None), **kwargs)
        is None
    )
    monkeypatch.setattr(codex, "_HOST_BOUND_RUNNER_CONNECT_GRACE_S", 0)
    assert (
        await codex._wait_for_existing_codex_goal_runner(conv=_conv(runner_id="r1"), **kwargs)
        is None
    )


async def test_wait_for_existing_runner_delegates_with_the_bound_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    sentinel = object()

    async def _wait(session_id: str, router: Any, registry: Any, **kwargs: Any) -> Any:
        seen.update(session_id=session_id, **kwargs)
        return sentinel

    monkeypatch.setattr(codex, "_wait_for_runner_client", _wait)
    monkeypatch.setattr(codex, "_HOST_BOUND_RUNNER_CONNECT_GRACE_S", 3.5)
    got = await codex._wait_for_existing_codex_goal_runner(
        session_id=SESSION,
        conv=_conv(runner_id="r1"),
        runner_router=None,
        tunnel_registry=None,
        runner_exit_reports=cast(RunnerExitReports, "reports"),
    )
    assert got is sentinel
    assert seen == {
        "session_id": SESSION,
        "runner_id": "r1",
        "timeout_s": 3.5,
        "runner_exit_reports": "reports",
    }


# ── _start_codex_goal_runner_on_bound_host ─────────────────────────────────


async def _start(conv: Conversation, app_state: State, store: Any = None) -> str | None:
    return await codex._start_codex_goal_runner_on_bound_host(
        session_id=SESSION, conv=conv, app_state=app_state, conversation_store=store
    )


async def test_start_on_bound_host_needs_a_host_registry() -> None:
    assert await _start(_conv(host_id="h1"), State()) is None


async def test_start_on_bound_host_launches_through_the_live_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_conn = object()
    launches: list[Any] = []

    async def _launch(conv: Any, store: Any, registry: Any, conn: Any) -> _HostLaunchAttempt:
        launches.append(conn)
        return _HostLaunchAttempt(runner_id="r-new")

    monkeypatch.setattr(codex, "_launch_runner_on_host", _launch)
    state = State()
    state.host_registry = {"h1": host_conn}
    assert await _start(_conv(host_id="h1"), state) == "r-new"
    assert launches == [host_conn]


async def test_start_on_bound_host_surfaces_harness_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _launch(*args: Any) -> _HostLaunchAttempt:
        return _HostLaunchAttempt(
            runner_id="", error_code=codex._HARNESS_NOT_CONFIGURED_ERROR_CODE, error="no codex"
        )

    monkeypatch.setattr(codex, "_launch_runner_on_host", _launch)
    state = State()
    state.host_registry = {"h1": object()}
    with pytest.raises(OmnigentError) as exc:
        await _start(_conv(host_id="h1"), state)
    assert exc.value.code == ErrorCode.HARNESS_NOT_CONFIGURED
    assert "no codex" in str(exc.value)


async def test_start_on_bound_host_relaunches_a_managed_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relaunch_outcome = {"value": False}

    async def _relaunch(**kwargs: Any) -> bool:
        return relaunch_outcome["value"]

    monkeypatch.setattr(codex, "_maybe_relaunch_managed_sandbox", _relaunch)
    state = State()
    state.host_registry = {}  # host tunnel gone → managed relaunch path
    assert await _start(_conv(host_id="managed"), state, _Store(None)) is None

    relaunch_outcome["value"] = True
    with pytest.raises(OmnigentError) as exc:
        await _start(_conv(host_id="managed"), state, _Store(None))
    assert exc.value.code == ErrorCode.NOT_FOUND

    assert await _start(
        _conv(host_id="managed"), state, _Store(_conv(runner_id="r-relaunched"))
    ) == ("r-relaunched")


# ── _initialize_codex_goal_runner ──────────────────────────────────────────


async def test_initialize_runner_requires_the_session_and_runs_the_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(OmnigentError) as exc:
        await codex._initialize_codex_goal_runner(SESSION, httpx.AsyncClient(), _Store(None))
    assert exc.value.code == ErrorCode.NOT_FOUND

    seen: list[tuple[Any, ...]] = []

    async def _init(*args: Any) -> None:
        seen.append(args)

    monkeypatch.setattr(codex, "_ensure_runner_session_initialized", _init)
    conv = _conv()
    store = _Store(conv)
    client = httpx.AsyncClient()
    await codex._initialize_codex_goal_runner(SESSION, client, store)
    assert seen == [(SESSION, conv, client, store)]


# ── _launch_runner_for_codex_goal / _forward_codex_goal_event ──────────────


def _request() -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=State()))


async def _launch(monkeypatch: pytest.MonkeyPatch, conv: Conversation, **stages: Any) -> Any:
    async def _access(*args: Any) -> None:
        return None

    monkeypatch.setattr(codex, "_require_access", _access)
    for name, value in stages.items():

        async def _stage(*args: Any, _value: Any = value, **kwargs: Any) -> Any:
            return _value

        monkeypatch.setattr(codex, name, _stage)
    return await codex._launch_runner_for_codex_goal(
        session_id=SESSION,
        conv=conv,
        request=_request(),
        user_id=None,
        runner_router=None,
        conversation_store=_Store(conv),
        permission_store=None,
        runner_exit_reports=None,
    )


async def test_launch_for_goal_needs_a_host_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _launch(monkeypatch, _conv(host_id=None)) is None


async def test_launch_for_goal_reuses_a_runner_that_is_still_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = object()
    assert (
        await _launch(
            monkeypatch, _conv(host_id="h1"), _wait_for_existing_codex_goal_runner=existing
        )
        is existing
    )


async def test_launch_for_goal_gives_up_when_the_new_runner_never_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        await _launch(
            monkeypatch,
            _conv(host_id="h1"),
            _wait_for_existing_codex_goal_runner=None,
            _start_codex_goal_runner_on_bound_host="r-new",
            _wait_for_runner_client=None,
        )
        is None
    )


async def test_launch_for_goal_initializes_the_relaunched_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relaunched = object()
    initialized: list[Any] = []

    async def _init(session_id: str, client: Any, store: Any) -> None:
        initialized.append(client)

    monkeypatch.setattr(codex, "_initialize_codex_goal_runner", _init)
    got = await _launch(
        monkeypatch,
        _conv(host_id="h1"),
        _wait_for_existing_codex_goal_runner=None,
        _start_codex_goal_runner_on_bound_host="r-new",
        _wait_for_runner_client=relaunched,
    )
    assert got is relaunched
    assert initialized == [relaunched]


async def _forward(monkeypatch: pytest.MonkeyPatch, event: dict[str, Any], **stages: Any) -> Any:
    for name, value in stages.items():

        async def _stage(*args: Any, _value: Any = value, **kwargs: Any) -> Any:
            return _value

        monkeypatch.setattr(codex, name, _stage)
    return await codex._forward_codex_goal_event(
        session_id=SESSION,
        conv=_conv(host_id="h1"),
        event=event,
        request=_request(),
        user_id=None,
        runner_router=None,
        conversation_store=_Store(_conv()),
        permission_store=None,
        runner_exit_reports=None,
    )


async def test_forward_uses_the_live_runner_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    live = _result(200, {"goal": None})
    assert (
        await _forward(monkeypatch, {"type": "goal_set"}, _forward_session_change_to_runner=live)
        is live
    )


async def test_forward_never_wakes_a_runner_just_to_read(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _never(**kwargs: Any) -> None:
        raise AssertionError("goal_get must not relaunch")

    monkeypatch.setattr(codex, "_launch_runner_for_codex_goal", _never)
    assert (
        await _forward(monkeypatch, {"type": "goal_get"}, _forward_session_change_to_runner=None)
        is None
    )


async def test_forward_wakes_the_runner_then_retries_on_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    woken = object()
    retried = _result(200, {"goal": None})
    posted: list[Any] = []

    async def _post(session_id: str, client: Any, event: dict[str, Any]) -> Any:
        posted.append((client, event))
        return retried

    monkeypatch.setattr(codex, "_post_codex_goal_event_to_runner", _post)
    got = await _forward(
        monkeypatch,
        {"type": "goal_set", "objective": "x"},
        _forward_session_change_to_runner=None,
        _launch_runner_for_codex_goal=woken,
    )
    assert got is retried
    assert posted == [(woken, {"type": "goal_set", "objective": "x"})]

    assert (
        await _forward(
            monkeypatch,
            {"type": "goal_clear"},
            _forward_session_change_to_runner=None,
            _launch_runner_for_codex_goal=None,
        )
        is None
    )


# ── _require_codex_native_goal_session ─────────────────────────────────────


async def test_goal_session_guard() -> None:
    with pytest.raises(OmnigentError) as missing:
        await codex._require_codex_native_goal_session(SESSION, _Store(None))
    assert missing.value.code == ErrorCode.NOT_FOUND

    with pytest.raises(OmnigentError) as wrong:
        await codex._require_codex_native_goal_session(SESSION, _Store(_conv(labels={})))
    assert wrong.value.code == ErrorCode.INVALID_INPUT

    conv = _conv()
    assert await codex._require_codex_native_goal_session(SESSION, _Store(conv)) is conv


# ── The HTTP routes on the real server app ─────────────────────────────────


@pytest.fixture
async def codex_session(client: httpx.AsyncClient, db_uri: str) -> str:
    agent = await create_test_agent(client, name="codex-goal-agent")
    session_id = await create_session_for_agent(client, agent["id"])
    SqlAlchemyConversationStore(db_uri).set_labels(
        session_id, {WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE}
    )
    return session_id


class _StagedForward:
    """Replace ``_forward_codex_goal_event`` with a canned result; record events."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, result: _RunnerForwardResult | None):
        self.result = result
        self.events: list[dict[str, Any]] = []

        async def _forward(**kwargs: Any) -> _RunnerForwardResult | None:
            self.events.append(kwargs["event"])
            return self.result

        monkeypatch.setattr(codex, "_forward_codex_goal_event", _forward)


async def test_get_goal_without_a_runner_reads_as_no_goal(
    client: httpx.AsyncClient, codex_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = _StagedForward(monkeypatch, None)
    resp = await client.get(f"/v1/sessions/{codex_session}/codex_goal")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"goal": None}
    assert staged.events == [{"type": "goal_get"}]


async def test_get_goal_returns_the_runner_goal(
    client: httpx.AsyncClient, codex_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _StagedForward(monkeypatch, _result(200, {"goal": _goal(token_budget=5000)}))
    resp = await client.get(f"/v1/sessions/{codex_session}/codex_goal")
    assert resp.status_code == 200, resp.text
    assert resp.json()["goal"]["objective"] == "keep tests green"
    assert resp.json()["goal"]["token_budget"] == 5000


async def test_get_goal_rejects_a_goal_the_model_cannot_validate(
    client: httpx.AsyncClient, codex_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _StagedForward(monkeypatch, _result(200, {"goal": {"objective": "no thread id"}}))
    resp = await client.get(f"/v1/sessions/{codex_session}/codex_goal")
    assert resp.status_code == 502
    assert "malformed response" in resp.json()["error"]["message"]


async def test_get_goal_passes_structured_runner_errors_through(
    client: httpx.AsyncClient, codex_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _StagedForward(monkeypatch, _result(409, {"error": "goal_busy", "detail": "turn running"}))
    resp = await client.get(f"/v1/sessions/{codex_session}/codex_goal")
    assert resp.status_code == 409
    assert resp.json() == {"error": "goal_busy", "detail": "turn running"}


async def test_set_goal_forwards_only_the_fields_the_caller_sent(
    client: httpx.AsyncClient, codex_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = _StagedForward(monkeypatch, _result(200, {"goal": _goal()}))
    resp = await client.put(
        f"/v1/sessions/{codex_session}/codex_goal", json={"objective": "  keep tests green  "}
    )
    assert resp.status_code == 200, resp.text
    assert staged.events == [{"type": "goal_set", "objective": "keep tests green"}]

    resp = await client.put(
        f"/v1/sessions/{codex_session}/codex_goal",
        json={"objective": "ship", "token_budget": 10, "status": "paused"},
    )
    assert resp.status_code == 200, resp.text
    assert staged.events[-1] == {
        "type": "goal_set",
        "objective": "ship",
        "token_budget": 10,
        "status": "paused",
    }


async def test_set_goal_rejects_a_blank_objective(
    client: httpx.AsyncClient, codex_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = _StagedForward(monkeypatch, _result(200, {"goal": _goal()}))
    resp = await client.put(f"/v1/sessions/{codex_session}/codex_goal", json={"objective": "   "})
    assert resp.status_code == 400
    assert staged.events == []


async def test_set_goal_without_a_runner_is_503(
    client: httpx.AsyncClient, codex_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _StagedForward(monkeypatch, None)
    resp = await client.put(f"/v1/sessions/{codex_session}/codex_goal", json={"objective": "x"})
    assert resp.status_code == 503
    assert "Could not set Codex goal" in resp.json()["error"]["message"]


async def test_set_goal_rejects_a_malformed_runner_goal(
    client: httpx.AsyncClient, codex_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _StagedForward(monkeypatch, _result(200, {"goal": "nope"}))
    resp = await client.put(f"/v1/sessions/{codex_session}/codex_goal", json={"objective": "x"})
    assert resp.status_code == 502
    assert "Could not set Codex goal" in resp.json()["error"]["message"]


async def test_update_status_forwards_the_status_event(
    client: httpx.AsyncClient, codex_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = _StagedForward(monkeypatch, _result(200, {"goal": _goal(status="paused")}))
    resp = await client.patch(
        f"/v1/sessions/{codex_session}/codex_goal/status", json={"status": "paused"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["goal"]["status"] == "paused"
    assert staged.events == [{"type": "goal_status", "status": "paused"}]

    _StagedForward(monkeypatch, _result(200, {"goal": 1}))
    resp = await client.patch(
        f"/v1/sessions/{codex_session}/codex_goal/status", json={"status": "active"}
    )
    assert resp.status_code == 502
    assert "Could not update Codex goal status" in resp.json()["error"]["message"]


async def test_clear_goal_reports_whether_codex_removed_one(
    client: httpx.AsyncClient, codex_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = _StagedForward(monkeypatch, _result(200, {"cleared": True}))
    resp = await client.delete(f"/v1/sessions/{codex_session}/codex_goal")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"cleared": True}
    assert staged.events == [{"type": "goal_clear"}]

    _StagedForward(monkeypatch, _result(200, {"cleared": "maybe"}))
    resp = await client.delete(f"/v1/sessions/{codex_session}/codex_goal")
    assert resp.status_code == 502
    assert "Could not clear Codex goal" in resp.json()["error"]["message"]


async def test_goal_routes_refuse_non_codex_and_missing_sessions(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = _StagedForward(monkeypatch, _result(200, {"goal": None}))
    agent = await create_test_agent(client, name="plain-agent")
    plain = await create_session_for_agent(client, agent["id"])
    resp = await client.get(f"/v1/sessions/{plain}/codex_goal")
    assert resp.status_code == 400
    assert "codex-native" in resp.json()["error"]["message"]

    resp = await client.delete("/v1/sessions/conv_does_not_exist/codex_goal")
    assert resp.status_code == 404
    assert staged.events == []
