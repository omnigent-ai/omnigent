"""An LLM connection failure must surface as a classified ``connection_error``.

Journey (reconstructed from the ticket's log chain): a user chats with an
openai-agents harness agent while the model endpoint is unreachable (a network
blip, a down gateway). The OpenAI SDK raises ``openai.APIConnectionError``
(``str(exc) == "Connection error."``), the executor fails the turn, and the
failure lands in the user's chat as an error pill.

Today that failure reaches the user as the generic
``{'code': 'runner_error', 'message': 'inner executor error: OpenAI Agents SDK
error: Connection error.'}`` blob. The semantic ``connection_error``
classification the adapter defines for exactly this exception
(``_classify_openai_exception`` in
``omnigent/runtime/harnesses/_executor_adapter.py``) never fires: the executor
stringifies the typed exception into ``ExecutorError``, and the runner's
``_normalize_turn_error`` reads ``type`` where the harness's ``ErrorDetail``
carries ``code``. The UI then mis-attributes an upstream transport failure as
"Something went wrong setting up the turn on the host." and KPI attribution
books it as an Omnigent runner defect instead of an upstream connection error.

This test drives the real journey — real server, real runner, real
openai-agents harness, real SPA in Chromium — with the harness's model
endpoint pointed at a loopback port with nothing listening, so the connect
genuinely fails (the same ``httpx.ConnectError`` → ``openai.APIConnectionError``
chain the ticket's logs show). It asserts the failed turn the user sees is
classified as ``connection_error``: on the buggy build the persisted code is
the generic ``runner_error``, so the final assertion fails; it passes once the
classification survives to the persisted error item.

Run::

    pytest tests/e2e_ui/chat/test_llm_connection_error_classification.py -v
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Seconds for the dedicated runner to tunnel into the shared server.
_RUNNER_ONLINE_TIMEOUT_S = 30.0

# Ceiling for the failing turn to settle: first-turn harness boot plus the
# openai client's built-in connect retries (a refused loopback connect fails
# instantly, so the retries add only their sub-second backoff).
_TURN_FAIL_TIMEOUT_S = 90.0

# Mirrors ``hello_world`` (tests/e2e_ui/conftest.py): the openai-agents
# harness with a plain (non-``databricks-``) model name resolves no provider
# auth and falls back to ``OPENAI_BASE_URL`` — here the dead endpoint.
_AGENT_YAML = """\
name: {name}
prompt: You are a friendly assistant. Say hello and answer questions.

executor:
  model: {model}
  harness: openai-agents

os_env:
  type: caller_process
  cwd: {cwd}
  sandbox:
    type: none
