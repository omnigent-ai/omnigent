"""Reader must recover when the agy cold-start misses the model-catalog deadline.

Drives the real ``agy`` CLI against a local mock Gemini backend, forces the
trigger (empty ``GetAvailableModels`` in a shortened cold-start window) so the
``agy_conv_*`` placeholder stays, sends a web turn, and asserts agy's reply is
mirrored; the buggy build's reader deadlocks and mirrors nothing. Run without
credentials::

    uv run --no-sync pytest --ui-skip-build -v \
        tests/e2e_ui/shells/test_antigravity_native_coldstart_deadlock.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent._wrapper_labels import (
    ANTIGRAVITY_NATIVE_WRAPPER_VALUE,
    UI_MODE_LABEL_KEY,
    UI_MODE_TERMINAL_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.harnesses.antigravity_native.bridge import (
    bridge_dir_for_bridge_id,
    is_placeholder_conversation_id,
    read_bridge_state,
    read_tmux_info,
)
from omnigent.harnesses.antigravity_native.main import _materialize_antigravity_agent_spec
from omnigent.runner.identity import token_bound_runner_id
from tests.e2e_ui.conftest import _find_free_port
from tests.e2e_ui.shells.test_terminal_direct_attach import _BLOCK_LOOPBACK_DIALS

pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.skipif(
        shutil.which("agy") is None or shutil.which("tmux") is None,
        reason="needs the real `agy` CLI and tmux on PATH",
    ),
    pytest.mark.timeout(600),
]

_ROOT = Path(__file__).resolve().parents[3]

# Force the trigger before the runner starts: GetAvailableModels returns an
# empty catalog, so the shortened cold-start deadline is always missed. The
# reader's only other caller of this RPC already tolerates an empty catalog.
_RUNNER_BOOTSTRAP = (
    "import runpy\n"
    "import omnigent.harnesses.antigravity_native.rpc as _rpc\n"
    "import omnigent.runner.native.orchestration as _orch\n"
    "_rpc.get_available_models = lambda *a, **k: {}\n"
    "_orch._AGY_COLD_START_PORT_TIMEOUT_S = 2.0\n"
    'runpy.run_module("omnigent.runner._entry", run_name="__main__")\n'
)


@pytest.fixture
def antigravity_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, built_spa: None
) -> Iterator[list[str]]:
    """A local mock Gemini backend; yields the tokens agy asked it to echo."""
    replies: list[str] = []

    class GeminiHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            streaming = ":streamGenerateContent" in self.path
            if not streaming and ":generateContent" not in self.path:
                self.send_error(404)
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            reply = "Ready."
            for content in reversed(request.get("contents", [])):
                if content.get("role") != "user":
                    continue
                text = "\n".join(part.get("text", "") for part in content.get("parts", []))
                matches = re.findall(r"Reply (agy-e2e-[0-9a-f]{8})\.", text)
                if matches:
                    reply = matches[-1]
                    replies.append(reply)
                    break
            response = {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": reply}]},
                        "finishReason": "STOP",
                        "index": 0,
                    }
                ],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
            }
            payload = json.dumps(response)
            body = (f"data: {payload}\n\n" if streaming else payload).encode()
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/event-stream" if streaming else "application/json"
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    home = tmp_path / "model-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "omnigent.harnesses.antigravity_native.bridge._BRIDGE_ROOT",
        home / ".omnigent" / "antigravity-native",
    )
    monkeypatch.setenv("GEMINI_API_KEY", "mock-gemini-key")
    with ThreadingHTTPServer(("127.0.0.1", 0), GeminiHandler) as server:
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", f"http://127.0.0.1:{server.server_port}/")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield replies
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _wait_until(check: Callable[[], bool], message: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, message
        time.sleep(0.2)


@dataclass
class AntigravitySession:
    base_url: str
    session_id: str
    runner: subprocess.Popen[bytes]
    directory: Path

    @property
    def bridge_dir(self) -> Path:
        return bridge_dir_for_bridge_id(self.session_id)


def _create_session(base_url: str, directory: Path) -> str:
    spec = _materialize_antigravity_agent_spec(directory)
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w:gz") as archive:
        archive.add(spec, arcname=spec.name)
    response = httpx.post(
        f"{base_url}/v1/sessions",
        data={
            "metadata": json.dumps(
                {
                    "labels": {
                        WRAPPER_LABEL_KEY: ANTIGRAVITY_NATIVE_WRAPPER_VALUE,
                        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
                    },
                    "workspace": str(directory),
                }
            )
        },
        files={"bundle": ("agent.tar.gz", bundle.getvalue(), "application/gzip")},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["session_id"]


@contextlib.contextmanager
def _antigravity_stack(directory: Path) -> Iterator[AntigravitySession]:
    tmux = shutil.which("tmux")
    assert tmux is not None, "install tmux before running this test"
    bootstrap = directory / "runner_bootstrap.py"
    bootstrap.write_text(_RUNNER_BOOTSTRAP, encoding="utf-8")
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    base_url = f"http://127.0.0.1:{_find_free_port()}"
    env = {
        **os.environ,
        "PYTHONPATH": str(_ROOT),
        "OMNIGENT_CONFIG_HOME": str(directory / "config"),
    }
    for key in list(env):
        if key.startswith(("OMNIGENT_RUNNER_", "HARNESS_ANTIGRAVITY_NATIVE_")):
            del env[key]
    session: AntigravitySession | None = None
    with contextlib.ExitStack() as stack:
        stack.callback(lambda: shutil.rmtree(session.bridge_dir, True) if session else None)
        server_log = stack.enter_context((directory / "server.log").open("w"))
        runner_log = stack.enter_context((directory / "runner.log").open("w"))

        def spawn(args: list[str], process_env: dict[str, str], log) -> subprocess.Popen[bytes]:
            process = subprocess.Popen(
                args, cwd=directory, env=process_env, stdout=log, stderr=subprocess.STDOUT
            )

            def stop() -> None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

            stack.callback(stop)
            return process

        server = spawn(
            [
                sys.executable,
                "-m",
                "omnigent",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                base_url.rsplit(":", 1)[1],
                "--database-uri",
                f"sqlite:///{directory / 'test.db'}",
                "--artifact-location",
                str(directory / "artifacts"),
            ],
            {**env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token},
            server_log,
        )
        runner = spawn(
            [sys.executable, str(bootstrap)],
            {
                **env,
                "OMNIGENT_RUNNER_ID": runner_id,
                "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                "RUNNER_SERVER_URL": base_url,
            },
            runner_log,
        )

        def ready() -> bool:
            assert server.poll() is None and runner.poll() is None, f"see logs in {directory}"
            try:
                response = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                return response.status_code == 200 and response.json()["online"] is True
            except httpx.HTTPError:
                return False

        try:
            _wait_until(ready, f"server/runner did not become ready; see {directory}", 120)
            session_id = _create_session(base_url, directory)
            session = AntigravitySession(base_url, session_id, runner, directory)
            response = httpx.patch(
                f"{base_url}/v1/sessions/{session_id}", json={"runner_id": runner_id}, timeout=30
            )
            response.raise_for_status()
            yield session
        finally:
            if session is not None:
                pane = read_tmux_info(session.bridge_dir)
                with contextlib.suppress(httpx.HTTPError):
                    httpx.delete(f"{base_url}/v1/sessions/{session.session_id}", timeout=10)
                if pane is not None:
                    subprocess.run(
                        [tmux, "-S", pane["socket_path"], "kill-server"],
                        capture_output=True,
                        timeout=10,
                    )


@pytest.fixture
def antigravity_session(
    request: pytest.FixtureRequest, tmp_path: Path, antigravity_model: list[str]
) -> Iterator[AntigravitySession]:
    assert not request.config.getoption("--ui-base-url"), "this test requires its own server"
    with _antigravity_stack(tmp_path) as session:
        yield session


def test_coldstart_placeholder_reader_still_mirrors_the_turn(
    page: Page, antigravity_session: AntigravitySession, antigravity_model: list[str]
) -> None:
    session = antigravity_session
    page.add_init_script(_BLOCK_LOOPBACK_DIALS)

    # Precondition: with no ready catalog the cold-start missed its deadline and
    # left the placeholder in bridge state (the reported starting state).
    _wait_until(
        lambda: read_tmux_info(session.bridge_dir) is not None, "agy pane never launched", 60
    )
    _wait_until(
        lambda: (
            (state := read_bridge_state(session.bridge_dir)) is not None
            and is_placeholder_conversation_id(state.conversation_id)
        ),
        "cold-start bound a real cascade instead of leaving the placeholder; "
        "the forced empty catalog did not take effect",
        timeout=30,
    )

    page.goto(f"{session.base_url}/c/{session.session_id}?view=terminal")
    terminal = page.get_by_test_id("main-terminal-view").get_by_test_id("terminal-view")
    expect(terminal).to_have_attribute("data-state", "connected", timeout=120_000)

    token = f"agy-e2e-{uuid.uuid4().hex[:8]}"
    page.get_by_test_id("view-mode-chat").click()
    composer = page.get_by_placeholder("Send a message…")
    composer.fill(f"Reply {token}. No tools.")
    page.get_by_role("button", name="Send", exact=True).click()

    # agy processes the typed turn in its own TUI and calls the model, so a reply
    # exists on agy's side. The deadlock is purely in the mirror, not in agy.
    _wait_until(
        lambda: token in antigravity_model,
        "agy never processed the turn (the mock model was never called)",
        timeout=120,
    )

    # The report's expected behavior: once the TUI mints the real cascade, the
    # reader adopts it and mirrors agy's reply into the web session. On the buggy
    # build this never happens (the placeholder-adoption deadlock).
    reply = page.locator('[data-testid="message-bubble"][data-role="assistant"]')
    expect(reply.filter(has_text=token)).to_have_count(1, timeout=90_000)
