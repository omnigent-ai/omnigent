"""E2E: an early codex-native TUI startup failure must report its real cause.

The native Codex TUI is a separate ``--remote`` process from the app-server.
If it dies before creating a thread, the first chat turn must report the TUI's
exit status and, when opted in, parser error instead of a discovery timeout.

The rig forces the failure deterministically by injecting an unsupported
launch arg via ``harness.codex-native.args`` — the real ``codex`` binary then
prints ``error: unexpected argument '--omni-telemetry-invalid-flag' found``
and exits 2 before any thread exists. A mock provider routes the codex harness
so login is not required (the launch is otherwise healthy).

The test requires a visible error and a canonical transcript error containing
exit status 2, with parser output included only when diagnostics are enabled.
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
_HEALTH_TIMEOUT_S = 60.0
# Allow slow CI startup while still requiring the specific parser error.
_TURN_OUTCOME_TIMEOUT_S = 150.0
_ERROR_PILL = '[data-testid="error-pill"]'
_BAD_FLAG = "--omni-telemetry-invalid-flag"

# The generic thread-discovery-timeout markers the buggy path emits instead of
# the real TUI startup cause.
_TIMEOUT_MARKER = "startup timed out"
_NO_THREAD_MARKER = "never started a thread"

# Proxy-blind client: CI forces an egress proxy that must not intercept
# loopback requests to the spawned server.
_client = httpx.Client(trust_env=False)
for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _no_proxy_env() -> dict[str, str]:
    env = os.environ.copy()
    for var in ("NO_PROXY", "no_proxy"):
        env[var] = ",".join(filter(None, [env.get(var, ""), "127.0.0.1,localhost"]))
    return env


@pytest.fixture(params=[True, False], ids=["capture-on", "capture-off"])
def bad_flag_codex_session(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, str, bool]]:
    """A codex-native session whose TUI exits 2 at startup on a bad launch arg.

    Spawns a dedicated server + runner (so the injected config and empty
    ``CODEX_HOME`` cannot leak into other tests), configures a mock provider
    that routes the codex harness, and injects an unsupported launch arg so
    the real Codex TUI dies immediately. Both values of
    ``OMNIGENT_HARNESS_STDERR_ENABLED`` exercise the startup-text export gate.

    :returns: ``(base_url, session_id, capture_enabled)``.
    """
    if shutil.which("codex") is None:
        pytest.skip("codex CLI is required for the codex-native early-startup rig")

    capture_enabled = bool(request.param)
    work = tmp_path_factory.mktemp("codex_early_startup")
    config_home = work / "config-home"
    codex_home = work / "codex-home"
    home_dir = work / "home"
    state_dir = work / "codex-native-state"
    artifacts = work / "artifacts"
    for path in (config_home, codex_home, home_dir, state_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)

    (config_home / "config.yaml").write_text(
        "providers:\n"
        "  mock-codex:\n"
        "    kind: key\n"
        "    default: [openai]\n"
        "    openai:\n"
        f'      base_url: "{mock_llm_server_url}/v1"\n'
        '      api_key: "mock-key"\n'
        "      wire_api: responses\n"
        "      models:\n"
        "        default: gpt-4o\n"
        "harness:\n"
        "  codex-native:\n"
        "    args:\n"
        f'      - "{_BAD_FLAG}"\n'
    )

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
        "OMNIGENT_HARNESS_STDERR_ENABLED": "1" if capture_enabled else "0",
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
                # Transient HTTP failures are expected while the local rig starts.
                pass
            time.sleep(0.5)
        if not online:
            raise RuntimeError(
                "codex early-startup rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )

        session_id = _create_native_codex_session(base_url, runner_id)
        yield (base_url, session_id, capture_enabled)
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
def test_codex_early_tui_startup_failure_reports_cause_not_timeout(
    page: Page,
    bad_flag_codex_session: tuple[str, str, bool],
) -> None:
    """The first turn must expose exit 2 and gate the TUI's parser text.

    Journey: open a codex-native session whose TUI exits 2 at startup, send
    the first chat message, and require an error with the actual startup
    cause in the transcript rather than a generic thread-discovery timeout.
    Disabling diagnostic export must omit terminal text from the chat error.
    """
    base_url, session_id, capture_enabled = bad_flag_codex_session
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)

    _send(page, "Run a harmless command and report the result.")
    sent_at = time.monotonic()

    expect(page.locator(_ERROR_PILL).first).to_be_visible(
        timeout=int(_TURN_OUTCOME_TIMEOUT_S * 1000)
    )
    elapsed = time.monotonic() - sent_at

    # Assert against the canonical transcript, not the pill's summarized text.
    items = _client.get(f"{base_url}/v1/sessions/{session_id}/items?limit=50", timeout=10.0)
    items.raise_for_status()
    error_messages = [
        str(item.get("message", ""))
        for item in items.json()["data"]
        if item.get("type") == "error"
    ]
    assert error_messages, "The visible startup error must be recorded in the transcript."
    startup_errors = [
        message
        for message in error_messages
        if any(
            f"Codex terminal exited with status 2 {stage}." in message
            for stage in ("before starting a thread", "before becoming available")
        )
    ]
    assert startup_errors, f"Expected the TUI exit status in the error: {error_messages!r}"
    if capture_enabled:
        assert any(
            "unexpected argument" in message and _BAD_FLAG in message for message in startup_errors
        ), f"Expected the TUI exit status and parser cause in one error: {error_messages!r}"
    else:
        assert all(
            "Codex startup terminal output:" not in message
            and "unexpected argument" not in message
            and _BAD_FLAG not in message
            for message in error_messages
        ), f"Disabled diagnostic export leaked terminal text into chat: {error_messages!r}"

    timed_out = [m for m in error_messages if _TIMEOUT_MARKER in m]
    assert not timed_out, (
        "codex-native early-TUI-startup failure burned the thread-start timeout "
        f"(after {elapsed:.0f}s) instead of reporting the actual startup cause "
        f"(the TUI exited 2 with {_BAD_FLAG!r}): {timed_out[0][:500]}"
    )
    no_thread = [m for m in error_messages if _NO_THREAD_MARKER in m]
    assert not no_thread, (
        "codex-native early-TUI-startup failure still reports the generic "
        f"'never started a thread' cause instead of the real exit status: "
        f"{no_thread[0][:500]}"
    )