"""


def _agent_bundle(name: str, model: str, cwd: str) -> bytes:
    """Gzip-tar the inline agent YAML for multipart upload."""
    yaml_text = _AGENT_YAML.format(name=name, model=model, cwd=cwd)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo(name=f"{name}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _reserve_dead_port() -> int:
    """Return a loopback port with nothing listening on it.

    Binding to port 0 lets the OS pick a free port; closing the socket leaves
    the port unbound, so a connect to it is refused — the transport-level
    failure the openai client maps to ``APIConnectionError``.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def dead_endpoint_runner(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """Spawn a dedicated runner whose ``OPENAI_BASE_URL`` refuses connections.

    Same dedicated-runner pattern as
    ``tests/e2e_ui/chat/test_wedged_llm_call_recovers_run.py``: the shared
    ``live_server`` runner points the openai-agents harness at the healthy
    mock LLM, so simulating an unreachable model endpoint needs a sibling
    runner with its own environment. Yields the runner id to bind sessions to.
    """
    from omnigent.runner.identity import token_bound_runner_id

    runner_tmp = tmp_path_factory.mktemp("dead_endpoint_runner")
    log_path = runner_tmp / "runner.log"

    dead_port = _reserve_dead_port()
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": live_server,
        # The model endpoint under test: nothing listens on this port, so the
        # SDK's connect fails exactly like a network blip / down gateway.
        "OPENAI_BASE_URL": f"http://127.0.0.1:{dead_port}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
        # Keep the runner's and harness's process logs inside the test tmp so a
        # failure's diagnostics (the ticket's log-signature lines) are local.
        "OMNIGENT_DATA_DIR": str(runner_tmp / "data"),
        # A fresh, empty config home: an ambient OMNIGENT_CONFIG_HOME (e.g. a
        # CI harness config with env-ref'd gateway credentials) would make
        # turn setup fail before the journey starts.
        "OMNIGENT_CONFIG_HOME": str(runner_tmp / "config-home"),
    }
    (runner_tmp / "config-home").mkdir(exist_ok=True)
    log_handle = open(log_path, "w")  # noqa: SIM115 — fd dup'd into child; closed below
    proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    log_handle.close()  # child holds its own dup of the fd

    deadline = time.monotonic() + _RUNNER_ONLINE_TIMEOUT_S
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"dead-endpoint runner exited early (code {proc.returncode}); "
                f"log:\n{log_path.read_text()[-3000:]}"
            )
        try:
            resp = httpx.get(f"{live_server}/v1/runners/{runner_id}/status", timeout=2)
            if resp.status_code == 200 and resp.json().get("online") is True:
                ready = True
                break
        except httpx.HTTPError:
            time.sleep(0.25)
            continue
        time.sleep(0.25)

    if not ready:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        raise RuntimeError(
            f"dead-endpoint runner did not register within "
            f"{_RUNNER_ONLINE_TIMEOUT_S:.0f}s; log:\n{log_path.read_text()[-3000:]}"
        )

    try:
        yield runner_id
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture
def dead_endpoint_session(
    live_server: str,
    dead_endpoint_runner: str,
) -> Iterator[tuple[str, str]]:
    """Yield ``(base_url, session_id)`` for a session on the dead-endpoint runner."""
    ws = Path(tempfile.mkdtemp(prefix="omnigent-e2e-conn-err-"))
    name = f"conn_err_probe_{uuid.uuid4().hex[:8]}"
    model = f"conn-err-probe-{uuid.uuid4().hex[:8]}"

    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": "{}"},
        files={
            "bundle": (
                "agent.tar.gz",
                _agent_bundle(name, model, str(ws)),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    try:
        httpx.patch(
            f"{live_server}/v1/sessions/{session_id}",
            json={"runner_id": dead_endpoint_runner},
            timeout=10.0,
        ).raise_for_status()

        # Wait for the runner-backed environment so the session is fully
        # bound before the browser journey starts.
        deadline = time.monotonic() + 30.0
        while True:
            env_resp = httpx.get(
                f"{live_server}/v1/sessions/{session_id}/resources/environments/default",
                timeout=10.0,
            )
            if env_resp.status_code == 200:
                break
            if time.monotonic() > deadline:
                env_resp.raise_for_status()
            time.sleep(0.5)

        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        shutil.rmtree(ws, ignore_errors=True)


def _wait_for_persisted_connection_failure(
    base_url: str,
    session_id: str,
    *,
    timeout_s: float = _TURN_FAIL_TIMEOUT_S,
) -> tuple[str, str]:
    """Poll the transcript until the connection-failure ``error`` item lands.

    :returns: ``(code, message)`` of the persisted error item whose message
        references the connection failure.
    :raises AssertionError: When no such item persists within ``timeout_s``.
    """
    deadline = time.monotonic() + timeout_s
    seen: list[tuple[str, str]] = []
    while time.monotonic() < deadline:
        resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/items?limit=200", timeout=10.0)
        resp.raise_for_status()
        seen = []
        for item in resp.json()["data"]:
            if item.get("type") != "error":
                continue
            data = item.get("data") or {}
            code = str(item.get("code") or data.get("code") or "")
            message = str(item.get("message") or data.get("message") or "")
            seen.append((code, message))
        for code, message in seen:
            if "connection" in message.lower():
                return code, message
        time.sleep(1.0)
    raise AssertionError(
        f"no persisted connection-failure error item within {timeout_s:.0f}s; "
        f"error items seen: {seen!r}"
    )


@pytest.mark.timeout(240)
def test_llm_connection_error_fails_turn_as_classified_connection_error(
    page: Page,
    dead_endpoint_session: tuple[str, str],
) -> None:
    """A model connection failure must reach the user as ``connection_error``.

    Journey: open the session → send a message → the model endpoint connect
    fails → the turn fails and an error pill lands in the chat. The persisted
    failure must carry the semantic ``connection_error`` code (upstream
    transport, retry-allowlist-recognized), not the generic ``runner_error``
    blob the buggy build surfaces.
    """
    base_url, session_id = dead_endpoint_session

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Hello! Quick check-in — please reply.")
    page.get_by_role("button", name="Send", exact=True).click()

    # The turn fails in front of the user: an error pill lands in the chat.
    pill = page.get_by_test_id("error-pill").first
    expect(pill).to_be_visible(timeout=int(_TURN_FAIL_TIMEOUT_S * 1000))

    # Expand the pill so the raw failure is on screen: the message must be the
    # SDK's connection failure (keys this test to the SDK connection-failure
    # chain, not incidental turn errors).
    pill.locator('button[aria-expanded="false"]').first.click()
    expect(pill.get_by_test_id("error-message-content")).to_contain_text(
        re.compile("connection", re.IGNORECASE), timeout=10_000
    )

    code, message = _wait_for_persisted_connection_failure(base_url, session_id)
    assert "connection" in message.lower(), message

    # THE BUG: the adapter classifies openai.APIConnectionError as
    # the semantic ``connection_error`` code, but the classification is lost on
    # the way out and the user/KPI sees a generic ``runner_error`` — an
    # upstream transport blip mis-attributed as an Omnigent runner defect.
    assert code == "connection_error", (
        f"model connection failure surfaced with generic code {code!r} "
        f"(message: {message!r}); expected the semantic 'connection_error' "
        f"classification to be preserved end-to-end"
    )
