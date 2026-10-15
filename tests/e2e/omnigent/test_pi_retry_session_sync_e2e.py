"""A pi RPC session stays in sync after a turn that pi auto-retried: on one real
``pi --mode rpc`` process the first prompt's provider call 503s and pi recovers,
and the second prompt must complete with only its own answer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.inner.executor import ExecutorConfig, ExecutorError, TextChunk, TurnComplete
from omnigent.inner.pi_harness import _build_pi_executor
from omnigent.models.model_catalog import ModelEntry
from omnigent.models.model_metadata import ModelCapability, ModelMetadata, ModelWireAPI
from omnigent.onboarding.providers import ModelInfo
from tests.e2e._harness_probes import skip_if_harness_cli_missing
from tests.e2e.conftest import configure_mock_llm, get_mock_requests, reset_mock_llm

_FIRST_ANSWER = "PI_RETRY_FIRST_ANSWER_RECOVERED"
_SECOND_ANSWER = "PI_RETRY_SECOND_ANSWER_IN_SYNC"
_FIRST_PROMPT = "Reply with the recovered answer."
_SECOND_PROMPT = "Reply with the second answer."

# errorMessage pi classifies as retryable, driving its agent-level auto-retry.
_RETRYABLE_ERROR = "503 Service Unavailable: upstream overloaded, please retry"
_SYSTEM_PROMPT = "You are a helpful assistant."
_SESSION_ID = "pi-retry-session-sync"

# Env the pi harness reads (see ``omnigent/inner/pi_harness.py``) that must
# not leak in from the developer's shell or an earlier test.
_STALE_HARNESS_ENV = (
    "HARNESS_PI_OS_ENV",
    "HARNESS_PI_DATABRICKS_PROFILE",
    "HARNESS_PI_GATEWAY_BASE_URL",
    "HARNESS_PI_GATEWAY_OPENAI_WIRE_API",
    "HARNESS_PI_BUNDLE_DIR",
    "HARNESS_PI_SKILLS_FILTER",
    "HARNESS_PI_PRESERVE_MODEL_IDS",
    "DATABRICKS_CONFIG_PROFILE",
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
)


async def _run_turn(executor, prompt: str, effort: str) -> list:
    messages = [{"role": "user", "content": prompt, "session_id": _SESSION_ID}]
    tools = [
        {
            "name": "remember",
            "description": "Remember a value",
            "parameters": {"type": "object", "properties": {}},
        }
    ]
    return [
        event
        async for event in executor.run_turn(
            messages, tools, _SYSTEM_PROMPT, ExecutorConfig(extra={"reasoning_effort": effort})
        )
    ]


def _streamed_text(events: list) -> str:
    return "".join(e.text for e in events if isinstance(e, TextChunk))


def _errors(events: list) -> list[str]:
    return [e.message for e in events if isinstance(e, ExecutorError)]


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    ("wire_api", "databricks", "reasoning"),
    [
        ("chat", False, True),
        ("responses", False, True),
        ("anthropic", False, True),
        ("chat", True, True),
        ("responses", True, True),
        ("chat", True, False),
    ],
)
async def test_pi_session_stays_in_sync_after_retry_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_llm_server_url: str,
    wire_api: str,
    databricks: bool,
    reasoning: bool,
) -> None:
    """The prompt after a retry-recovered turn gets only its own answer."""
    skip_if_harness_cli_missing("pi")

    mock_model = "mock-pi-retry-session-sync"
    if databricks:
        mock_model = f"system.ai.{mock_model}"
        entry = ModelEntry(
            id=mock_model,
            family="other",
            metadata=ModelMetadata(
                supported_capabilities=frozenset({ModelCapability.REASONING})
                if reasoning
                else frozenset(),
                wire_apis=frozenset(
                    {
                        ModelWireAPI.OPENAI_CHAT
                        if wire_api == "chat"
                        else ModelWireAPI.OPENAI_RESPONSES
                    }
                ),
            ),
        )
        monkeypatch.setattr(
            "omnigent.models.model_catalog.fetch_databricks_model_service_entries",
            lambda *_: [entry],
        )
        monkeypatch.setattr("omnigent.models.model_catalog.catalog_model_entries", lambda *_: [])
        monkeypatch.setattr(
            "omnigent.inner.pi_executor._databricks_workspace_url_for_gateway",
            lambda *_args, **_kwargs: mock_llm_server_url,
        )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("omnigent.inner.pi_settings.DEFAULT_PI_AGENT_DIR", home / ".pi/agent")
    monkeypatch.setattr(
        "omnigent.onboarding.providers.find_catalog_models",
        lambda _: [ModelInfo(name=mock_model, provider="openai", supports_reasoning=True)],
    )
    gateway_chat = databricks and wire_api == "chat"
    # The gateway deliberately repeats the tool name as its call ID across turns.
    tool = {
        "call_id": "remember" if gateway_chat else "remember-1",
        "name": "remember",
        "arguments": "{}",
        **({"thoughtSignature": "first-tool-signature"} if gateway_chat else {}),
    }
    responses = [
        {"error": _RETRYABLE_ERROR, "status_code": 503},
        {"tool_calls": [tool]},
        {"text": _FIRST_ANSWER, "stream": True},
        *(
            [{"tool_calls": [{**tool, "thoughtSignature": "second-tool-signature"}]}]
            if gateway_chat
            else []
        ),
        {"text": _SECOND_ANSWER, "stream": True},
    ]
    if gateway_chat:
        for response in responses:
            if "text" in response:
                response["chat_content"] = [
                    {"type": "reasoning", "text": "PRIVATE_GATEWAY_REASONING"},
                    {"type": "text", "text": response["text"]},
                ]
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(mock_llm_server_url, responses, key=mock_model)

    # The harness env the runtime emits for an OpenAI-compatible provider.
    monkeypatch.setenv("HARNESS_PI_MODEL", mock_model)
    monkeypatch.setenv("HARNESS_PI_GATEWAY", "true")
    monkeypatch.setenv(
        "HARNESS_PI_GATEWAY_BASE_URLS",
        json.dumps(
            {"claude": mock_llm_server_url}
            if wire_api == "anthropic"
            else {
                "openai": f"{mock_llm_server_url}/ai-gateway/codex/v1"
                if databricks
                else f"{mock_llm_server_url}/v1"
            }
        ),
    )
    monkeypatch.setenv("HARNESS_PI_GATEWAY_HOST", mock_llm_server_url)
    monkeypatch.setenv("HARNESS_PI_GATEWAY_AUTH_COMMAND", "printf %s mock-key")
    monkeypatch.setenv("HARNESS_PI_CWD", str(tmp_path))
    monkeypatch.setenv("HARNESS_PI_CONTEXT_FILES", "false")
    for name in _STALE_HARNESS_ENV:
        monkeypatch.delenv(name, raising=False)

    if wire_api != "anthropic":
        monkeypatch.setenv("HARNESS_PI_GATEWAY_OPENAI_WIRE_API", wire_api)
    executor = _build_pi_executor()
    calls = []

    async def remember(name, arguments, *, call_id):
        calls.append(name)
        return {"value": "REMEMBERED_PI_VALUE"}

    executor._tool_executor = remember
    try:
        first = await _run_turn(executor, _FIRST_PROMPT, "high")
        pid = executor._session_states[_SESSION_ID].rpc.process.pid
        second = await _run_turn(executor, _SECOND_PROMPT, "low")
        assert executor._session_states[_SESSION_ID].rpc.process.pid == pid
    finally:
        await executor.close()

    assert _errors(first) == [], (
        "the first turn was reported as a failure instead of completing with pi's "
        f"recovered answer: {_errors(first)}"
    )
    first_complete = [e for e in first if isinstance(e, TurnComplete)]
    assert [_FIRST_ANSWER in (t.response or "") for t in first_complete] == [True], (
        f"the first turn did not complete with {_FIRST_ANSWER!r}: {first}"
    )

    assert _errors(second) == [], (
        f"the second turn failed on the reused pi session: {_errors(second)}"
    )
    second_text = _streamed_text(second)
    assert _FIRST_ANSWER not in second_text, (
        "the second turn streamed the first turn's recovered answer: the pi session "
        f"is out of sync: {second}"
    )
    assert _SECOND_ANSWER in second_text, (
        f"the second turn did not stream its own answer: {second}"
    )
    second_complete = [e for e in second if isinstance(e, TurnComplete)]
    assert [_SECOND_ANSWER in (t.response or "") for t in second_complete] == [True], (
        f"the second turn did not complete with {_SECOND_ANSWER!r}: {second}"
    )

    requests = get_mock_requests(mock_llm_server_url)
    assert len(requests) == (5 if gateway_chat else 4), requests

    assert calls == (["remember"] * 2 if gateway_chat else ["remember"])
    if wire_api == "anthropic":
        budgets = [r.get("thinking", {}).get("budget_tokens") for r in requests]
        assert all(isinstance(budget, int) for budget in budgets), budgets
        assert budgets[0] == budgets[1] == budgets[2] > budgets[3] > 0
    else:
        efforts = [
            r.get("reasoning_effort")
            if wire_api == "chat"
            else r.get("reasoning", {}).get("effort")
            for r in requests
        ]
        assert efforts == (
            ["high"] * 3 + ["low"] * (len(requests) - 3) if reasoning else [None] * len(requests)
        )
    history = json.dumps(requests[-1].get("messages", requests[-1].get("input")))
    assert "REMEMBERED_PI_VALUE" in history and _FIRST_ANSWER in history

    if gateway_chat:
        wire_calls = [
            call for message in requests[-1]["messages"] for call in message.get("tool_calls", [])
        ]
        assert [call.get("thoughtSignature") for call in wire_calls] == [
            "first-tool-signature",
            "second-tool-signature",
        ]
        ids = [call["id"] for call in wire_calls]
        assert len(set(ids)) == 2 and "remember" not in ids
        result_ids = [
            message["tool_call_id"]
            for message in requests[-1]["messages"]
            if message["role"] == "tool"
        ]
        assert result_ids == ids
        assert "PRIVATE_GATEWAY_REASONING" not in history and "[object Object]" not in history
