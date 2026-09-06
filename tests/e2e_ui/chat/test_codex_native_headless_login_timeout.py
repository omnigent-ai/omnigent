"""E2E: a headless codex-native chat turn must not die in the thread-start timeout.

Regression test for the headless login-fallback timeout: on a machine where the
native-Codex launch routing resolves to "Codex CLI login" with no usable
stored credential (no Omnigent provider configured, empty ``CODEX_HOME``),
the runner still launches the ``--remote`` Codex TUI headlessly. The TUI
parks on the ChatGPT sign-in / onboarding screen, never emits
``thread/started``, and the user's first chat message hangs for the whole
30s ``wait_for_thread_started`` timeout before the turn dies with::

    inner executor error: Codex native thread never started: Codex
    app-server never started a thread (startup timed out: TimeoutError). ...

That is the failure the reporter hit when Polly's codex sub-agent
(``harness: codex-native``) was dispatched headlessly: the runner logged
"Codex TUI never started a thread for conv_...; chat will not forward" and
the cross-vendor review never ran.

The launch router *already knows* this launch cannot start a thread — its
routing summary literally says "the TUI likely renders the ChatGPT sign-in
screen and never starts a thread" — so burning the startup timeout and
failing the turn with a TimeoutError is the bug. After a fix, the first
turn must either start a thread (e.g. by making the stored login usable
headlessly) or fail with a clear, non-timeout error. Either way the
``startup timed out`` marker disappears, which is what this test asserts.

The rig mirrors ``mocked_native_codex_session`` (own server + runner so the
credential-less ``CODEX_HOME`` / ``OMNIGENT_CONFIG_HOME`` cannot leak into
other tests), minus any provider config — the whole point is that nothing
routes.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _create_native_codex_session
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view, _send

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Boot budget for the spawned server + runner pair.
_HEALTH_TIMEOUT_S = 60.0
# The buggy path errors after the 30s thread-start timeout plus the
# executor's bridge-state poll; give the terminal signal ample room.
_TURN_OUTCOME_TIMEOUT_S = 150.0
_ERROR_PILL = '[data-testid="error-pill"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# The timeout-path marker in the executor error (the observed live failure).
_STARTUP_TIMEOUT_MARKER = "startup timed out"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars
# that must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False)

# Shared fixtures/helpers (e.g. the conftest session factory) use ambient
# ``httpx`` calls that DO trust env, so also exclude loopback from any forced
# proxy at import time.
for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))


def _no_proxy_env() -> dict[str, str]:
    """Ambient env with loopback excluded from any forced HTTP(S) proxy."""
    env = os.environ.copy()
    for var in ("NO_PROXY", "no_proxy"):
        existing = env.get(var, "")
        env[var] = ",".join(filter(None, [existing, "127.0.0.1,localhost"]))
    return env


@pytest.fixture
def headless_codex_session(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A codex-native wrapper session on a rig with no usable Codex credential.

    Spawns a dedicated server + runner whose ``CODEX_HOME`` is empty (no
    ``auth.json`` — Codex not logged in) and whose ``OMNIGENT_CONFIG_HOME``
    is empty (no provider routes the codex harness), then creates and binds
    the same codex-native wrapper session ``omnigent codex`` ships. This is
    the launch-routing state in which the reported thread-start timeout
    fires.

    :returns: ``(base_url, session_id)``.
    """
    if shutil.which("codex") is None:
        pytest.skip("codex CLI is required for the headless codex-native rig")

    work = tmp_path_factory.mktemp("codex_headless_login")
    config_home = work / "config-home"
    codex_home = work / "codex-home"
    home_dir = work / "home"
    state_dir = work / "codex-native-state"
    artifacts = work / "artifacts"
    for path in (config_home, codex_home, home_dir, state_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)

    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    shared_env = {
        **_no_proxy_env(),
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "CODEX_HOME": str(codex_home),
        "HOME": str(home_dir),
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

    server_log = work / "server.log"
    runner_log = work / "runner.log"
    server_handle = server_log.open("w")
    runner_handle = runner_log.open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    try:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{work}/test.db",
                "--artifact-location",
                str(artifacts),
            ],
            env=server_env,
            stdout=server_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            if server_proc.poll() is not None or runner_proc.poll() is not None:
                break
            try:
                if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                    status = _client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online"):
                        online = True
                        break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        if not online:
            raise RuntimeError(
                "headless codex rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )

        session_id = _create_native_codex_session(base_url, runner_id)
        yield (base_url, session_id)
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                _client.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        for proc in (runner_proc, server_proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in (runner_proc, server_proc):
            if proc is not None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        server_handle.close()
        runner_handle.close()


@pytest.mark.timeout(400)
def test_headless_codex_native_first_turn_does_not_hit_thread_start_timeout(
    page: Page,
    headless_codex_session: tuple[str, str],
) -> None:
    """The first chat turn must not die in the 30s thread-start timeout.

    Journey (the reported one, minus Polly's orchestration wrapper): open a
    codex-native session that was launched headlessly with no usable Codex
    credential, send the first chat message, and watch the outcome. While
    the bug is live the turn hangs through ``wait_for_thread_started`` and
    then fails with the ``startup timed out`` executor error, which the SPA
    renders as an error pill — that is exactly what this test rejects.
    """
    base_url, session_id = headless_codex_session
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)

    _send(page, "Review this diff and reply with your findings.")
    sent_at = time.monotonic()

    # Wait for the turn to reach a terminal, user-visible outcome: either an
    # assistant reply (thread started, model responded) or an error pill.
    outcome = page.locator(_ERROR_PILL).or_(page.locator(_ASSISTANT))
    expect(outcome.first).to_be_visible(timeout=int(_TURN_OUTCOME_TIMEOUT_S * 1000))
    elapsed = time.monotonic() - sent_at

    # The durable assertion runs against the canonical transcript, not the
    # pill's summarized text: no error item of this turn may be the
    # thread-start timeout.
    items = _client.get(f"{base_url}/v1/sessions/{session_id}/items?limit=50", timeout=10.0)
    items.raise_for_status()
    error_messages = [
        str(item.get("message", ""))
        for item in items.json()["data"]
        if item.get("type") == "error"
    ]
    timed_out_errors = [
        message for message in error_messages if _STARTUP_TIMEOUT_MARKER in message
    ]
    assert not timed_out_errors, (
        "codex-native headless turn burned the thread-start timeout "
        f"(after {elapsed:.0f}s) instead of starting a thread or failing fast "
        f"with a clear error: {timed_out_errors[0][:500]}"
    )

    # Belt and braces: the session must not carry the runner-side startup
    # failure marker either ("Codex TUI never started a thread" in the
    # runner log / "never started a thread" in the turn error).
    lingering = [m for m in error_messages if "never started a thread" in m]
    assert not lingering, (
        "codex-native headless turn still reports the TUI thread-start "
        f"failure: {lingering[0][:500]}"
    )
