"""E2E: a configured codex-native command's setup must not trip the launch watchdog.

Regression test: an explicit
``harness.codex-native.command`` wrapper may perform cold-start setup before
launching Codex, so a *healthy* launch can emit ``thread/started`` after the
direct-launch 30s watchdog
(``bridge.CODEX_NATIVE_DIRECT_THREAD_START_TIMEOUT_SECONDS``) expires. The
runner then records::

    Codex app-server never started a thread (startup timed out: TimeoutError). ...

into the bridge startup error, and the user's first chat message dies with
that error even though the wrapper is still progressing and Codex starts
shortly afterward. Expected: configured commands get a larger bounded startup
allowance, while direct Codex launches keep their existing timeout.

Journey (all user-observable):

1. configure ``harness.codex-native.command`` with a wrapper that performs
   cold-start setup before launching Codex;
2. create a fresh codex-native session and send its first prompt immediately;
3. the turn dies at the 30s ``thread/started`` deadline with the startup
   timeout error, even though Codex starts a few seconds later.

The rig mirrors ``test_codex_native_headless_login_timeout.py`` (own server +
runner so the mock ``OMNIGENT_CONFIG_HOME`` / ``CODEX_HOME`` cannot leak into
other tests), with a mock openai Responses provider routed so the launch is
credentialed (``login_required=False`` — the watchdog path, not the login
fail-fast path). The wrapper's setup is simulated with a sleep just past the
watchdog to reproduce the boundary deterministically; a marker file written at
``exec codex`` time proves the launch was healthy, so the failing assertion can
only be the premature watchdog. The assertions encode the DESIRED behavior —
the first turn completes with no startup-timeout error — so this test FAILS
while the bug is live and passes once configured commands get their larger
bounded allowance.
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
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _create_native_codex_session,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view, _send

_REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None or shutil.which("tmux") is None,
    reason="codex-native e2e needs the `codex` CLI and `tmux` on PATH.",
)

# Boot budget for the spawned server + runner pair.
_HEALTH_TIMEOUT_S = 60.0
# The buggy path errors at the 30s thread-start watchdog plus the executor's
# bridge-state poll; the fixed path completes after the wrapper's setup delay
# plus a mock model turn. Cold CI runners are slow, so stay generous.
_TURN_OUTCOME_TIMEOUT_S = 240.0
_ERROR_PILL = '[data-testid="error-pill"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# Wrapper setup delay: just past the direct-launch 30s thread-start watchdog
# (``bridge.CODEX_NATIVE_DIRECT_THREAD_START_TIMEOUT_SECONDS``). Codex's own
# boot adds a few seconds on top before ``thread/started``.
_WRAPPER_SETUP_DELAY_S = 45

# Must match the model in the mock openai provider config written below.
_CODEX_MOCK_MODEL = "gpt-4o"

# Markers of the regression failure in the turn's executor error text (the
# runner's bridge startup error, surfaced verbatim by the codex-native
# executor as "Codex native thread never started: ...").
_STARTUP_TIMEOUT_MARKER = "startup timed out"
_NEVER_STARTED_MARKER = "never started a thread"


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


def _clean_env() -> dict[str, str]:
    """Ambient env with loopback proxy-excluded and runner/host vars stripped.

    Stripping ``OMNIGENT_RUNNER_*`` / ``OMNIGENT_HOST_*`` matters when the
    test itself runs inside a server-spawned runner: leaked zygote/tunnel
    vars make the spawned child runner take the zygote-fork path and hang.
    ``OMNIGENT_PROCESS_LOG_FILE`` / ``OMNIGENT_DATA_DIR`` are host-owned
    write paths; the spawned pair must not write into (or crash on) the
    calling host's log/data locations.
    """
    env = os.environ.copy()
    for var in ("NO_PROXY", "no_proxy"):
        existing = env.get(var, "")
        env[var] = ",".join(filter(None, [existing, "127.0.0.1,localhost"]))
    for key in list(env):
        if key.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_HOST_")):
            del env[key]
    for key in ("RUNNER_SERVER_URL", "OMNIGENT_PROCESS_LOG_FILE", "OMNIGENT_DATA_DIR"):
        env.pop(key, None)
    return env


@pytest.fixture
def wrapped_codex_session(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """A codex-native session launched through a slow configured wrapper command.

    Spawns a dedicated server + runner whose ``OMNIGENT_CONFIG_HOME`` carries
    (a) a mock openai Responses provider (so the codex launch routes with a
    usable credential and arms the thread-start watchdog, not the login
    fail-fast path) and (b) ``harness.codex-native.command`` pointing at a
    wrapper that sleeps ``_WRAPPER_SETUP_DELAY_S`` (simulated cold-start
    setup) before ``exec``-ing the real Codex CLI with the runner's launch
    args. The wrapper stamps marker files so the test can prove the launch
    was healthy (Codex really started, just late).

    :returns: ``(base_url, session_id, markers_dir)``.
    """
    codex_path = shutil.which("codex")
    assert codex_path is not None  # pytestmark guards this

    work = tmp_path_factory.mktemp("codex_wrapped_startup")
    config_home = work / "config-home"
    codex_home = work / "codex-home"
    home_dir = work / "home"
    state_dir = work / "codex-native-state"
    artifacts = work / "artifacts"
    markers = work / "markers"
    for path in (config_home, codex_home, home_dir, state_dir, artifacts, markers):
        path.mkdir(parents=True, exist_ok=True)

    wrapper = work / "codex-setup-wrapper.sh"
    wrapper.write_text(
        f"""#!/usr/bin/env bash
