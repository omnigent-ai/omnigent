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
from omnigent.inner.executor import (
    ExecutorConfig,
    ExecutorError,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnComplete,
)
from omnigent.server import app as app_module
from omnigent.server.routes.sessions import create_sessions_router


def ev_response_created(response_id: str) -> dict[str, Any]:
    return {"type": "response.created", "response": {"id": response_id}}


def ev_assistant_message(item_id: str, text: str, *, phase: str | None = None) -> dict[str, Any]:
    event = {
        "type": "response.output_item.done",
        "item": {
            "type": "message",
            "role": "assistant",
            "id": item_id,
            "content": [{"type": "output_text", "text": text}],
        },
    }
    if phase is not None:
        event["item"]["phase"] = phase
    return event


def ev_message_item_added(item_id: str) -> dict[str, Any]:
    return {
        "type": "response.output_item.added",
        "item": {
            "type": "message",
            "role": "assistant",
            "id": item_id,
            "content": [],
        },
    }


def ev_output_text_delta(delta: str) -> dict[str, Any]:
    return {"type": "response.output_text.delta", "delta": delta}


def ev_completed(response_id: str) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "usage": {
                "input_tokens": 0,
                "input_tokens_details": None,
                "output_tokens": 0,
                "output_tokens_details": None,
                "total_tokens": 0,
            },
        },
    }


def ev_completed_with_usage(
    response_id: str,
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
    total_tokens: int,
) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "usage": {
                "input_tokens": input_tokens,
                "input_tokens_details": {"cached_tokens": cached_input_tokens},
                "output_tokens": output_tokens,
                "output_tokens_details": {"reasoning_tokens": reasoning_output_tokens},
                "total_tokens": total_tokens,
            },
        },
    }


def ev_function_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
        },
    }


def ev_failed(response_id: str, message: str) -> dict[str, Any]:
    return {
        "type": "response.failed",
        "response": {
            "id": response_id,
            "error": {"code": "server_error", "message": message},
        },
    }


def _executor(codex_bin: str, base_url: str, cwd: Path) -> CodexExecutor:
    return CodexExecutor(
        codex_path=codex_bin,
        cwd=str(cwd),
        gateway=True,
        gateway_host="http://127.0.0.1",
        base_url_override=base_url,
        gateway_auth_command="printf %s dummy",
        model="mock-model",
        enable_web_search=False,
        skills_filter="none",
    )


async def _run_turn(
    executor: CodexExecutor,
    prompt: str,
    tools: list[dict[str, Any]] | None = None,
    config: ExecutorConfig | None = None,
) -> list[Any]:
    events = []
    async for event in executor.run_turn(
        [{"role": "user", "content": prompt, "session_id": "session-1"}],
        tools or [],
        "You are a parity test assistant.",
        config=config,
    ):
        events.append(event)
    return events


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
            )
        }

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self._conversations.get(conversation_id)


class _CodexGoalAgentStore:
    def get(self, agent_id: str) -> None:
        del agent_id
        return


class _CodexGoalRunnerClient:
    def __init__(self, *, response_status: str | None = None) -> None:
        self.response_status = response_status
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
    def __init__(self, client: _CodexGoalRunnerClient) -> None:
        self.client = client

    def client_for_session_resources(self, session_id: str) -> _CodexGoalRoutedRunner:
        assert session_id == "conv_codex"
        return _CodexGoalRoutedRunner(self.client)


