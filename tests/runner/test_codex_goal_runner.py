"""Unit tests for ``omnigent.runner.codex.goal``."""

from __future__ import annotations

import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import JSONResponse

from omnigent.harness_plugins import CODEX_NATIVE_CODING_AGENT
from omnigent.runner.codex.goal import CodexGoalRunner


class _FakeCodexClient:
    def __init__(
        self,
        *,
        response: dict[str, object] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.response = response or {"result": {}}
        self.exc = exc
        self.connect_calls = 0
        self.request_calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    async def connect(self) -> None:
        self.connect_calls += 1

    async def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.request_calls.append((method, params))
        if self.exc is not None:
            raise self.exc
        return self.response

    async def close(self) -> None:
        self.closed = True


def _make_runner(
    bridge_state: object | None = SimpleNamespace(
        socket_path="/tmp/codex.sock",
        thread_id="thread_1",
    ),
    *,
    safe_detail: str = "safe detail",
) -> CodexGoalRunner:
    async def _bridge_state_for_session(
        conv_id: str,
        *,
        action: str,
        missing_state_log_level: int = logging.WARNING,
    ) -> object | None:
        del conv_id, action, missing_state_log_level
        return bridge_state

    return CodexGoalRunner(
        bridge_state_for_session=_bridge_state_for_session,
        client_safe_error_detail=lambda exc, *, context: f"{safe_detail}: {context}: {exc}",
        logger=logging.getLogger("tests.codex_goal_runner"),
    )


def _json_response(response: JSONResponse) -> dict[str, object]:
    return json.loads(response.body.decode("utf-8"))


@pytest.fixture()
def upstream_goal() -> dict[str, object]:
    return {
        "threadId": "thread_1",
        "objective": "Finish parity",
        "status": "active",
        "tokenBudget": 100,
        "tokensUsed": 12,
        "timeUsedSeconds": 5,
        "createdAt": 1700000000,
        "updatedAt": 1700000001,
    }


def test_goal_to_api_maps_camel_case_fields(upstream_goal: dict[str, object]) -> None:
    assert CodexGoalRunner._goal_to_api(upstream_goal) == {
        "thread_id": "thread_1",
        "objective": "Finish parity",
        "status": "active",
        "token_budget": 100,
        "tokens_used": 12,
        "time_used_seconds": 5,
        "created_at": 1700000000,
        "updated_at": 1700000001,
    }


def test_goal_to_api_allows_missing_optional_timestamps(
    upstream_goal: dict[str, object],
) -> None:
    goal = dict(upstream_goal)
    goal["tokenBudget"] = None
    goal.pop("createdAt")
    goal.pop("updatedAt")

    assert CodexGoalRunner._goal_to_api(goal)["created_at"] is None
    assert CodexGoalRunner._goal_to_api(goal)["updated_at"] is None
    assert CodexGoalRunner._goal_to_api(goal)["token_budget"] is None


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("threadId", None, "missing string 'threadId'"),
        ("tokensUsed", True, "missing non-negative integer 'tokensUsed'"),
        ("timeUsedSeconds", -1, "missing non-negative integer 'timeUsedSeconds'"),
        ("createdAt", True, "invalid integer 'createdAt'"),
        ("tokenBudget", 0, "invalid integer 'tokenBudget'"),
    ],
)
def test_goal_to_api_rejects_malformed_fields(
    upstream_goal: dict[str, object],
    field: str,
    value: object,
    match: str,
) -> None:
    goal = dict(upstream_goal)
    goal[field] = value

    with pytest.raises(ValueError, match=match):
        CodexGoalRunner._goal_to_api(goal)


def test_goal_result_requires_result_object() -> None:
    assert CodexGoalRunner._goal_result({"result": {"goal": {}}}, action="read") == {"goal": {}}

    with pytest.raises(ValueError, match="Codex goal read returned no result object"):
        CodexGoalRunner._goal_result({}, action="read")


