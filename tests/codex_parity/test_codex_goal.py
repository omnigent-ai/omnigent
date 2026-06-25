from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.entities import Conversation
from omnigent.errors import OmnigentError
from omnigent.inner.codex_executor import CodexExecutor
from omnigent.server import app as app_module
from omnigent.server.routes.sessions import create_sessions_router
from tests.codex_parity.helpers import (
    assert_completed as _assert_completed,
)
from tests.codex_parity.helpers import (
    ev_assistant_message,
    ev_completed,
    ev_response_created,
)
from tests.codex_parity.helpers import (
    executor as _executor,
)
from tests.codex_parity.helpers import (
    run_turn as _run_turn,
)


def _app_session_for_test(executor: CodexExecutor) -> Any:
    states = list(executor._session_states.values())
    assert len(states) == 1
    app_session = states[0].app_session
    assert app_session is not None
    assert app_session.thread_id is not None
    return app_session


class _CodexGoalConversationStore:
    def __init__(self) -> None:
        self._conversations = {
            "conv_codex": Conversation(
                id="conv_codex",
                created_at=1,
                updated_at=1,
                root_conversation_id="conv_codex",
                agent_id="ag_codex",
                labels={
                    "omnigent.ui": "terminal",
                    "omnigent.wrapper": "codex-native-ui",
                },
            ),
            "conv_codex_no_runner": Conversation(
                id="conv_codex_no_runner",
                created_at=1,
                updated_at=1,
                root_conversation_id="conv_codex_no_runner",
                agent_id="ag_codex",
                labels={
                    "omnigent.ui": "terminal",
                    "omnigent.wrapper": "codex-native-ui",
                },
            ),
        }

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self._conversations.get(conversation_id)


class _CodexGoalAgentStore:
    def get(self, agent_id: str) -> None:
        del agent_id
        return


