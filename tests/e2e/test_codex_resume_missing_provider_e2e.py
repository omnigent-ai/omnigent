"""E2E: a Codex cold resume must recover when the saved provider is gone.

Reproduces the reported journey end to end with the real ``codex`` CLI:

1. A native Codex thread was created under a custom model provider
   (``legacy-provider``). The real ``codex exec`` writes a rollout on disk
   whose ``session_meta.model_provider`` names that provider.
2. The host configuration changes: ``legacy-provider`` is removed and a
   working ``current-provider`` is configured instead.
3. Cold resume: a fresh ``codex app-server`` loads the old thread from disk
   through the production seam
   :func:`omnigent.harnesses.codex_native.app_server.preload_codex_thread_for_resume`,
   which is exactly what the runner preload / native-CLI resume paths call.

Expected (the regression this guards): the thread resumes through the
app-server's current configured provider, preserving the original thread id
and conversation history.

Before the fix ``preload_codex_thread_for_resume`` sends ``thread/resume``
with no provider override, so the app-server tries to load the saved
``legacy-provider`` and startup fails with JSON-RPC ``-32600`` /
``failed to load configuration: Model provider `legacy-provider` not
found`` -- the failure this test reproduces and guards against.

Self-contained: the model provider is a loopback fake, and the codex CLI,
its rollout store, and the omnigent resume seam are all real. Requires no
server, no credentials, no network, and no inference request -- the
failure happens at thread-load time.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerResponseError,
    client_for_transport,
    preload_codex_thread_for_resume,
)
from tests.e2e._harness_probes import cli_unavailable_reason

_LEGACY_PROVIDER = "legacy-provider"
_CURRENT_PROVIDER = "current-provider"
_CANARY_REPLY = "synthetic canary reply"


def _sse_text_response(text: str) -> bytes:
    """Minimal Responses-API SSE stream: created -> message -> completed."""
    message_item = {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text}],
    }
    completed = {
        "id": "resp-1",
        "object": "response",
        "status": "completed",
        "output": [message_item],
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": None,
            "output_tokens": 1,
            "output_tokens_details": None,
            "total_tokens": 2,
        },
    }
    events: list[tuple[str, dict[str, Any]]] = [
        ("response.created", {"response": {"id": "resp-1"}}),
        ("response.output_item.done", {"item": message_item}),
        ("response.completed", {"response": completed}),
    ]
    return "".join(
        f"event: {evt}\ndata: {json.dumps({'type': evt, **payload})}\n\n"
        for evt, payload in events
    ).encode()


class _LoopbackResponsesProvider(http.server.ThreadingHTTPServer):
    """Loopback Responses provider that accepts any turn with a scripted reply."""

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
        # codex polls /models on startup; an empty list keeps it quiet.
        self._send(200, "application/json", json.dumps({"models": []}).encode())

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        self.server.requests.append(json.loads(self.rfile.read(length) or b"{}"))
        self._send(200, "text/event-stream", _sse_text_response(_CANARY_REPLY))

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        pass


def _provider_config(provider_id: str, base_url: str) -> str:
    return (
        'model = "mock-model"\n'
        f'model_provider = "{provider_id}"\n'
        f"[model_providers.{provider_id}]\n"
        f'name = "{provider_id}"\n'
        f'base_url = "{base_url}"\n'
        'wire_api = "responses"\n'
    )


def _codex_subprocess_env(codex_home: Path) -> dict[str, str]:
    """A clean env for the codex CLI: no leaked runner/harness/auth state."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OMNIGENT", "RUNNER_", "CODEX", "ANTHROPIC", "DATABRICKS", "OPENAI"))
    }
    env.update(
        {
            "CODEX_HOME": str(codex_home),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    return env


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _saved_thread_id(codex_home: Path) -> str:
    """Read the thread id and its persisted provider from the rollout on disk."""
    rollouts = sorted((codex_home / "sessions").rglob("rollout-*.jsonl"))
    assert rollouts, "codex exec produced no rollout to resume"
    meta = json.loads(rollouts[-1].read_text().splitlines()[0])["payload"]
    assert meta.get("model_provider") == _LEGACY_PROVIDER, (
        "precondition: the created thread must record the legacy provider it "
        f"was created under; got {meta.get('model_provider')!r}"
    )
    thread_id = meta["id"]
    assert isinstance(thread_id, str) and thread_id
    return thread_id


async def _await_app_server(ws_url: str, deadline_s: float) -> None:
    """Wait until the app-server accepts an initialize handshake."""
    deadline = time.monotonic() + deadline_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        probe = client_for_transport(ws_url, client_name="omnigent-test-probe")
        try:
            await probe.connect()
            await probe.close()
            return
        except Exception as exc:  # startup race, retried below
            last_exc = exc
            await asyncio.sleep(0.5)
    raise RuntimeError(f"codex app-server never came up: {last_exc}")


@pytest.mark.posix_only
@pytest.mark.timeout(300)
async def test_codex_cold_resume_recovers_when_saved_provider_removed(
    tmp_path: Path,
) -> None:
    """Cold-resuming a thread whose saved provider is gone must not fail load.

    The app-server has a working ``current-provider``; the saved thread names
    a removed ``legacy-provider``. The resume must recover through the current
    provider and keep the original thread id -- not reject the thread. Before
    the fix the resume propagates the app-server's config-load failure
    (``Model provider `legacy-provider` not found``), which the runner treats
    as an unreadable thread and drops the conversation's Codex history.
    """
    reason = cli_unavailable_reason("codex")
    if reason is not None:
        pytest.skip(f"requires a runnable 'codex' CLI; {reason}")

    codex_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    codex_home.mkdir()
    workspace.mkdir()

    provider = _LoopbackResponsesProvider()
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    try:
        # Step 1: create a native Codex thread under the custom provider.
        (codex_home / "config.toml").write_text(
            _provider_config(_LEGACY_PROVIDER, provider.base_url)
        )
        env = _codex_subprocess_env(codex_home)
        create = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "Reply with exactly: pong"],
            cwd=workspace,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert create.returncode == 0 and provider.requests, (
            "codex exec did not create a thread under the legacy provider: "
            f"exit={create.returncode} stdout={create.stdout[-1500:]!r} "
            f"stderr={create.stderr[-1500:]!r}"
        )
        thread_id = _saved_thread_id(codex_home)

        # Step 2: the host config drops the legacy provider and configures a
        # working current provider (same loopback endpoint, new id).
        (codex_home / "config.toml").write_text(
            _provider_config(_CURRENT_PROVIDER, provider.base_url)
        )

        # Step 3: cold resume on a fresh app-server through the production seam.
        ws_url = f"ws://127.0.0.1:{_free_port()}"
        app_server = subprocess.Popen(
            ["codex", "app-server", "--listen", ws_url],
            cwd=workspace,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            await _await_app_server(ws_url, deadline_s=60)
            try:
                await preload_codex_thread_for_resume(ws_url, thread_id)
            except CodexAppServerResponseError as exc:
                pytest.fail(
                    "cold resume rejected the thread instead of recovering "
                    "through the app-server's current provider: "
                    f"code={exc.code} message={exc.message!r}. This is the "
                    "reported bug -- the resume must not fail config load on "
                    "the removed saved provider."
                )
        finally:
            app_server.terminate()
            try:
                app_server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                app_server.kill()
                app_server.wait()

        # The recovered resume must keep the original thread usable: a turn on
        # the resumed thread completes through the current provider.
        provider.requests.clear()
        resume_turn = subprocess.run(
            [
                "codex",
                "exec",
                "resume",
                thread_id,
                "--skip-git-repo-check",
                "Reply with exactly: pong again",
            ],
            cwd=workspace,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert resume_turn.returncode == 0 and _CANARY_REPLY in (
            resume_turn.stdout + resume_turn.stderr
        ), (
            "the resumed thread could not complete a turn through the current "
            f"provider: exit={resume_turn.returncode} "
            f"stdout={resume_turn.stdout[-1500:]!r} stderr={resume_turn.stderr[-1500:]!r}"
        )
    finally:
        provider.shutdown()
        provider.server_close()
