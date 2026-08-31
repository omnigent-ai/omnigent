"""E2E: a stale ambient ``DATABRICKS_BEARER`` must not poison codex gateway auth.

Reproduces the poisoned-auth journey: the codex harness's generated
Databricks ``auth.command`` prefers an ambient ``DATABRICKS_BEARER`` env var
verbatim over the configured profile. A stale bearer exported in the user's
shell (these tokens have a ~1h TTL) then poisons every turn with
``403 Forbidden: Invalid Token``, and ``databricks auth login --profile <p>``
does not help because the profile mint is never consulted.

The journey is the user's own, driven end to end through a real codex CLI:

1. The user's profile is healthy -- ``databricks auth token --profile oss``
   mints a token the gateway accepts (the fake ``databricks`` CLI on PATH
   stands in for a workspace the user just re-logged into).
2. A stale ``DATABRICKS_BEARER`` sits exported in the environment.
3. The user runs a codex turn on the Databricks gateway.

Expected: the turn completes -- a stale ambient credential must not shadow a
working configured auth source. Before the fix codex sent the dead bearer,
the gateway rejected every attempt with 403, and the turn errored out.

Self-contained: mocks only the gateway HTTP endpoint and the ``databricks``
CLI binary; the codex CLI, the executor, and the generated auth command are
all real. Requires no server, no credentials, and no network.
"""

from __future__ import annotations

import http.server
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from omnigent.inner.codex_executor import CodexExecutor
from omnigent.inner.executor import ExecutorError, TurnComplete
from omnigent.spec.types import RetryPolicy
from tests.e2e._harness_probes import cli_unavailable_reason

STALE_BEARER = "stale-shell-bearer-token"
FRESH_TOKEN = "fresh-profile-token"


class _FakeGateway(http.server.ThreadingHTTPServer):
    """Loopback stand-in for the Databricks Unity AI Gateway.

    Accepts only ``Bearer <FRESH_TOKEN>`` (what the profile mints); any other
    Authorization header gets the workspace's real-world rejection shape:
    ``403 {"error_code": "403", "message": "Invalid Token"}``.
    """

    def __init__(self) -> None:
        self.auth_headers_seen: list[str] = []
        super().__init__(("127.0.0.1", 0), _FakeGatewayHandler)

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _FakeGatewayHandler(http.server.BaseHTTPRequestHandler):
    server: _FakeGateway

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
        self.rfile.read(length)
        auth = self.headers.get("Authorization", "")
        self.server.auth_headers_seen.append(auth)
        if auth != f"Bearer {FRESH_TOKEN}":
            body = json.dumps({"error_code": "403", "message": "Invalid Token"}).encode()
            self._send(403, "application/json", body)
            return
        self._send(200, "text/event-stream", _sse_text_response("gateway says hello"))

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        pass


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


def _install_healthy_profile(tmp_path: Path, host: str) -> tuple[Path, Path, Path]:
    """Set up the post-re-login state: a profile whose mint always works.

    Returns (bin dir with the fake ``databricks`` CLI, cfg path, a marker file
    the fake CLI touches so the test can tell whether the mint was consulted).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    mint_marker = tmp_path / "mint-consulted"
    fake_cli = bindir / "databricks"
    fake_cli.write_text(
        "#!/bin/sh\n"
        f": >> {mint_marker}\n"
        'case "$*" in *--help*) echo "usage"; exit 0;; esac\n'
        f'echo \'{{"access_token":"{FRESH_TOKEN}"}}\'\n'
    )
    fake_cli.chmod(0o755)
    cfg = tmp_path / "databrickscfg"
    cfg.write_text(f"[oss]\nhost = {host}\n")
    return bindir, cfg, mint_marker


@pytest.mark.posix_only
@pytest.mark.timeout(120)
async def test_stale_ambient_bearer_does_not_poison_codex_gateway_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A codex gateway turn must survive a stale ``DATABRICKS_BEARER`` in the shell.

    The profile is healthy and the gateway accepts its freshly minted token,
    so the only way this turn can fail is the ambient stale bearer shadowing
    the profile -- the regression this guards: an auth.command that
    short-circuits on ``DATABRICKS_BEARER`` never falls back to the working
    mint, and the turn dies with the gateway's 403 "Invalid Token".
    """
    reason = cli_unavailable_reason("codex")
    if reason is not None:
        pytest.skip(f"requires a runnable 'codex' CLI; {reason}")

    gateway = _FakeGateway()
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    try:
        bindir, cfg, mint_marker = _install_healthy_profile(tmp_path, gateway.host)

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()

        # The user's shell state: a bearer exported long ago, now expired.
        monkeypatch.setenv("DATABRICKS_BEARER", STALE_BEARER)
        # Isolate from any ambient Databricks state on the test machine.
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        monkeypatch.delenv("DATABRICKS_HOST", raising=False)
        monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        # Keep any corporate proxy out of the loopback gateway path.
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

        executor = CodexExecutor(
            cwd=str(workspace),
            gateway=True,
            databricks_profile="oss",
            model="mock-model",
            enable_web_search=False,
            skills_filter="none",
            retry_policy=RetryPolicy(max_retries=1, backoff_base_s=0.1, backoff_max_s=0.5),
        )

        events: list[Any] = []
        try:
            async for event in executor.run_turn(
                [{"role": "user", "content": "hello?", "session_id": "session-1"}],
                [],
                "You are a test assistant.",
            ):
                events.append(event)
        finally:
            await executor.close()

        errors = [e for e in events if isinstance(e, ExecutorError)]
        completions = [e for e in events if isinstance(e, TurnComplete)]
        assert not errors, (
            "codex gateway turn failed with a healthy profile because the stale "
            f"ambient DATABRICKS_BEARER shadowed it: {errors[0].message!r}; "
            f"profile mint consulted: {mint_marker.exists()}; "
            f"gateway saw auth headers: {sorted(set(gateway.auth_headers_seen))}"
        )
        assert len(completions) == 1, events
        assert completions[0].response == "gateway says hello"
        # The completed turn must have authenticated with a working token,
        # not the stale shell bearer.
        assert f"Bearer {FRESH_TOKEN}" in gateway.auth_headers_seen
    finally:
        gateway.shutdown()
        gateway.server_close()