# A configured codex-native command that performs cold-start setup before
# launching Codex (the representative shape). The setup is simulated with a sleep
# so the timing boundary is deterministic; the launch itself is healthy.
set -eu
date +%s > "{markers}/wrapper-started"
sleep {_WRAPPER_SETUP_DELAY_S}
date +%s > "{markers}/codex-exec"
exec "{codex_path}" "$@"
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    (config_home / "config.yaml").write_text(
        f"""\
providers:
  mock-codex:
    kind: key
    default: [openai]
    openai:
      base_url: "{mock_llm_server_url}/v1"
      api_key: "mock-key"
      wire_api: responses
      models:
        default: {_CODEX_MOCK_MODEL}
harness:
  codex-native:
    command: {wrapper}
""",
        encoding="utf-8",
    )

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)

    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    shared_env = {
        **_clean_env(),
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
                "wrapped codex rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )

        session_id = _create_native_codex_session(base_url, runner_id)
        yield (base_url, session_id, markers)
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


@pytest.mark.timeout(600)
def test_configured_codex_command_setup_survives_startup_watchdog(
    page: Page,
    wrapped_codex_session: tuple[str, str, Path],
    mock_llm_server_url: str,
) -> None:
    """A healthy wrapped codex launch must not die at the 30s watchdog.

    Journey under test: a codex-native session whose configured
    ``codex-native.command`` performs cold-start setup gets its first prompt
    immediately after creation. While the bug is live, the runner's 30s
    ``thread/started`` watchdog fires mid-setup, records the startup error,
    and the turn dies with ``Codex native thread never started: Codex
    app-server never started a thread (startup timed out: TimeoutError)`` —
    rendered by the SPA as an error pill — even though the wrapper execs
    Codex a few seconds later (proven via the marker file).
    """
    base_url, session_id, markers = wrapped_codex_session

    nonce = uuid.uuid4().hex[:8]
    user_marker = f"wrapped-start-{nonce}"
    assistant_token = f"wrapped-start-done-{nonce}"

    reset_mock_llm(mock_llm_server_url)
    # Main-turn script for the fixed path: the mock model completes the turn
    # with the token. Internal requests embedding the transcript (helper
    # threads, title generation) also match the marker queue, so pad with
    # extra entries — a stray consumer must not starve the main completion.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": assistant_token}] * 8,
        key=user_marker,
        match=user_marker,
    )
    # Stray internal Codex calls (model-routed, no transcript) must not stall.
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)
    _send(
        page,
        f"Context marker {user_marker}. Reply with exactly: {assistant_token}",
    )
    sent_at = time.monotonic()

    # Wait for the first turn to reach a terminal, user-visible outcome:
    # either an assistant reply (thread started, model responded) or an
    # error pill (the premature startup failure).
    outcome = page.locator(_ERROR_PILL).or_(page.locator(_ASSISTANT))
    expect(outcome.first).to_be_visible(timeout=int(_TURN_OUTCOME_TIMEOUT_S * 1000))
    elapsed = time.monotonic() - sent_at

    # Rig validity: the wrapped launch must be HEALTHY — the wrapper really
    # exec'd Codex after its setup delay. Without this, a startup-timeout
    # error could come from a broken wrapper instead of the premature
    # watchdog, and the regression assertion below would be meaningless.
    exec_marker = markers / "codex-exec"
    marker_deadline = time.monotonic() + _WRAPPER_SETUP_DELAY_S + 90.0
    while not exec_marker.exists() and time.monotonic() < marker_deadline:
        time.sleep(1.0)
    assert exec_marker.exists(), (
        "the configured wrapper never exec'd Codex — the rig is broken "
        f"(wrapper markers: {sorted(p.name for p in markers.iterdir())})"
    )

    # THE BUG: the durable assertion runs against the canonical transcript.
    # No error item of this turn may be the thread-start watchdog timeout —
    # the launch was healthy, merely slower than the direct-launch budget.
    items = _client.get(f"{base_url}/v1/sessions/{session_id}/items?limit=50", timeout=10.0)
    items.raise_for_status()
    error_messages = [
        str(item.get("message", ""))
        for item in items.json()["data"]
        if item.get("type") == "error"
    ]
    premature = [
        message
        for message in error_messages
        if _STARTUP_TIMEOUT_MARKER in message or _NEVER_STARTED_MARKER in message
    ]
    assert not premature, (
        "codex-native configured-command launch died at the direct-launch "
        f"thread-start watchdog (after {elapsed:.0f}s) even though the wrapper "
        "exec'd Codex shortly afterward — configured commands need a larger "
        f"bounded startup allowance: {premature[0][:500]}"
    )

    # And the journey must actually complete: the wrapped launch's thread
    # serves the first turn once the allowance covers the setup delay.
    reply = page.locator(_ASSISTANT, has_text=assistant_token)
    expect(reply.first).to_be_visible(timeout=120_000)
