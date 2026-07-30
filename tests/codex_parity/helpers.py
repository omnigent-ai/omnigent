"""Shared helpers for real-Codex/mock-Responses parity tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omnigent.inner.codex_executor import CodexExecutor
from omnigent.inner.executor import ExecutorConfig, TurnComplete


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


def executor(codex_bin: str, base_url: str, cwd: Path) -> CodexExecutor:
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


async def run_turn(
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


def assert_completed(events: list[Any]) -> list[Any]:
    completions = [event for event in events if isinstance(event, TurnComplete)]
    assert len(completions) == 1, events
    return events


def only_completion(events: list[Any]) -> TurnComplete:
    completions = [event for event in events if isinstance(event, TurnComplete)]
    assert len(completions) == 1, events
    return completions[0]
