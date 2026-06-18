from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

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


def _assert_completed(events: list[Any]) -> list[Any]:
    completions = [event for event in events if isinstance(event, TurnComplete)]
    assert len(completions) == 1, events
    return events


def _only_completion(events: list[Any]) -> TurnComplete:
    completions = [event for event in events if isinstance(event, TurnComplete)]
    assert len(completions) == 1, events
    return completions[0]
