"""A real Claude CLI busy with a hung MCP reconnect must not hold the session's next turn."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import threading
import uuid
from pathlib import Path

import pytest

from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
from omnigent.inner.executor import ExecutorError, TurnComplete
from tests._helpers.live_server import reserve_port
from tests._helpers.polling import poll_until, poll_until_async
from tests.e2e.conftest import configure_mock_llm

pytestmark = pytest.mark.skipif(shutil.which("claude") is None, reason="needs the claude CLI")

_MODEL = "claude-sonnet-4-20250514"
# The CLI waits this long on each reconnect, far past the turn bound below.
_CLI_MCP_TIMEOUT_MS = 300_000
_TURN_BOUND_SECONDS = 60


class _HangsAfterSignIn:
    """An HTTP MCP server that answers 401 until sign-in, then never answers."""

    def __init__(self) -> None:
        self.signed_in = False
        self.requests = 0
        self.requests_after_sign_in = 0
        self.port = 0
        self._release = threading.Event()
        self._cleanup = contextlib.ExitStack()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    async def _app(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        self.requests += 1
        status = 401
        if self.signed_in:
            self.requests_after_sign_in += 1
            while not self._release.is_set():
                await asyncio.sleep(0.05)
            status = 503
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    def __enter__(self) -> _HangsAfterSignIn:
        import uvicorn

        with contextlib.ExitStack() as stack:
            sock = stack.enter_context(reserve_port())
            self.port = sock.getsockname()[1]
            config = uvicorn.Config(
                self._app, log_level="warning", lifespan="off", interface="asgi3"
            )
            server = uvicorn.Server(config)
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            stack.callback(self._stop, server, thread)
            poll_until(
                lambda: server.started or not thread.is_alive(), timeout_s=15, interval_s=0.05
            )
            assert server.started, "MCP server did not start"
            self._cleanup = stack.pop_all()
        return self

    def __exit__(self, *exc: object) -> None:
        self._cleanup.close()

    def _stop(self, server, thread: threading.Thread) -> None:
        self._release.set()
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "MCP server did not stop"


def _report_auth(path: Path, names: list[str], status: str) -> None:
    entries = [{"harness": "claude", "name": name, "status": status} for name in names]
    staged = path.with_suffix(".tmp")
    staged.write_text(json.dumps({"servers": entries}))
    staged.replace(path)


@pytest.mark.timeout(300)
async def test_turn_after_hung_mcp_reconnect_is_not_held_by_the_cli(
    isolated_mock_llm_server_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = uuid.uuid4().hex[:6]
    tokens = [f"TURN{index}-{uid}" for index in range(3)]
    for token in tokens:
        # Extra entries absorb the CLI's side requests that repeat the prompt.
        configure_mock_llm(
            isolated_mock_llm_server_url, [{"text": f"done {token}"}] * 4, match=token
        )
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    status_file = home / "mcp-auth-status.json"
    names = ["alpha", "beta"]
    monkeypatch.setenv("HOME", str(home))
    # The CLI would read its config from an inherited directory instead of the seeded HOME.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("OMNIGENT_MCP_AUTH_STATUS_FILE", str(status_file))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", isolated_mock_llm_server_url)
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    monkeypatch.setenv("MCP_TIMEOUT", str(_CLI_MCP_TIMEOUT_MS))
    # The CLI sandbox can't see an editable checkout outside the workspace; it isn't under test.
    monkeypatch.setenv("OMNIGENT_CLAUDE_SDK_NO_SANDBOX", "1")
    monkeypatch.setattr("omnigent.inner.claude_sdk_executor._MCP_RECONNECT_TIMEOUT_SECONDS", 2.0)

    with _HangsAfterSignIn() as alpha, _HangsAfterSignIn() as beta:
        gates = [alpha, beta]
        servers = {
            name: {"type": "http", "url": gate.url}
            for name, gate in zip(names, gates, strict=True)
        }
        (home / ".claude.json").write_text(json.dumps({"mcpServers": servers}))
        _report_auth(status_file, names, "sign_in_required")
        executor = ClaudeSDKExecutor(
            cwd=str(workspace),
            model=_MODEL,
            permission_mode="bypassPermissions",
            api_key_helper="printf %s mock-key",
        )
        messages: list[dict[str, object]] = []

        async def turn(token: str) -> None:
            messages.append({"role": "user", "content": f"Reply. {token}", "session_id": uid})
            try:
                async with asyncio.timeout(_TURN_BOUND_SECONDS):
                    events = [event async for event in executor.run_turn(messages, [], "")]
            except TimeoutError:
                pytest.fail(f"turn {token} was still waiting after {_TURN_BOUND_SECONDS}s")
            assert not [event for event in events if isinstance(event, ExecutorError)]
            completed = [event for event in events if isinstance(event, TurnComplete)]
            assert completed and f"done {token}" in (completed[-1].response or "")
            messages.append({"role": "assistant", "content": completed[-1].response})

        try:
            await turn(tokens[0])
            # The CLI tried each server at startup and found it needed sign-in.
            await poll_until_async(lambda: all(gate.requests for gate in gates), timeout_s=30)

            for gate in gates:
                gate.signed_in = True
            _report_auth(status_file, names, "connected")
            await turn(tokens[1])
            assert alpha.requests_after_sign_in, "the reconnect never reached the server"

            await turn(tokens[2])
        finally:
            await executor.close()
