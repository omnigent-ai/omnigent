"""A pi RPC session stays in sync after a turn that pi auto-retried: on one real
``pi --mode rpc`` process the first prompt's provider call 503s and pi recovers,
and later prompts retain history while picking up rotated credentials."""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from omnigent.inner.executor import (
    ExecutorConfig,
    ExecutorError,
    TextChunk,
    ToolCallComplete,
    TurnComplete,
)
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
_TOOL_RESULT = "PI_RETAINED_TOOL_RESULT"
_TOOLS = [
    {
        "name": "remember",
        "description": "Remember a value",
        "parameters": {"type": "object", "properties": {}},
    }
]

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


async def _run_turn(executor, prompt: str, effort: str = "low") -> list:
    messages = [{"role": "user", "content": prompt, "session_id": _SESSION_ID}]
    return [
        event
        async for event in executor.run_turn(
            messages, _TOOLS, _SYSTEM_PROMPT, ExecutorConfig(extra={"reasoning_effort": effort})
        )
    ]


def _streamed_text(events: list) -> str:
    return "".join(e.text for e in events if isinstance(e, TextChunk))


def _errors(events: list) -> list[str]:
    return [e.message for e in events if isinstance(e, ExecutorError)]


@pytest.fixture
def rotating_gateway(
    tmp_path: Path, mock_llm_server_url: str
) -> Iterator[tuple[str, Path, list[bool]]]:
    """Enforce the current token before forwarding to the existing mock provider."""
    token_file = tmp_path / "helper's token"
    token_file.write_text("first-token")
    accepted: list[bool] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            expected = token_file.read_text() if token_file.exists() else None
            credential = (
                self.headers.get("Authorization") or f"Bearer {self.headers.get('x-api-key')}"
            )
            valid = expected is not None and credential == f"Bearer {expected}"
            accepted.append(valid)
            if valid:
                response = httpx.post(
                    mock_llm_server_url + self.path,
                    content=body,
                    headers={"Content-Type": "application/json"},
                    timeout=30,
                )
                status, payload = response.status_code, response.content
                content_type = response.headers["Content-Type"]
            else:
                status, payload = 401, b'{"error":{"message":"stale credential"}}'
                content_type = "application/json"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", token_file, accepted
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    ("wire_api", "sandboxed", "databricks", "reasoning"),
    [
        ("chat", False, False, True),
        ("chat", True, False, True),
        ("responses", False, False, True),
        ("anthropic", False, False, True),
        ("chat", False, True, True),
        ("responses", False, True, True),
        ("chat", False, True, False),
    ],
    ids=[
        "chat-unsandboxed",
        "chat-sandboxed",
        "responses",
        "anthropic",
        "gateway-chat",
        "gateway-responses",
        "gateway-nonreasoning",
    ],
)
async def test_pi_session_stays_in_sync_after_retry_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_llm_server_url: str,
    rotating_gateway: tuple[str, Path, list[bool]],
    sandboxed: bool,
    wire_api: str,
    databricks: bool,
    reasoning: bool,
) -> None:
    """Effort changes, retries and credential rotation preserve process and history."""
    skip_if_harness_cli_missing("pi")
    if sandboxed and sys.platform != "darwin" and not shutil.which("bwrap"):
        pytest.skip("requires Seatbelt or bubblewrap")
    gateway_url, token_file, accepted = rotating_gateway
    home = tmp_path / "Pi's home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()

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
                unsupported_capabilities=frozenset({ModelCapability.REASONING})
                if not reasoning
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
            lambda *_args, **_kwargs: gateway_url,
        )
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
        {"text": _SECOND_ANSWER, "stream": True},
        {"text": "THIRD_ANSWER", "stream": True},
        {"tool_calls": [{**tool, "thoughtSignature": "second-tool-signature"}]}
        if gateway_chat
        else {"tool_calls": [{"call_id": "remember-2", "name": "remember", "arguments": "{}"}]},
        {"text": "AUTH_RECOVERED", "stream": True},
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
            {"claude": gateway_url}
            if wire_api == "anthropic"
            else {
                "openai": f"{gateway_url}/ai-gateway/codex/v1"
                if databricks
                else f"{gateway_url}/v1"
            }
        ),
    )
    monkeypatch.setenv("HARNESS_PI_GATEWAY_HOST", gateway_url)
    monkeypatch.setenv("HARNESS_PI_GATEWAY_AUTH_COMMAND", f"cat {shlex.quote(str(token_file))}")
    monkeypatch.setenv("HARNESS_PI_GATEWAY_AUTH_REFRESH_INTERVAL_MS", "100")
    monkeypatch.setenv("HARNESS_PI_CWD", str(workspace))
    monkeypatch.setenv("HARNESS_PI_CONTEXT_FILES", "false")
    for name in _STALE_HARNESS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("omnigent.inner.pi_settings.DEFAULT_PI_AGENT_DIR", home / ".pi" / "agent")
    monkeypatch.setenv("HARNESS_PI_SKILLS_FILTER", '"none"')
    if sandboxed:
        monkeypatch.setenv(
            "HARNESS_PI_OS_ENV",
            json.dumps(
                {
                    "type": "caller_process",
                    "sandbox": {
                        "type": "darwin_seatbelt" if sys.platform == "darwin" else "linux_bwrap",
                        # The editable Python install is outside the workspace.
                        "read_paths": [str(Path(__file__).resolve().parents[3] / "omnigent")],
                    },
                }
            ),
        )

    if wire_api != "anthropic":
        monkeypatch.setenv("HARNESS_PI_GATEWAY_OPENAI_WIRE_API", wire_api)
    executor = _build_pi_executor()
    calls: list[str] = []
    tool_pids: list[int] = []

    async def remember(name, args, *, call_id):
        calls.append(name)
        tool_pids.append(executor._session_states[_SESSION_ID].rpc.process.pid)
        if len(calls) == 1:
            token_file.write_text("during-tool-token")
        else:
            token_file.unlink()
        # Real elapsed time: the cached token expires while Pi awaits the tool.
        await asyncio.sleep(0.15)
        return {"value": _TOOL_RESULT}

    executor._tool_executor = remember
    try:
        assert executor._sandboxed is sandboxed
        first = await _run_turn(executor, _FIRST_PROMPT, "high")
        assert _errors(first) == [], (
            "the first turn was reported as a failure instead of completing with pi's "
            f"recovered answer: {_errors(first)}"
        )
        rpc = executor._session_states[_SESSION_ID].rpc
        assert rpc is not None and rpc.process is not None and rpc._tmp_dir is not None
        pid, agent_dir = rpc.process.pid, Path(rpc._tmp_dir)
        token_file.write_text("second-token")
        await asyncio.sleep(0.15)
        second = await _run_turn(executor, _SECOND_PROMPT)
        assert executor._session_states[_SESSION_ID].rpc.process.pid == pid
        token_file.write_text("third-token")
        await asyncio.sleep(0.15)
        third = await _run_turn(executor, "Reply with the third answer.")
        assert executor._session_states[_SESSION_ID].rpc.process.pid == pid
        failed = await _run_turn(executor, "Call remember again.")
        assert _errors(failed), "an expired token must not hide a failed credential helper"
        assert len(accepted) == 6, "a failed refresh must stop before the next provider request"
        token_file.write_text("recovered-token")
        recovered = await _run_turn(executor, "Continue with the recovered answer.")
        assert _errors(recovered) == []
        assert _streamed_text(recovered) == "AUTH_RECOVERED"
        assert executor._session_states[_SESSION_ID].rpc.process.pid == pid
        assert calls == ["remember", "remember"]
        assert tool_pids == [pid, pid]
    finally:
        await executor.close()

    assert not agent_dir.exists()
    assert _errors(third) == []
    assert _streamed_text(third) == "THIRD_ANSWER"
    assert any(isinstance(e, ToolCallComplete) for e in first)
    assert accepted == [True] * 7
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
    assert len(requests) == 7, (
        "expected the retry, tool continuation, later prompts and auth recovery; "
        f"got {len(requests)} provider call(s)"
    )
    if wire_api == "anthropic":
        budgets = [r.get("thinking", {}).get("budget_tokens") for r in requests]
        assert all(isinstance(budget, int) for budget in budgets), budgets
        assert budgets[0] == budgets[1] == budgets[2] > budgets[3] > 0
        assert budgets[3:] == [budgets[3]] * 4
    else:
        efforts = [
            r.get("reasoning_effort")
            if wire_api == "chat"
            else r.get("reasoning", {}).get("effort")
            for r in requests
        ]
        assert efforts == (["high"] * 3 + ["low"] * 4 if reasoning else [None] * 7)
    for request in requests[-2:]:
        history = json.dumps(request.get("messages", request.get("input")))
        assert _FIRST_PROMPT in history
        assert _FIRST_ANSWER in history
        assert _TOOL_RESULT in history

    history = json.dumps(requests[-1].get("messages", requests[-1].get("input")))
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
