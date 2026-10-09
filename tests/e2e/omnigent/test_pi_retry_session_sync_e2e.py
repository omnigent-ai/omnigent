"""A pi RPC session stays in sync after a turn that pi auto-retried: on one real
``pi --mode rpc`` process the first prompt's provider call 503s and pi recovers,
and the second prompt must complete with only its own answer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.inner.executor import ExecutorError, TextChunk, TurnComplete
from omnigent.inner.pi_harness import _build_pi_executor
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


async def _run_turn(executor, prompt: str) -> list:
    messages = [{"role": "user", "content": prompt, "session_id": _SESSION_ID}]
    return [event async for event in executor.run_turn(messages, [], _SYSTEM_PROMPT)]


def _streamed_text(events: list) -> str:
    return "".join(e.text for e in events if isinstance(e, TextChunk))


def _errors(events: list) -> list[str]:
    return [e.message for e in events if isinstance(e, ExecutorError)]


@pytest.mark.timeout(300)
async def test_pi_session_stays_in_sync_after_retry_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_llm_server_url: str,
) -> None:
    """The prompt after a retry-recovered turn gets only its own answer."""
    skip_if_harness_cli_missing("pi")

    mock_model = "mock-pi-retry-session-sync"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"error": _RETRYABLE_ERROR, "status_code": 503},
            {"text": _FIRST_ANSWER},
            {"text": _SECOND_ANSWER},
        ],
        key=mock_model,
    )

    # The harness env the runtime emits for an OpenAI-compatible provider.
    monkeypatch.setenv("HARNESS_PI_MODEL", mock_model)
    monkeypatch.setenv("HARNESS_PI_GATEWAY", "true")
    monkeypatch.setenv(
        "HARNESS_PI_GATEWAY_BASE_URLS", json.dumps({"openai": f"{mock_llm_server_url}/v1"})
    )
    monkeypatch.setenv("HARNESS_PI_GATEWAY_HOST", mock_llm_server_url)
    monkeypatch.setenv("HARNESS_PI_GATEWAY_AUTH_COMMAND", "printf %s mock-key")
    monkeypatch.setenv("HARNESS_PI_CWD", str(tmp_path))
    monkeypatch.setenv("HARNESS_PI_CONTEXT_FILES", "false")
    for name in _STALE_HARNESS_ENV:
        monkeypatch.delenv(name, raising=False)

    executor = _build_pi_executor()
    try:
        first = await _run_turn(executor, _FIRST_PROMPT)
        second = await _run_turn(executor, _SECOND_PROMPT)
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
    assert len(requests) == 3, (
        f"expected the 503 call, pi's retry and the second prompt; got {len(requests)} "
        "provider call(s)"
    )
