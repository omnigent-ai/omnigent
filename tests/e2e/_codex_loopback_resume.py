"""Shared harness for real-Codex cold-resume end-to-end tests.

Rebuild a rollout through the production cold-resume seam, start the installed
``codex`` app-server on it in an isolated environment, submit a turn, and record
the Responses request Codex sends to a loopback provider (no credentials/network).
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexNativeAppServer,
    preload_codex_thread_for_resume,
)
from omnigent.harnesses.codex_native.main import _ensure_local_codex_resume_rollout

CANARY_REPLY = "cold resume context accepted"


async def write_cold_resume_rollout(
    items: list[dict[str, Any]],
    *,
    session_id: str,
    thread_id: str,
    codex_home: Path,
    workspace: Path,
    codex_path: str | None,
) -> Path:
    """Rebuild a rollout from ``items`` through the production server-history seam."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{session_id}/items", request.url
        return httpx.Response(200, json={"data": items, "has_more": False})

    async with httpx.AsyncClient(
        base_url="http://omnigent-server.invalid",
        transport=httpx.MockTransport(handler),
    ) as client:
        return await _ensure_local_codex_resume_rollout(
            client,
            session_id=session_id,
            external_session_id=thread_id,
            codex_home=codex_home,
            workspace=workspace,
            model_provider="loopback",
            codex_path=codex_path,
        )


def sse_text_response(text: str) -> bytes:
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


class LoopbackResponsesProvider(http.server.ThreadingHTTPServer):
    """Record the model input after Codex has normalized resumed history."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        super().__init__(("127.0.0.1", 0), _LoopbackHandler)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class _LoopbackHandler(http.server.BaseHTTPRequestHandler):
    server: LoopbackResponsesProvider

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
        self._send(200, "text/event-stream", sse_text_response(CANARY_REPLY))

    def log_message(self, *args: object) -> None:
        pass


def clean_codex_env(home: Path) -> dict[str, str]:
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


async def wait_for_turn_end(client: CodexAppServerClient) -> dict[str, Any]:
    """Wait for the real app-server's terminal event for the submitted turn."""
    async with asyncio.timeout(30):
        async for event in client.iter_events():
            if event.get("method") in {"turn/completed", "turn/failed"}:
                return event
    raise AssertionError("Codex event stream ended before the turn completed")


@dataclass(frozen=True)
class ColdResumeTurn:
    """What the real app-server did with one turn on a cold-resumed thread."""

    terminal_event: dict[str, Any]
    provider_requests: list[dict[str, Any]]
    stderr: list[str]

    def model_input(self) -> list[Any]:
        """Return the ``input`` list Codex sent on its last provider request."""
        assert self.provider_requests, (
            f"real Codex app-server never reached the loopback provider; stderr={self.stderr!r}"
        )
        actual_input = self.provider_requests[-1].get("input")
        assert isinstance(actual_input, list), self.provider_requests[-1]
        return actual_input


async def run_cold_resume_turn(
    *,
    codex_path: str,
    codex_home: Path,
    bridge_dir: Path,
    workspace: Path,
    child_home: Path,
    thread_id: str,
    session_id: str,
    prompt: str,
) -> ColdResumeTurn:
    """Preload ``thread_id`` from ``codex_home`` with real Codex and submit one turn."""
    provider = LoopbackResponsesProvider()
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    app_server = CodexNativeAppServer(
        codex_path=codex_path,
        socket_path=bridge_dir / "app-server.sock",
        codex_home=codex_home,
        env=clean_codex_env(child_home),
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
        session_id=session_id,
    )
    client: CodexAppServerClient | None = None
    try:
        await app_server.start()
        client = await preload_codex_thread_for_resume(
            str(app_server.socket_path),
            thread_id,
            retain_client=True,
            cwd=workspace,
        )
        assert client is not None
        await client.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
        )
        terminal_event = await wait_for_turn_end(client)
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()
        await app_server.close()
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)
    return ColdResumeTurn(
        terminal_event=terminal_event,
        provider_requests=provider.requests,
        stderr=list(app_server.recent_stderr or []),
    )
