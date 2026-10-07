"""E2E: Codex cold resume preserves a tool call spanning compaction.

This reproduces the production failure with the real native Codex harness:

1. Omnigent history contains a function call, a compaction captured while the
   tool is still running, and the matching function output after compaction.
2. The production cold-resume path rebuilds the local Codex rollout from that
   server history.
3. The production native app-server wrapper starts the installed ``codex``
   binary, preloads the rebuilt thread, and submits a real turn.
4. A loopback Responses provider records the context Codex actually sends.

Before the fix, rollout synthesis discards the pre-compaction function call.
Codex then reports an orphan function output, removes it during context
normalization, and sends neither side of the completed tool interaction to the
provider. The test requires the real provider request to contain the matched
call/output pair in order, proving resumed context did not lose the result.

The provider is loopback-only, so the test needs no credentials or network.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexNativeAppServer,
    preload_codex_thread_for_resume,
)
from omnigent.harnesses.codex_native.main import _ensure_local_codex_resume_rollout
from tests.e2e._harness_probes import cli_unavailable_reason

_THREAD_ID = "019e96aa-0be2-7343-8d3b-6f914d60936b"
_SESSION_ID = "conv_cold_resume_inflight_tool"
_CALL_ID = "call_tool_spanning_compaction"
_TOOL_OUTPUT = "delayed-tool-output-survived-cold-resume"
_CANARY_REPLY = "cold resume context accepted"


def _stored_session_items() -> list[dict[str, Any]]:
    """Return the persisted ordering observed when compaction races a tool."""
    return [
        {
            "id": "msg_1",
            "response_id": "codex_turn_slow_tool",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Run the slow diagnostic."}],
        },
        {
            "id": "fc_1",
            "response_id": "codex_turn_slow_tool",
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": "printf delayed"}),
            "call_id": _CALL_ID,
        },
        {
            "id": "cmp_1",
            "response_id": "compact_1",
            "type": "compaction",
            "summary": "The user requested a slow diagnostic command.",
            "last_item_id": "fc_1",
            "token_count": 42,
            "window_id": "01a070e2-2665-7d62-9b74-973decf239b7",
            # The snapshot was taken while the tool was running, so it does
            # not yet contain either half of the in-flight interaction.
            "compacted_messages": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Run the slow diagnostic."}],
                }
            ],
        },
        {
            "id": "fco_1",
            "response_id": "codex_turn_slow_tool",
            "type": "function_call_output",
            "call_id": _CALL_ID,
            "output": _TOOL_OUTPUT,
        },
        {
            "id": "msg_2",
            "response_id": "codex_turn_slow_tool",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "The slow diagnostic completed."}],
        },
    ]


async def _write_cold_resume_rollout(codex_home: Path, workspace: Path) -> Path:
    """Rebuild a rollout through the production server-history seam."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{_SESSION_ID}/items", request.url
        return httpx.Response(200, json={"data": _stored_session_items(), "has_more": False})

    async with httpx.AsyncClient(
        base_url="http://omnigent-server.invalid",
        transport=httpx.MockTransport(handler),
    ) as client:
        return await _ensure_local_codex_resume_rollout(
            client,
            session_id=_SESSION_ID,
            external_session_id=_THREAD_ID,
            codex_home=codex_home,
            workspace=workspace,
            model_provider="loopback",
            codex_path=shutil.which("codex"),
        )


def _sse_text_response(text: str) -> bytes:
    """Return the minimal Responses SSE stream Codex needs to finish a turn."""
    message = {
        "id": "msg-loopback",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    completed = {
        "id": "resp-loopback",
        "object": "response",
        "status": "completed",
        "output": [message],
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": None,
            "output_tokens": 1,
            "output_tokens_details": None,
            "total_tokens": 2,
        },
    }
    events: list[tuple[str, dict[str, Any]]] = [
        ("response.created", {"response": {"id": "resp-loopback"}}),
        ("response.output_item.done", {"item": message}),
        ("response.completed", {"response": completed}),
    ]
    return "".join(
        f"event: {event}\ndata: {json.dumps({'type': event, **payload})}\n\n"
        for event, payload in events
    ).encode()