def _codex_goal_api_app(runner_client: _CodexGoalRunnerClient) -> FastAPI:
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
async def test_real_codex_smoke_uses_mock_responses(
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
                ev_response_created("resp-1"),
                ev_assistant_message("msg-1", "fixture hello"),
                ev_completed("resp-1"),
            ]
        ]
    )
    executor = _executor(resolved_codex_bin, sidecar.base_url, tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()

    events = _assert_completed(await _run_turn(executor, "hello?"))
    await executor.close()

    assert [event.text for event in events if isinstance(event, TextChunk)] == ["fixture hello"]
    requests = sidecar.requests(min_count=1)
    assert requests[0]["path"] == "/v1/responses"
    assert requests[0]["body"]["model"] == "mock-model"
    assert "hello?" in str(requests[0]["body"]["input"])


@pytest.mark.asyncio
async def test_real_codex_streaming_deltas(
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
                ev_response_created("resp-stream"),
                ev_message_item_added("msg-stream"),
                ev_output_text_delta("he"),
                ev_output_text_delta("llo"),
                ev_assistant_message("msg-stream", "hello"),
                ev_completed("resp-stream"),
            ]
        ]
    )
    executor = _executor(resolved_codex_bin, sidecar.base_url, tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()

    events = _assert_completed(await _run_turn(executor, "stream please"))
    await executor.close()

    assert [event.text for event in events if isinstance(event, TextChunk)] == ["he", "llo"]
    assert [event.response for event in events if isinstance(event, TurnComplete)] == ["hello"]


@pytest.mark.asyncio
async def test_real_codex_usage_and_model_override_cross_boundary(
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
                ev_response_created("resp-usage"),
                ev_assistant_message("msg-usage", "usage applied"),
                ev_completed_with_usage(
                    "resp-usage",
                    input_tokens=11,
                    cached_input_tokens=3,
                    output_tokens=7,
                    reasoning_output_tokens=5,
                    total_tokens=18,
                ),
            ]
        ]
    )
    executor = _executor(resolved_codex_bin, sidecar.base_url, tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()

    events = _assert_completed(
        await _run_turn(
            executor,
            "use override",
            config=ExecutorConfig(model="mock-model-override"),
        )
    )
    await executor.close()

    completion = _only_completion(events)
    assert completion.response == "usage applied"
    assert completion.usage == {
        "input_tokens": 8,
        "output_tokens": 7,
        "total_tokens": 18,
        "cache_read_input_tokens": 3,
    }
    assert sidecar.requests(min_count=1)[0]["body"]["model"] == "mock-model-override"


@pytest.mark.asyncio
async def test_real_codex_uses_last_unknown_phase_message(
    codex_responses_sidecar,
    resolved_codex_bin: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: unknown-phase assistant messages should use the latest
    # completed message as the final response, matching real Codex behavior.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "source-codex-home"))
    (tmp_path / "source-codex-home").mkdir()
    sidecar = codex_responses_sidecar(
        [
            [
                ev_response_created("resp-last"),
                ev_assistant_message("msg-last-1", "First message"),
                ev_assistant_message("msg-last-2", "Second message"),
                ev_completed("resp-last"),
            ]
        ]
    )
    executor = _executor(resolved_codex_bin, sidecar.base_url, tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()

    events = _assert_completed(await _run_turn(executor, "case: last unknown phase wins"))
    await executor.close()

    assert [event.text for event in events if isinstance(event, TextChunk)] == [
        "First message",
        "Second message",
    ]
    assert _only_completion(events).response == "Second message"


@pytest.mark.asyncio
async def test_real_codex_final_answer_phase_wins(
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
                ev_response_created("resp-phase"),
                ev_assistant_message("msg-commentary", "Commentary", phase="commentary"),
                ev_assistant_message("msg-final", "Final answer", phase="final_answer"),
                ev_completed("resp-phase"),
            ]
        ]
    )
    executor = _executor(resolved_codex_bin, sidecar.base_url, tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()

    events = _assert_completed(await _run_turn(executor, "choose final answer"))
    await executor.close()

    assert [event.text for event in events if isinstance(event, TextChunk)] == [
        "Commentary",
        "Final answer",
    ]
    assert _only_completion(events).response == "Final answer"


@pytest.mark.asyncio
async def test_real_codex_commentary_only_does_not_become_final_response(
    codex_responses_sidecar,
    resolved_codex_bin: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: commentary should stream to the caller but should not be
    # promoted into the completed turn response.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "source-codex-home"))
    (tmp_path / "source-codex-home").mkdir()
    sidecar = codex_responses_sidecar(
        [
            [
                ev_response_created("resp-commentary"),
                ev_assistant_message("msg-commentary", "Commentary", phase="commentary"),
                ev_completed("resp-commentary"),
            ]
        ]
    )
    executor = _executor(resolved_codex_bin, sidecar.base_url, tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()

    events = _assert_completed(await _run_turn(executor, "case: commentary only"))
    await executor.close()

    assert [event.text for event in events if isinstance(event, TextChunk)] == ["Commentary"]
    assert _only_completion(events).response == ""


@pytest.mark.asyncio
async def test_real_codex_ignores_retry_progress_until_terminal_failure(
    codex_responses_sidecar,
    resolved_codex_bin: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: real Codex emits retry-progress failures before the terminal
    # error. CodexExecutor must keep waiting until Codex has exhausted retries.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "source-codex-home"))
    (tmp_path / "source-codex-home").mkdir()
    sidecar = codex_responses_sidecar(
        [
            [
                ev_response_created("resp-failed"),
                ev_failed("resp-failed", "boom from mock model"),
            ]
            for _ in range(6)
        ]
    )
    executor = _executor(resolved_codex_bin, sidecar.base_url, tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()

    events = await _run_turn(executor, "trigger failure")
    await executor.close()

    assert [event for event in events if isinstance(event, TurnComplete)] == []
    errors = [event for event in events if isinstance(event, ExecutorError)]
    assert len(errors) == 1
    assert "boom from mock model" in errors[0].message
    assert errors[0].retryable is True
    assert len(sidecar.requests(min_count=6)) == 6


@pytest.mark.asyncio
async def test_real_codex_dynamic_tool_round_trip(
    codex_responses_sidecar,
    resolved_codex_bin: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "source-codex-home"))
    (tmp_path / "source-codex-home").mkdir()
    call_id = "call-1"
    sidecar = codex_responses_sidecar(
        [
            [
                ev_response_created("resp-tool-1"),
                ev_function_call(call_id, "calculate", '{"value": 41}'),
                ev_completed("resp-tool-1"),
            ],
            [
                ev_response_created("resp-tool-2"),
                ev_assistant_message("msg-tool", "42"),
                ev_completed("resp-tool-2"),
            ],
        ]
    )
    executor = _executor(resolved_codex_bin, sidecar.base_url, tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()

    def tool_executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert name == "calculate"
        assert args == {"value": 41}
        return {"result": "42"}

    # Parity harness wires the current executor hook directly.
    executor._tool_executor = tool_executor
    tools = [
        {
            "name": "calculate",
            "description": "Add one.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
        }
    ]
    events = _assert_completed(await _run_turn(executor, "use the tool", tools=tools))
    await executor.close()

    assert any(
        isinstance(event, ToolCallRequest) and event.name == "calculate" for event in events
    )
    assert any(
        isinstance(event, ToolCallComplete)
        and event.name == "calculate"
        and event.status == ToolCallStatus.SUCCESS
        for event in events
    )
    assert [event.response for event in events if isinstance(event, TurnComplete)] == ["42"]
    requests = sidecar.requests(min_count=2)
    assert len(requests) == 2
    assert call_id in str(requests[1]["body"]["input"])
    assert "42" in str(requests[1]["body"]["input"])


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


def _assert_completed(events: list[Any]) -> list[Any]:
    completions = [event for event in events if isinstance(event, TurnComplete)]
    assert len(completions) == 1, events
    return events


def _only_completion(events: list[Any]) -> TurnComplete:
    completions = [event for event in events if isinstance(event, TurnComplete)]
    assert len(completions) == 1, events
    return completions[0]