class _CodexGoalRunnerClient:
    def __init__(
        self,
        *,
        response_status: str | None = None,
        response_body: dict[str, Any] | str | None = None,
        status_code: int = 200,
    ) -> None:
        self.response_status = response_status
        self.response_body = response_body
        self.status_code = status_code
        self.post_json_calls: list[tuple[str, Any]] = []

    async def post(
        self,
        url: str,
        *,
        json: Any = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        del timeout
        self.post_json_calls.append((url, json))
        if isinstance(self.response_body, str):
            return httpx.Response(
                status_code=self.status_code,
                text=self.response_body,
                request=httpx.Request("POST", url),
            )
        if self.response_body is not None:
            return httpx.Response(
                status_code=self.status_code,
                json=self.response_body,
                request=httpx.Request("POST", url),
            )
        requested_status = json.get("status") if isinstance(json, dict) else None
        status = self.response_status or (
            requested_status if isinstance(requested_status, str) else "active"
        )
        return httpx.Response(
            status_code=200,
            json={
                "goal": {
                    "thread_id": "thread_goal_test",
                    "objective": "Finish parity",
                    "status": status,
                    "token_budget": 40000,
                    "tokens_used": 0,
                    "time_used_seconds": 0,
                }
            },
            request=httpx.Request("POST", url),
        )


class _CodexGoalRoutedRunner:
    def __init__(self, client: _CodexGoalRunnerClient) -> None:
        self.runner_id = "runner_goal_test"
        self.client = client


class _CodexGoalRunnerRouter:
    def __init__(self, client: _CodexGoalRunnerClient | None) -> None:
        self.client = client

    def client_for_session_resources(self, session_id: str) -> _CodexGoalRoutedRunner:
        if self.client is None or session_id == "conv_codex_no_runner":
            raise LookupError(session_id)
        return _CodexGoalRoutedRunner(self.client)


def _codex_goal_api_app(runner_client: _CodexGoalRunnerClient | None) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(
        request: Request,
        exc: OmnigentError,
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_sessions_router(
            _CodexGoalConversationStore(),  # type: ignore[arg-type]
            _CodexGoalAgentStore(),  # type: ignore[arg-type]
            runner_router=_CodexGoalRunnerRouter(runner_client),  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    return app


async def _codex_goal_request(
    app_session: Any,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await asyncio.wait_for(
        app_session._request(
            method,
            {"threadId": app_session.thread_id, **(params or {})},
        ),
        timeout=10,
    )
    result = response.get("result")
    assert isinstance(result, dict)
    return result


@pytest.mark.asyncio
async def test_real_codex_goal_set_get_clear_round_trip(
    codex_responses_sidecar,
    resolved_codex_bin: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "source-codex-home"))
    (tmp_path / "source-codex-home").mkdir()
    sidecar = codex_responses_sidecar(
        [
            [
                ev_response_created("resp-goal-bootstrap"),
                ev_assistant_message("msg-goal-bootstrap", "thread ready"),
                ev_completed("resp-goal-bootstrap"),
            ]
        ]
    )
    executor = _executor(resolved_codex_bin, sidecar.base_url, tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()

    try:
        _assert_completed(await _run_turn(executor, "bootstrap a goal thread"))
        app_session = _app_session_for_test(executor)
        thread_id = app_session.thread_id
        objective = "Finish the migration"

        set_result = await _codex_goal_request(
            app_session,
            "thread/goal/set",
            {"objective": objective, "tokenBudget": 40000},
        )

        goal = set_result.get("goal")
        assert isinstance(goal, dict)
        assert goal["threadId"] == thread_id
        assert goal["objective"] == objective
        assert isinstance(goal["status"], str)
        assert goal["status"]
        assert goal["tokenBudget"] == 40000
        assert isinstance(goal["tokensUsed"], int)
        assert isinstance(goal["timeUsedSeconds"], int | float)

        pause_result = await _codex_goal_request(
            app_session,
            "thread/goal/set",
            {"status": "paused"},
        )
        assert pause_result.get("goal", {}).get("status") == "paused"
        assert pause_result.get("goal", {}).get("objective") == objective

        resume_result = await _codex_goal_request(
            app_session,
            "thread/goal/set",
            {"status": "active"},
        )
        assert resume_result.get("goal", {}).get("status") == "active"
        assert resume_result.get("goal", {}).get("objective") == objective

        get_result = await _codex_goal_request(app_session, "thread/goal/get")
        assert get_result.get("goal", {}).get("objective") == objective

        clear_result = await _codex_goal_request(app_session, "thread/goal/clear")
        assert clear_result.get("cleared") is True

        get_after_clear = await _codex_goal_request(app_session, "thread/goal/get")
        assert get_after_clear.get("goal") is None

        clear_again = await _codex_goal_request(app_session, "thread/goal/clear")
        assert clear_again.get("cleared") is False
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_omnigent_codex_goal_set_api_forwards_mode_configuration() -> None:
    runner_client = _CodexGoalRunnerClient()
    app = _codex_goal_api_app(runner_client)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/v1/sessions/conv_codex/codex_goal",
            json={
                "objective": "Finish parity",
                "token_budget": 40000,
                "status": "paused",
            },
        )

    assert response.status_code == 200
    assert response.json()["goal"]["status"] == "paused"
    assert runner_client.post_json_calls == [
        (
            "/v1/sessions/conv_codex/events",
            {
                "type": "goal_set",
                "objective": "Finish parity",
                "token_budget": 40000,
                "status": "paused",
            },
        )
    ]


@pytest.mark.asyncio
async def test_omnigent_codex_goal_get_without_live_runner_returns_empty_read_only() -> None:
    app = _codex_goal_api_app(None)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/sessions/conv_codex_no_runner/codex_goal")

    assert response.status_code == 200
    assert response.json() == {"goal": None}


@pytest.mark.asyncio
async def test_omnigent_codex_goal_put_without_live_runner_returns_503() -> None:
    app = _codex_goal_api_app(None)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/v1/sessions/conv_codex_no_runner/codex_goal",
            json={"objective": "Finish parity"},
        )

    assert response.status_code == 503
    assert response.json()["error"] == "codex_native_goal_failed"


@pytest.mark.asyncio
async def test_omnigent_codex_goal_set_api_rejects_boolean_token_budget() -> None:
    runner_client = _CodexGoalRunnerClient()
    app = _codex_goal_api_app(runner_client)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/v1/sessions/conv_codex/codex_goal",
            json={"objective": "Finish parity", "token_budget": True},
        )

    assert response.status_code == 422
    assert runner_client.post_json_calls == []


@pytest.mark.asyncio
async def test_omnigent_codex_goal_api_preserves_runner_4xx_error_payload() -> None:
    runner_client = _CodexGoalRunnerClient(
        status_code=400,
        response_body={"error": "invalid_input", "detail": "harness mismatch"},
    )
    app = _codex_goal_api_app(runner_client)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/v1/sessions/conv_codex/codex_goal",
            json={"objective": "Finish parity"},
        )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_input", "detail": "harness mismatch"}