@pytest.mark.asyncio
async def test_request_returns_bridge_error_without_loaded_bridge() -> None:
    runner = _make_runner(None)

    result = await runner._request(
        "conv_1",
        action="read",
        method="thread/goal/get",
        params={},
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 502
    assert _json_response(result) == {
        "error": "codex_native_goal_failed",
        "detail": "Codex-native goal read requires a loaded Codex bridge.",
    }


@pytest.mark.asyncio
async def test_request_connects_and_forwards_thread_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeCodexClient(response={"result": {"goal": {"threadId": "thread_1"}}})
    monkeypatch.setitem(
        sys.modules,
        "omnigent.harnesses.codex_native.app_server",
        SimpleNamespace(client_for_transport=lambda *_args, **_kwargs: fake_client),
    )
    runner = _make_runner()

    result = await runner._request(
        "conv_1",
        action="read",
        method="thread/goal/get",
        params={"status": "active"},
    )

    assert result == {"goal": {"threadId": "thread_1"}}
    assert fake_client.connect_calls == 1
    assert fake_client.request_calls == [
        ("thread/goal/get", {"threadId": "thread_1", "status": "active"})
    ]
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_request_maps_malformed_upstream_result_to_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeCodexClient(response={"oops": "missing result"})
    monkeypatch.setitem(
        sys.modules,
        "omnigent.harnesses.codex_native.app_server",
        SimpleNamespace(client_for_transport=lambda *_args, **_kwargs: fake_client),
    )
    runner = _make_runner()

    result = await runner._request(
        "conv_1",
        action="read",
        method="thread/goal/get",
        params={},
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 503
    assert _json_response(result) == {
        "error": "codex_native_goal_failed",
        "detail": "Codex-native goal read returned Codex goal read returned no result object.",
    }
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_request_maps_generic_exception_to_safe_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeCodexClient(exc=RuntimeError("boom"))
    monkeypatch.setitem(
        sys.modules,
        "omnigent.harnesses.codex_native.app_server",
        SimpleNamespace(client_for_transport=lambda *_args, **_kwargs: fake_client),
    )
    runner = _make_runner(safe_detail="safe")

    result = await runner._request(
        "conv_1",
        action="read",
        method="thread/goal/get",
        params={},
    )

    assert isinstance(result, JSONResponse)
    assert result.status_code == 503
    assert _json_response(result) == {
        "error": "codex_native_goal_failed",
        "detail": "safe: codex-native goal read: boom",
    }
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_get_allows_absent_goal() -> None:
    runner = _make_runner()
    runner._request = AsyncMock(return_value={"goal": None})  # type: ignore[method-assign]

    response = await runner.get("conv_1")

    assert _json_response(response) == {"goal": None}


@pytest.mark.asyncio
async def test_get_rejects_invalid_goal_object() -> None:
    runner = _make_runner()
    runner._request = AsyncMock(return_value={"goal": "bad"})  # type: ignore[method-assign]

    response = await runner.get("conv_1")

    assert response.status_code == 503
    assert _json_response(response) == {
        "error": "codex_native_goal_failed",
        "detail": "Codex-native goal read returned invalid goal object.",
    }


@pytest.mark.asyncio
async def test_set_normalizes_goal_payload(upstream_goal: dict[str, object]) -> None:
    runner = _make_runner()
    runner._request = AsyncMock(return_value={"goal": upstream_goal})  # type: ignore[method-assign]

    response = await runner.set(
        "conv_1",
        objective="Finish parity",
        token_budget=100,
        token_budget_provided=True,
        status="active",
    )

    assert _json_response(response) == {
        "goal": {
            "thread_id": "thread_1",
            "objective": "Finish parity",
            "status": "active",
            "token_budget": 100,
            "tokens_used": 12,
            "time_used_seconds": 5,
            "created_at": 1700000000,
            "updated_at": 1700000001,
        }
    }
    runner._request.assert_awaited_once_with(
        "conv_1",
        action="set",
        method="thread/goal/set",
        params={"objective": "Finish parity", "tokenBudget": 100, "status": "active"},
    )


@pytest.mark.asyncio
async def test_update_status_normalizes_goal_payload(upstream_goal: dict[str, object]) -> None:
    runner = _make_runner()
    runner._request = AsyncMock(return_value={"goal": upstream_goal})  # type: ignore[method-assign]

    response = await runner.update_status("conv_1", status="paused")

    assert _json_response(response)["goal"]["status"] == "active"
    runner._request.assert_awaited_once_with(
        "conv_1",
        action="update status",
        method="thread/goal/set",
        params={"status": "paused"},
    )


@pytest.mark.asyncio
async def test_clear_passthroughs_json_errors() -> None:
    runner = _make_runner()
    runner._request = AsyncMock(return_value=JSONResponse(status_code=502, content={"error": "x"}))  # type: ignore[method-assign]

    response = await runner.clear("conv_1")

    assert response.status_code == 502
    assert _json_response(response) == {"error": "x"}


@pytest.mark.asyncio
async def test_clear_rejects_non_boolean_cleared_flag() -> None:
    runner = _make_runner()
    runner._request = AsyncMock(return_value={"cleared": "yes"})  # type: ignore[method-assign]

    response = await runner.clear("conv_1")

    assert response.status_code == 503
    assert _json_response(response) == {
        "error": "codex_native_goal_failed",
        "detail": "Codex-native goal clear returned invalid cleared flag.",
    }


@pytest.mark.asyncio
async def test_clear_returns_boolean_cleared_flag() -> None:
    runner = _make_runner()
    runner._request = AsyncMock(return_value={"cleared": True})  # type: ignore[method-assign]

    response = await runner.clear("conv_1")

    assert _json_response(response) == {"cleared": True}


@pytest.mark.asyncio
async def test_handle_event_returns_none_for_non_goal_types() -> None:
    runner = _make_runner()

    response = await runner.handle_event(
        "conv_1",
        "other_event",
        {},
        session_harness_name=lambda _conv_id: CODEX_NATIVE_CODING_AGENT.harness,
    )

    assert response is None


@pytest.mark.asyncio
async def test_handle_event_rejects_non_codex_session() -> None:
    runner = _make_runner()

    response = await runner.handle_event(
        "conv_1",
        "goal_get",
        {},
        session_harness_name=lambda _conv_id: "pi",
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    assert _json_response(response) == {
        "error": "invalid_input",
        "detail": "Codex goal controls require a codex-native session",
    }


@pytest.mark.asyncio
async def test_handle_event_goal_get_and_clear_delegate() -> None:
    runner = _make_runner()
    runner.get = AsyncMock(return_value=JSONResponse({"goal": None}))  # type: ignore[method-assign]
    runner.clear = AsyncMock(return_value=JSONResponse({"cleared": True}))  # type: ignore[method-assign]

    get_response = await runner.handle_event(
        "conv_1",
        "goal_get",
        {},
        session_harness_name=lambda _conv_id: CODEX_NATIVE_CODING_AGENT.harness,
    )
    clear_response = await runner.handle_event(
        "conv_1",
        "goal_clear",
        {},
        session_harness_name=lambda _conv_id: CODEX_NATIVE_CODING_AGENT.harness,
    )

    assert isinstance(get_response, JSONResponse)
    assert isinstance(clear_response, JSONResponse)
    assert _json_response(get_response) == {"goal": None}
    assert _json_response(clear_response) == {"cleared": True}
    runner.get.assert_awaited_once_with("conv_1")
    runner.clear.assert_awaited_once_with("conv_1")


@pytest.mark.asyncio
async def test_handle_event_goal_set_validates_and_trims_objective() -> None:
    runner = _make_runner()
    runner.set = AsyncMock(return_value=JSONResponse({"ok": True}))  # type: ignore[method-assign]

    response = await runner.handle_event(
        "conv_1",
        "goal_set",
        {"objective": "  Finish parity  ", "token_budget": 42, "status": "paused"},
        session_harness_name=lambda _conv_id: CODEX_NATIVE_CODING_AGENT.harness,
    )

    assert isinstance(response, JSONResponse)
    assert _json_response(response) == {"ok": True}
    runner.set.assert_awaited_once_with(
        "conv_1",
        objective="Finish parity",
        token_budget=42,
        token_budget_provided=True,
        status="paused",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({}, "Body 'objective' must be a non-empty string"),
        ({"objective": "x" * 4001}, "Body 'objective' must be at most 4000 characters"),
        (
            {"objective": "ok", "token_budget": 0},
            "Body 'token_budget' must be a positive integer or null",
        ),
        ({"objective": "ok", "status": "done"}, "Body 'status' must be 'active' or 'paused'"),
    ],
)
async def test_handle_event_goal_set_rejects_invalid_body(
    body: dict[str, object],
    detail: str,
) -> None:
    runner = _make_runner()

    response = await runner.handle_event(
        "conv_1",
        "goal_set",
        body,
        session_harness_name=lambda _conv_id: CODEX_NATIVE_CODING_AGENT.harness,
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    assert _json_response(response) == {"error": "invalid_input", "detail": detail}


@pytest.mark.asyncio
async def test_handle_event_goal_status_rejects_invalid_status() -> None:
    runner = _make_runner()

    response = await runner.handle_event(
        "conv_1",
        "goal_status",
        {"status": "done"},
        session_harness_name=lambda _conv_id: CODEX_NATIVE_CODING_AGENT.harness,
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    assert _json_response(response) == {
        "error": "invalid_input",
        "detail": "Body 'status' must be 'active' or 'paused'",
    }


@pytest.mark.asyncio
async def test_handle_event_goal_status_delegates_update() -> None:
    runner = _make_runner()
    runner.update_status = AsyncMock(return_value=JSONResponse({"goal": {"status": "active"}}))  # type: ignore[method-assign]

    response = await runner.handle_event(
        "conv_1",
        "goal_status",
        {"status": "active"},
        session_harness_name=lambda _conv_id: CODEX_NATIVE_CODING_AGENT.harness,
    )

    assert isinstance(response, JSONResponse)
    assert _json_response(response) == {"goal": {"status": "active"}}
    runner.update_status.assert_awaited_once_with("conv_1", status="active")
