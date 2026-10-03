"""UI journey: codex-native turns on gateway models that reject ``parallel_tool_calls``.

Codex includes a ``parallel_tool_calls`` field on every Responses request it
sends. Databricks-hosted (non-OpenAI) gateway models reject any request that
carries the field — gpt-oss/llama with a capability gate
(``INVALID_PARAMETER_VALUE``), qwen/gemma with a schema rejection
(``json: unknown field``) — so every turn of a codex-native session launched on
such a model fails, including on the discovery-resolved launch default. The
same request succeeds, tool calls included, the moment the field is omitted.

The gateway stand-in below reproduces that split faithfully: it rejects any
Responses request carrying ``parallel_tool_calls`` with the gateway's error
body and serves a normal Responses reply otherwise. The test passes only when
a real Codex turn round-trips against it — i.e. when the launch stops sending
the field for a non-OpenAI model.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _codex_cli_supports_mocked_app_server,
    _create_native_codex_session,
    _find_free_port,
    _write_mock_codex_provider_config,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _ensure_chat_view,
    _item_text,
    _ordered_message_items,
    _select_view_mode,
    _send,
)
from tests.server.integration.mock_llm_server import json_text_response, sse_text_response

_TERMINAL_VIEW = '[data-testid="terminal-view"]'
_TERMINAL_READY_TIMEOUT_MS = 120_000
_TURN_OUTCOME_TIMEOUT_S = 150.0
_REJECTION_SETTLE_S = 15.0

_REPLY_TOKEN = "E2E-GATEWAY-TURN-COMPLETED"
_PROMPT = f"Reply with exactly this token and nothing else: {_REPLY_TOKEN}"

# The two rejection shapes Databricks-hosted Responses models return for a
# request carrying ``parallel_tool_calls``, verbatim from the gateway.
_CAPABILITY_GATE_BODY = {
    "error_code": "INVALID_PARAMETER_VALUE",
    "message": (
        "INVALID_PARAMETER_VALUE: This model does not support the "
        "'parallel_tool_calls' parameter for the Open Responses API."
    ),
}
_SCHEMA_REJECT_BODY = {
    "error_code": "BAD_REQUEST",
    "message": 'Bad request: json: unknown field "parallel_tool_calls"\n',
}


class _GatewaySimulator:
    """Databricks-gateway stand-in for a non-OpenAI Responses model.

    Rejects any ``POST …/responses`` whose body carries a
    ``parallel_tool_calls`` field (any value) with the configured gateway
    error body, and serves a normal scripted Responses reply otherwise.
    """

    def __init__(self, rejection_body: dict[str, str]) -> None:
        self._rejection_body = rejection_body
        self._lock = threading.Lock()
        self._rejected: list[dict] = []
        self._served: list[dict] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._build_handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def rejected(self) -> list[dict]:
        with self._lock:
            return list(self._rejected)

    def served(self) -> list[dict]:
        with self._lock:
            return list(self._served)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        simulator = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if not self.path.rstrip("/").endswith("/responses"):
                    self._reply(404, b'{"error": "not found"}', "application/json")
                    return
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                if "parallel_tool_calls" in body:
                    with simulator._lock:
                        simulator._rejected.append(body)
                    payload = json.dumps(simulator._rejection_body).encode()
                    self._reply(400, payload, "application/json")
                    return
                with simulator._lock:
                    simulator._served.append(body)
                model = str(body.get("model", "mock-model"))
                if body.get("stream"):
                    sse = sse_text_response(_REPLY_TOKEN, model=model).encode()
                    self._reply(200, sse, "text/event-stream")
                    return
                payload = json.dumps(json_text_response(_REPLY_TOKEN, model=model)).encode()
                self._reply(200, payload, "application/json")

            def do_GET(self) -> None:
                if self.path.rstrip("/").endswith("/models"):
                    self._reply(200, b'{"object": "list", "data": []}', "application/json")
                    return
                self._reply(404, b'{"error": "not found"}', "application/json")

            def _reply(self, status: int, payload: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args: object) -> None:
                pass

        return Handler


@dataclass(frozen=True)
class _GatewayCodexSession:
    """Session handle for a native Codex session routed at the gateway stand-in."""

    base_url: str
    session_id: str
    gateway: _GatewaySimulator


_CASES = [
    # The provider's default model wins an unpinned launch — the report's
    # "broken out of the box" resolved-default case.
    pytest.param(
        ("system.ai.gpt-oss-20b", False, _CAPABILITY_GATE_BODY),
        id="resolved-default-capability-gate",
    ),
    # A model the user explicitly selected, hitting the schema-reject shape.
    pytest.param(
        ("system.ai.qwen35-122b-a10b", True, _SCHEMA_REJECT_BODY),
        id="pinned-model-schema-reject",
    ),
]


@pytest.fixture
def gateway_codex_session(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[_GatewayCodexSession]:
    """Spawn native Codex against the gateway stand-in on a dedicated server.

    Like ``mocked_native_codex_session``, this cannot reuse the session-scoped
    ``live_server``: the mock provider config and runner env must exist before
    the server and runner start, and the stand-in must be reachable from the
    runner's own network namespace.
    """
    if request.config.getoption("--ui-base-url"):
        pytest.skip("gateway-simulated native Codex e2e requires an isolated spawned server")
    codex_path = shutil.which("codex")
    if codex_path is None:
        pytest.skip("codex CLI is required for native Codex e2e")
    if not _codex_cli_supports_mocked_app_server(codex_path):
        pytest.skip("codex CLI >= 0.139.0 is required for mocked app-server e2e")

    model, pin_session_model, rejection_body = request.param
    server_tmp = tmp_path_factory.mktemp("e2e_ui_codex_gateway")
    gateway = _GatewaySimulator(rejection_body)

    config_home = server_tmp / "config-home"
    source_codex_home = server_tmp / "source-codex-home"
    home_dir = server_tmp / "home"
    state_dir = server_tmp / "codex-native-state"
    artifact_dir = server_tmp / "artifacts"
    for path in (source_codex_home, home_dir, state_dir, artifact_dir):
        path.mkdir(parents=True, exist_ok=True)
    _write_mock_codex_provider_config(config_home, gateway.base_url, model=model)

    port = _find_free_port()
    log_path = server_tmp / "server.log"
    runner_log_path = server_tmp / "runner.log"
    db_path = server_tmp / "test.db"
    agent_yaml_path = server_tmp / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML, encoding="utf-8")

    from omnigent.runner.identity import token_bound_runner_id

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    base_url = f"http://127.0.0.1:{port}"
    shared_env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "CODEX_HOME": str(source_codex_home),
        "HOME": str(home_dir),
        "OMNIGENT_CODEX_PATH": codex_path,
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }
    server_command = [
        sys.executable,
        "-c",
        "import omnigent.server.presence as _p; _p._LEAVE_GRACE_S = 1.0; "
        + "from omnigent.cli import main; main()",
        "server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--database-uri",
        f"sqlite:///{db_path}",
        "--artifact-location",
        str(artifact_dir),
        "--agent",
        str(agent_yaml_path),
    ]

    log_handle = open(log_path, "w")  # noqa: SIM115
    runner_log_handle = open(runner_log_path, "w")  # noqa: SIM115
    proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    session_id: str | None = None

    def _wait_until_ready(
        server_process: subprocess.Popen[bytes],
        runner_process: subprocess.Popen[bytes],
    ) -> None:
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        last_error = "not polled yet"
        while time.monotonic() < deadline:
            if server_process.poll() is not None:
                last_error = f"process exited early with code {server_process.returncode}"
                break
            if runner_process.poll() is not None:
                last_error = f"runner exited early with code {runner_process.returncode}"
                break
            try:
                resp = httpx.get(f"{base_url}/health", timeout=2)
                if resp.status_code == 200:
                    status_resp = httpx.get(
                        f"{base_url}/v1/runners/{runner_id}/status",
                        timeout=2,
                    )
                    if status_resp.status_code == 200 and status_resp.json()["online"] is True:
                        return
                    last_error = (
                        f"runner status HTTP {status_resp.status_code}: {status_resp.text[:200]}"
                    )
                else:
                    last_error = f"health HTTP {resp.status_code}: {resp.text[:200]}"
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(_HEALTH_POLL_INTERVAL_S)
        raise RuntimeError(
            f"gateway-simulated Codex e2e server did not become healthy within "
            f"{_HEALTH_TIMEOUT_S:.0f}s on {base_url} (last_error={last_error}).\n"
            f"Server log at {log_path}:\n"
            f"{log_path.read_text()[-3000:] if log_path.exists() else ''}\n"
            f"Runner log at {runner_log_path}:\n"
            f"{runner_log_path.read_text()[-3000:] if runner_log_path.exists() else ''}"
        )

    try:
        proc = subprocess.Popen(
            server_command,
            env=server_env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_log_handle,
            stderr=subprocess.STDOUT,
        )
        _wait_until_ready(proc, runner_proc)
        session_id = _create_native_codex_session(
            base_url, runner_id, model=model if pin_session_model else None
        )
        yield _GatewayCodexSession(base_url=base_url, session_id=session_id, gateway=gateway)
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        for process in (runner_proc, proc):
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        runner_log_handle.close()
        log_handle.close()
        gateway.close()


def _assistant_reply_landed(session: _GatewayCodexSession) -> bool:
    try:
        items = _ordered_message_items(session.base_url, session.session_id)
    except httpx.HTTPError:
        return False
    return any(
        item.get("role") == "assistant" and _REPLY_TOKEN in _item_text(item) for item in items
    )


def _wait_turn_outcome(session: _GatewayCodexSession) -> str:
    """Wait until the sent turn completes or the gateway rejects it."""
    deadline = time.monotonic() + _TURN_OUTCOME_TIMEOUT_S
    while time.monotonic() < deadline:
        if _assistant_reply_landed(session):
            return "completed"
        if session.gateway.rejected():
            # A 400 is terminal for the turn, but allow a retry without the
            # field to still land the reply before judging.
            settle = time.monotonic() + _REJECTION_SETTLE_S
            while time.monotonic() < settle:
                if _assistant_reply_landed(session):
                    return "completed"
                time.sleep(1.0)
            return "rejected"
        time.sleep(1.0)
    return "timeout"


def _tui_screen_text() -> str:
    """Best-effort Codex TUI text via its tmux sockets; the canvas xterm has no DOM text."""
    from omnigent.inner.terminal import _TERMINAL_DIR_PREFIX

    chunks: list[str] = []
    for sock in Path(tempfile.gettempdir()).glob(f"{_TERMINAL_DIR_PREFIX}*/tmux.sock"):
        try:
            panes = subprocess.run(
                ["tmux", "-S", str(sock), "list-panes", "-a", "-F", "#{pane_id}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            for pane in panes.stdout.split():
                capture = subprocess.run(
                    ["tmux", "-S", str(sock), "capture-pane", "-p", "-t", pane, "-S", "-50"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                chunks.append(capture.stdout)
        except (OSError, subprocess.SubprocessError):
            continue
    return "\n".join(chunks)


def _terminal_rejection_render() -> tuple[bool, str]:
    """Wait for the TUI to render the gateway rejection; return (shown, text).

    The TUI wraps long lines mid-word, so the marker is matched against the
    screen text with all whitespace stripped.
    """
    deadline = time.monotonic() + 30.0
    text = ""
    while time.monotonic() < deadline:
        text = _tui_screen_text()
        if "parallel_tool_calls" in "".join(text.split()):
            return True, text
        time.sleep(2.0)
    return False, text


def _fail_with_turn_outcome(session: _GatewayCodexSession, outcome: str) -> None:
    rejected = session.gateway.rejected()
    if not rejected:
        pytest.fail(
            f"codex-native turn never completed ({outcome}) but no Responses "
            f"request carried parallel_tool_calls "
            f"(served={len(session.gateway.served())}); the failure is not the "
            f"gateway rejection this test guards"
        )
    shown, tui_text = _terminal_rejection_render()
    tui_note = (
        f"the TUI renders the gateway rejection: {tui_text.strip()[-400:]!r}"
        if shown
        else f"TUI tail: {tui_text.strip()[-400:]!r}"
    )
    values = [body.get("parallel_tool_calls") for body in rejected]
    pytest.fail(
        f"codex-native turn never completed ({outcome}): Codex sent "
        f"parallel_tool_calls={values} on {len(rejected)} Responses request(s) "
        f"and the gateway rejected every one; {tui_note}. The same request is "
        f"served the scripted reply as soon as the field is omitted."
    )


@pytest.mark.nightly
@pytest.mark.timeout(600)
@pytest.mark.parametrize("gateway_codex_session", _CASES, indirect=True)
def test_codex_native_turn_completes_on_model_rejecting_parallel_tool_calls(
    page: Page,
    gateway_codex_session: _GatewayCodexSession,
) -> None:
    """A codex-native turn round-trips on a model whose gateway rejects the field."""
    session = gateway_codex_session
    page.goto(f"{session.base_url}/c/{session.session_id}")

    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(
        timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    _select_view_mode(page, "Terminal")
    terminal = page.locator(_TERMINAL_VIEW).last
    expect(terminal).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )

    _ensure_chat_view(page)
    _send(page, _PROMPT)
    # The reported failure renders in the Codex TUI; watch the turn there.
    _select_view_mode(page, "Terminal")

    outcome = _wait_turn_outcome(session)
    if outcome != "completed":
        _fail_with_turn_outcome(session, outcome)

    _ensure_chat_view(page)
    expect(page.locator(_ASSISTANT).filter(has_text=_REPLY_TOKEN).first).to_be_visible(
        timeout=30_000
    )