@pytest.mark.asyncio
async def test_omnigent_codex_goal_api_maps_malformed_runner_body_to_502() -> None:
    runner_client = _CodexGoalRunnerClient(response_body="not-json")
    app = _codex_goal_api_app(runner_client)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/v1/sessions/conv_codex/codex_goal",
            json={"objective": "Finish parity"},
        )

    assert response.status_code == 502
    assert response.json()["error"] == "codex_native_goal_failed"


@pytest.mark.asyncio
async def test_omnigent_codex_goal_api_maps_partial_goal_to_502() -> None:
    runner_client = _CodexGoalRunnerClient(
        response_body={
            "goal": {
                "thread_id": "thread_goal_test",
                "objective": "Finish parity",
                "status": "active",
            }
        }
    )
    app = _codex_goal_api_app(runner_client)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/sessions/conv_codex/codex_goal")

    assert response.status_code == 502
    assert response.json()["error"] == "codex_native_goal_failed"


@pytest.mark.asyncio
async def test_omnigent_codex_goal_status_api_forwards_pause_resume() -> None:
    runner_client = _CodexGoalRunnerClient()
    app = _codex_goal_api_app(runner_client)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        pause = await client.patch(
            "/v1/sessions/conv_codex/codex_goal/status",
            json={"status": "paused"},
        )
        resume = await client.patch(
            "/v1/sessions/conv_codex/codex_goal/status",
            json={"status": "active"},
        )

    assert pause.status_code == 200
    assert pause.json()["goal"]["status"] == "paused"
    assert resume.status_code == 200
    assert resume.json()["goal"]["status"] == "active"
    assert runner_client.post_json_calls == [
        ("/v1/sessions/conv_codex/events", {"type": "goal_status", "status": "paused"}),
        ("/v1/sessions/conv_codex/events", {"type": "goal_status", "status": "active"}),
    ]


@pytest.mark.asyncio
async def test_omnigent_codex_goal_status_api_rejects_codex_owned_statuses() -> None:
    runner_client = _CodexGoalRunnerClient()
    app = _codex_goal_api_app(runner_client)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/v1/sessions/conv_codex/codex_goal/status",
            json={"status": "complete"},
        )

    assert response.status_code == 422
    assert runner_client.post_json_calls == []


@pytest.mark.asyncio
async def test_omnigent_codex_goal_set_api_rejects_codex_owned_statuses() -> None:
    runner_client = _CodexGoalRunnerClient()
    app = _codex_goal_api_app(runner_client)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/v1/sessions/conv_codex/codex_goal",
            json={
                "objective": "Finish parity",
                "status": "complete",
            },
        )

    assert response.status_code == 422
    assert runner_client.post_json_calls == []


@pytest.mark.asyncio
async def test_omnigent_codex_goal_api_preserves_codex_owned_response_statuses() -> None:
    runner_client = _CodexGoalRunnerClient(response_status="budgetLimited")
    app = _codex_goal_api_app(runner_client)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/v1/sessions/conv_codex/codex_goal",
            json={
                "objective": "Finish parity",
                "token_budget": 40000,
                "status": "active",
            },
        )

    assert response.status_code == 200
    assert response.json()["goal"]["status"] == "budgetLimited"
    assert runner_client.post_json_calls == [
        (
            "/v1/sessions/conv_codex/events",
            {
                "type": "goal_set",
                "objective": "Finish parity",
                "token_budget": 40000,
                "status": "active",
            },
        )
    ]