class _LoopbackResponsesProvider(http.server.ThreadingHTTPServer):
    """Record the model input after Codex has normalized resumed history."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        super().__init__(("127.0.0.1", 0), _LoopbackHandler)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class _LoopbackHandler(http.server.BaseHTTPRequestHandler):
    server: _LoopbackResponsesProvider

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send(200, "application/json", b'{"models":[]}')

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append(body)
        self._send(200, "text/event-stream", _sse_text_response(_CANARY_REPLY))

    def log_message(self, *args: object) -> None:
        pass


def _clean_codex_env(home: Path) -> dict[str, str]:
    """Return an isolated environment for the real native subprocess."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("OMNIGENT", "RUNNER_", "CODEX", "ANTHROPIC", "DATABRICKS", "OPENAI")
        )
    }
    env.update(
        {
            "HOME": str(home),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            # The production diagnostic filter includes warnings, which is
            # where Codex reports orphan function outputs.
            "RUST_LOG": "warn",
        }
    )
    return env


async def _wait_for_turn_end(client: CodexAppServerClient) -> dict[str, Any]:
    """Wait for the real app-server's terminal event for the submitted turn."""
    async with asyncio.timeout(30):
        async for event in client.iter_events():
            if event.get("method") in {"turn/completed", "turn/failed"}:
                return event
    raise AssertionError("Codex event stream ended before the turn completed")


@pytest.mark.posix_only
@pytest.mark.timeout(300)
async def test_native_cold_resume_keeps_tool_output_that_finishes_after_compaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model receives both halves of a tool interaction after cold resume."""
    reason = cli_unavailable_reason("codex")
    if reason is not None:
        pytest.skip(f"requires a runnable 'codex' CLI; {reason}")

    source_home = tmp_path / "source-codex-home"
    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    child_home = tmp_path / "home"
    for path in (source_home, codex_home, bridge_dir, workspace, child_home):
        path.mkdir()
    # Keep the real harness from importing the developer's Codex config,
    # login, hooks, or plugins into this isolated native process.
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    rollout = await _write_cold_resume_rollout(codex_home, workspace)
    assert rollout.exists()

    provider = _LoopbackResponsesProvider()
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    codex = shutil.which("codex")
    assert codex is not None
    app_server = CodexNativeAppServer(
        codex_path=codex,
        socket_path=bridge_dir / "app-server.sock",
        codex_home=codex_home,
        env=_clean_codex_env(child_home),
        config_overrides=[
            'model_provider="loopback"',
            (
                "model_providers.loopback="
                f'{{name="Loopback",base_url={json.dumps(provider.base_url)},'
                'wire_api="responses",requires_openai_auth=false,'
                "request_max_retries=0,stream_max_retries=0}"
            ),
            "analytics.enabled=false",
            "feedback.enabled=false",
            "features.plugins=false",
            'otel.metrics_exporter="none"',
            "check_for_update_on_startup=false",
        ],
        cwd=workspace,
        bridge_dir=bridge_dir,
        pinned_model="mock-model",
        reconcile_process_registry=False,
        session_id=_SESSION_ID,
    )
    client: CodexAppServerClient | None = None
    try:
        await app_server.start()
        client = await preload_codex_thread_for_resume(
            str(app_server.socket_path),
            _THREAD_ID,
            retain_client=True,
            cwd=workspace,
        )
        assert client is not None
        await client.request(
            "turn/start",
            {
                "threadId": _THREAD_ID,
                "input": [{"type": "text", "text": "Continue after the cold resume."}],
            },
        )
        terminal_event = await _wait_for_turn_end(client)
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()
        await app_server.close()
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)

    assert terminal_event.get("method") == "turn/completed", terminal_event
    assert provider.requests, (
        "real Codex app-server never reached the loopback provider; "
        f"stderr={app_server.recent_stderr!r}"
    )

    actual_input = provider.requests[-1].get("input")
    assert isinstance(actual_input, list), provider.requests[-1]
    call_indexes = [
        index
        for index, item in enumerate(actual_input)
        if isinstance(item, dict)
        and item.get("type") == "function_call"
        and item.get("call_id") == _CALL_ID
    ]
    output_indexes = [
        index
        for index, item in enumerate(actual_input)
        if isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and item.get("call_id") == _CALL_ID
        and _TOOL_OUTPUT in str(item.get("output"))
    ]
    assert len(call_indexes) == len(output_indexes) == 1 and call_indexes[0] < output_indexes[0], (
        "cold resume lost the matched tool interaction while Codex normalized "
        f"the rebuilt context: input={json.dumps(actual_input, indent=2)}; "
        f"stderr={app_server.recent_stderr!r}"
    )
    assert not any(
        "Orphan function call output" in line for line in (app_server.recent_stderr or [])
    ), app_server.recent_stderr
