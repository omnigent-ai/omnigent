"""E2E: a codex-native thread-start timeout must not permanently mute the session.

When a codex-native launch's app-server stays alive but never emits
``thread/started`` within the startup budget, the runner's
``wait_for_thread_started`` hard-times-out, ``_codex_discover_thread_and_forward``
records a bridge startup error and returns, and the codex TUI pane is left
alive-but-hung. The per-turn self-heal (``_ensure_native_terminal_for_turn``)
only relaunches a *dead* pane, and nothing clears the recorded startup error,
so **every subsequent web-UI turn keeps failing** even after the launch that was
merely slow finally comes up. Without a recovery that keeps listening past the
deadline and adopts a late ``thread/started``, a slow/hung-then-recovered start
permanently mutes the session.

Journey (all user-observable):

1. configure ``harness.codex-native.command`` with a wrapper that performs
   cold-start setup for longer than the configured-command startup budget
   (``bridge.CODEX_NATIVE_CONFIGURED_COMMAND_STARTUP_TIMEOUT_SECONDS`` = 120s)
   before it execs the real Codex CLI;
2. create a fresh codex-native session and send a first prompt immediately —
   it dies at the startup-timeout watchdog with ``Codex native thread never
   started: ... startup timed out``, rendered as an error pill;
3. the wrapper then execs Codex (a marker file proves the launch was healthy —
   Codex really started, just later than the budget);
4. send a second prompt once Codex is confirmed up. The session must recover
   and complete this turn. While the bug is live it stays permanently muted —
   the second turn dies with the same startup-timeout error and no reply
   arrives.

The rig mirrors ``test_codex_native_configured_command_startup_timeout.py``
(own server + runner so the mock ``OMNIGENT_CONFIG_HOME`` / ``CODEX_HOME`` cannot
leak into other tests), with a mock openai Responses provider so the launch is
credentialed (``login_required=False`` — the watchdog path, not the login
fail-fast path). The assertions encode the DESIRED behavior — a healthy-but-late
codex recovers the session so a later turn completes — so this test FAILS while
the bug is live and passes once the runner adopts a late ``thread/started``
(or otherwise re-drives discovery / relaunches the hung pane) instead of leaving
the session permanently muted.
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
# The first turn errors at the 120s configured-command watchdog plus the
# executor's bridge-state poll; cold CI runners are slow, so stay generous.
_TURN_OUTCOME_TIMEOUT_S = 240.0
_ERROR_PILL = '[data-testid="error-pill"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# Wrapper setup delay: just PAST the 120s configured-command thread-start
# watchdog (``bridge.CODEX_NATIVE_CONFIGURED_COMMAND_STARTUP_TIMEOUT_SECONDS``),
# so the watchdog genuinely fires while the app-server is alive but no
# ``thread/started`` has arrived, then Codex execs a few seconds later.
_WRAPPER_SETUP_DELAY_S = 135

# Recovery window for the post-timeout turn: a healthy-but-late Codex connects to
# the still-alive app-server within seconds once the wrapper execs it, so a
# recovered session completes the turn quickly. Kept below the wrapper delay so a
# from-scratch pane relaunch (another full setup cycle) cannot masquerade as a fix.
_RECOVERY_TURN_TIMEOUT_S = 100.0

# Must match the model in the mock openai provider config written below.
_CODEX_MOCK_MODEL = "gpt-4o"

# Markers of the thread-start timeout in a turn's executor error text (the
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
def slow_wrapped_codex_session(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """A codex-native session launched through a wrapper that finishes past the watchdog.

    Spawns a dedicated server + runner whose ``OMNIGENT_CONFIG_HOME`` carries
    (a) a mock openai Responses provider (so the codex launch routes with a
    usable credential and arms the thread-start watchdog, not the login
    fail-fast path) and (b) ``harness.codex-native.command`` pointing at a
    wrapper that sleeps ``_WRAPPER_SETUP_DELAY_S`` (simulated cold-start setup
    that exceeds the 120s configured budget) before ``exec``-ing the real Codex
    CLI with the runner's launch args. The wrapper stamps marker files so the
    test can prove the launch was healthy (Codex really started, just late).

    :returns: ``(base_url, session_id, markers_dir)``.
    """
    codex_path = shutil.which("codex")
    assert codex_path is not None  # pytestmark guards this

    work = tmp_path_factory.mktemp("codex_slow_wrapped_startup")
    config_home = work / "config-home"
    codex_home = work / "codex-home"
    home_dir = work / "home"
    state_dir = work / "codex-native-state"
    artifacts = work / "artifacts"
    markers = work / "markers"
    for path in (config_home, codex_home, home_dir, state_dir, artifacts, markers):
        path.mkdir(parents=True, exist_ok=True)

    wrapper = work / "codex-slow-setup-wrapper.sh"
    wrapper.write_text(
        f"""#!/usr/bin/env bash