@pytest.mark.asyncio
async def test_real_codex_goal_set_preserves_null_token_budget(
    codex_responses_sidecar,
    resolved_codex_bin: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "source-codex-home"))
    (tmp_path / "source-codex-home").mkdir()
    sidecar = codex_responses_sidecar(
        [
            [
                ev_response_created("resp-goal-null-budget"),
                ev_assistant_message("msg-goal-null-budget", "thread ready"),
                ev_completed("resp-goal-null-budget"),
            ]
        ]
    )
    executor = _executor(resolved_codex_bin, sidecar.base_url, tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()

    try:
        _assert_completed(await _run_turn(executor, "bootstrap a goal thread"))
        app_session = _app_session_for_test(executor)

        set_result = await _codex_goal_request(
            app_session,
            "thread/goal/set",
            {"objective": "Finish the migration", "tokenBudget": None},
        )

        goal = set_result.get("goal")
        assert isinstance(goal, dict)
        assert goal["tokenBudget"] is None
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_real_codex_goal_set_preserves_budget_limited_same_objective(
    codex_responses_sidecar,
    resolved_codex_bin: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "source-codex-home"))
    (tmp_path / "source-codex-home").mkdir()
    sidecar = codex_responses_sidecar(
        [
            [
                ev_response_created("resp-goal-budget-limited"),
                ev_assistant_message("msg-goal-budget-limited", "thread ready"),
                ev_completed("resp-goal-budget-limited"),
            ]
        ]
    )
    executor = _executor(resolved_codex_bin, sidecar.base_url, tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()

    try:
        _assert_completed(await _run_turn(executor, "bootstrap a goal thread"))
        app_session = _app_session_for_test(executor)
        objective = "Keep polishing"

        limited_result = await _codex_goal_request(
            app_session,
            "thread/goal/set",
            {
                "objective": objective,
                "status": "budgetLimited",
                "tokenBudget": 10,
            },
        )
        limited_goal = limited_result.get("goal")
        assert isinstance(limited_goal, dict)
        assert limited_goal["status"] == "budgetLimited"

        replacement_result = await _codex_goal_request(
            app_session,
            "thread/goal/set",
            {"objective": objective},
        )
        replacement_goal = replacement_result.get("goal")
        assert isinstance(replacement_goal, dict)
        assert replacement_goal["objective"] == objective
        assert replacement_goal["status"] == "budgetLimited"
        assert replacement_goal["tokenBudget"] == 10
        assert replacement_goal["tokensUsed"] == 0
        assert replacement_goal["timeUsedSeconds"] == 0
    finally:
        await executor.close()


@pytest.mark.parametrize("wire_status", ["blocked", "usageLimited"])
@pytest.mark.asyncio
async def test_real_codex_goal_set_persists_resumable_stopped_statuses(
    wire_status: str,
    codex_responses_sidecar,
    resolved_codex_bin: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "source-codex-home"))
    (tmp_path / "source-codex-home").mkdir()
    sidecar = codex_responses_sidecar(
        [
            [
                ev_response_created(f"resp-goal-{wire_status}"),
                ev_assistant_message(f"msg-goal-{wire_status}", "thread ready"),
                ev_completed(f"resp-goal-{wire_status}"),
            ]
        ]
    )
    executor = _executor(resolved_codex_bin, sidecar.base_url, tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()

    try:
        _assert_completed(await _run_turn(executor, "bootstrap a goal thread"))
        app_session = _app_session_for_test(executor)

        set_result = await _codex_goal_request(
            app_session,
            "thread/goal/set",
            {
                "objective": "Keep polishing",
                "status": wire_status,
            },
        )
        goal = set_result.get("goal")
        assert isinstance(goal, dict)
        assert goal["status"] == wire_status

        get_result = await _codex_goal_request(app_session, "thread/goal/get")
        persisted_goal = get_result.get("goal")
        assert isinstance(persisted_goal, dict)
        assert persisted_goal["status"] == wire_status
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_web_ui_api_prefix_miss_returns_json_not_spa_shell(tmp_path: Path) -> None:
    """
    Keep the browser goal API path from failing as a JSON parse error.

    The committed server mounts the SPA at ``/`` after API routers. If a
    route is absent in a stacked build, the static fallback still receives
    ``/v1/...``; API-shaped misses must return JSON 404 instead of
    ``index.html`` so the ap-web Codex goal controls can surface a normal
    request failure.
    """
    web_ui_dist = tmp_path / "web-ui"
    web_ui_dist.mkdir()
    (web_ui_dist / "index.html").write_text("<!doctype html><div id='root'></div>")

    app = FastAPI()
    app.mount("/", app_module._SPAStaticFiles(directory=web_ui_dist, html=True))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        api_miss = await client.get("/v1/sessions/session_123/not_a_route")
        spa_fallback = await client.get("/c/session_123")

    assert api_miss.status_code == 404
    assert api_miss.headers["content-type"] == "application/json"
    assert api_miss.json() == {"error": {"code": "not_found", "message": "Not found"}}
    assert "cache-control" not in api_miss.headers
    assert spa_fallback.status_code == 200
    assert spa_fallback.headers["content-type"].startswith("text/html")