# A configured codex-native command whose cold-start setup runs LONGER than the
# 120s configured thread-start budget before launching Codex. The setup is
# simulated with a sleep so the timing boundary is deterministic; the launch
# itself is healthy — Codex really starts, just after the watchdog fires.
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
                "slow wrapped codex rig did not come online within "
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


def _turn_error_messages(base_url: str, session_id: str) -> list[str]:
    items = _client.get(f"{base_url}/v1/sessions/{session_id}/items?limit=100", timeout=10.0)
    items.raise_for_status()
    return [
        str(item.get("message", ""))
        for item in items.json()["data"]
        if item.get("type") == "error"
    ]


@pytest.mark.timeout(900)
def test_codex_thread_start_timeout_does_not_permanently_mute(
    page: Page,
    slow_wrapped_codex_session: tuple[str, str, Path],
    mock_llm_server_url: str,
) -> None:
    """A healthy-but-late codex launch must recover the session, not mute it forever.

    Journey under test: a codex-native session whose configured
    ``codex-native.command`` runs cold-start setup past the 120s watchdog gets
    a first prompt immediately after creation. That turn dies at the startup
    timeout (the app-server is alive but no ``thread/started`` yet). The wrapper
    then execs Codex a few seconds later (proven via the marker file). A second
    prompt sent once Codex is up must complete: the runner should adopt the late
    thread (or otherwise recover) instead of leaving the session permanently
    muted. While the bug is live the second turn dies with the same
    ``Codex native thread never started: ... startup timed out`` error — the
    session stays muted and no assistant reply arrives.
    """
    base_url, session_id, markers = slow_wrapped_codex_session

    nonce = uuid.uuid4().hex[:8]
    first_marker = f"mute-first-{nonce}"
    second_marker = f"mute-second-{nonce}"
    recovery_token = f"mute-recovered-{nonce}"

    reset_mock_llm(mock_llm_server_url)
    # Recovery-turn script: once the session recovers, the mock completes the
    # second turn with the token. Internal requests embedding the transcript
    # (helper threads, title generation) also match the marker queue, so pad
    # with extra entries — a stray consumer must not starve the completion.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": recovery_token}] * 8,
        key=second_marker,
        match=second_marker,
    )
    # Stray internal Codex calls (model-routed, no transcript) must not stall.
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)

    # Turn 1: sent immediately, it burns the thread-start watchdog and dies.
    _send(page, f"Context marker {first_marker}. Please reply.")
    first_outcome = page.locator(_ERROR_PILL).or_(page.locator(_ASSISTANT))
    expect(first_outcome.first).to_be_visible(timeout=int(_TURN_OUTCOME_TIMEOUT_S * 1000))

    # Rig validity: the wrapped launch must be HEALTHY — the wrapper really
    # exec'd Codex after its (over-budget) setup delay. Without this, a muted
    # session could be blamed on a broken wrapper instead of the missing
    # late-thread recovery, and the regression assertion below would be
    # meaningless.
    exec_marker = markers / "codex-exec"
    marker_deadline = time.monotonic() + _WRAPPER_SETUP_DELAY_S + 90.0
    while not exec_marker.exists() and time.monotonic() < marker_deadline:
        time.sleep(1.0)
    assert exec_marker.exists(), (
        "the configured wrapper never exec'd Codex — the rig is broken "
        f"(wrapper markers: {sorted(p.name for p in markers.iterdir())})"
    )

    # Confirm the first turn died on the thread-start timeout (the mute onset):
    # the failure that permanently disables the session while the bug is live.
    first_errors = _turn_error_messages(base_url, session_id)
    assert any(
        _STARTUP_TIMEOUT_MARKER in message or _NEVER_STARTED_MARKER in message
        for message in first_errors
    ), (
        "expected the first turn to die on the codex thread-start timeout so the "
        f"permanent-mute path is exercised, but saw errors: {first_errors}"
    )

    # Give the late Codex a moment to connect to the app-server and start its
    # thread before the recovery turn.
    time.sleep(20.0)

    # Turn 2: sent after Codex is confirmed up. A recovered session completes it.
    _send(page, f"Context marker {second_marker}. Reply with exactly: {recovery_token}")
    sent_at = time.monotonic()

    reply = page.locator(_ASSISTANT, has_text=recovery_token)
    try:
        expect(reply.first).to_be_visible(timeout=int(_RECOVERY_TURN_TIMEOUT_S * 1000))
    except AssertionError:
        elapsed = time.monotonic() - sent_at
        muted = [
            message
            for message in _turn_error_messages(base_url, session_id)
            if _STARTUP_TIMEOUT_MARKER in message or _NEVER_STARTED_MARKER in message
        ]
        raise AssertionError(
            "codex-native session stayed permanently muted after the thread-start "
            f"timeout: a turn sent {elapsed:.0f}s after Codex actually started did not "
            "complete because the runner never adopted the late thread / cleared the "
            f"startup error. Muted-turn errors: {muted[-1][:500] if muted else 'none'}"
        ) from None
