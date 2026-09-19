"""E2E guard: a reaped codex-native terminal pane self-heals on reopen.

Journey: a user opens a codex-native session, opens its Terminal view (the Codex
TUI launches under tmux and connects), then navigates away so the terminal WS —
and with it the tmux control client — detaches. Once the pane is unattended and
idle past the native-pane idle window, the idle reaper tears the Codex pane down
(codex process + per-session ``codex app-server``). Returning to the Terminal
view must transparently re-ensure the pane and reconnect the live TUI, rather
than stranding the user on a manual "Resume session" prompt: a terminal-first
session is driven entirely through its pane and has no composer turn to trigger
the turn-path pane recreation, so reopening the Terminal view is itself the
request for the pane.

The reap decision logic runs as-is; only its *timing* is compressed so the reap
is observable in bounded test time. Production defaults are a 3600s idle window,
a 120s tmux-activity busy window, and a 60s scan interval; here the idle window
is set to 1s via the supported ``OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S`` env knob
and a runner-side ``sitecustomize`` shrinks the busy window and scan cadence.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

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
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _CODEX_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)

_IDLE_TIMEOUT_S = "1"
_REAP_WAIT_S = 120

# Runner-side timing compression: scan interval is pure cadence (no busy decision
# depends on it); the tmux busy window is shortened so a genuinely-quiet pane is
# still required to read idle, just sooner than the 120s production window.
_SITECUSTOMIZE = """
try:
    from omnigent.terminals import pane_reaper as _pr
    _pr.PANE_OUTPUT_BUSY_WINDOW_S = 15.0
    if _pr.NativePaneReaper.__init__.__kwdefaults__:
        _pr.NativePaneReaper.__init__.__kwdefaults__["reaper_interval_s"] = 2.0
except Exception:
    pass
"""


@pytest.fixture
def codex_reaper_session(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, str, object]]:
    codex_path = shutil.which("codex")
    if codex_path is None:
        pytest.skip("codex CLI is required for the codex-native reap e2e")
    if not _codex_cli_supports_mocked_app_server(codex_path):
        pytest.skip("codex CLI >= 0.139.0 is required for the codex-native reap e2e")

    from omnigent.runner.identity import token_bound_runner_id

    server_tmp = tmp_path_factory.mktemp("e2e_ui_codex_reap_server")
    config_home = server_tmp / "config-home"
    source_codex_home = server_tmp / "source-codex-home"
    home_dir = server_tmp / "home"
    state_dir = server_tmp / "codex-native-state"
    artifact_dir = server_tmp / "artifacts"
    shim_dir = server_tmp / "shim"
    for path in (source_codex_home, home_dir, state_dir, artifact_dir, shim_dir):
        path.mkdir(parents=True, exist_ok=True)
    (shim_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")

    _write_mock_codex_provider_config(
        config_home, f"{mock_llm_server_url}/v1", model=_CODEX_MOCK_MODEL
    )

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = server_tmp / "server.log"
    runner_log_path = server_tmp / "runner.log"
    db_path = server_tmp / "test.db"
    agent_yaml_path = server_tmp / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML, encoding="utf-8")

    import secrets as _secrets

    binding_token = _secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    shared_env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "CODEX_HOME": str(source_codex_home),
        "HOME": str(home_dir),
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "OMNIGENT_LOG_LEVEL": "INFO",
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "PYTHONPATH": f"{shim_dir}{os.pathsep}{shared_env['PYTHONPATH']}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        "OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S": _IDLE_TIMEOUT_S,
        "OMNIGENT_PROCESS_LOG_FILE": str(runner_log_path),
    }

    log_handle = open(log_path, "w")  # noqa: SIM115 — closed in finally
    proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "omnigent.cli", "server",
                "--host", "127.0.0.1", "--port", str(port),
                "--database-uri", f"sqlite:///{db_path}",
                "--artifact-location", str(artifact_dir),
                "--agent", str(agent_yaml_path),
            ],
            env=server_env, stdout=log_handle, stderr=subprocess.STDOUT,
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        )

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        ready = False
        last_error = "not polled yet"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                last_error = f"server exited early ({proc.returncode})"
                break
            if runner_proc.poll() is not None:
                last_error = f"runner exited early ({runner_proc.returncode})"
                break
            try:
                resp = httpx.get(f"{base_url}/health", timeout=2)
                if resp.status_code == 200:
                    status_resp = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status_resp.status_code == 200 and status_resp.json().get("online") is True:
                        ready = True
                        break
                    last_error = f"runner status HTTP {status_resp.status_code}"
                else:
                    last_error = f"health HTTP {resp.status_code}"
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(_HEALTH_POLL_INTERVAL_S)

        if not ready:
            raise RuntimeError(f"server did not come online: {last_error}")

        session_id = _create_native_codex_session(base_url, runner_id, model=_CODEX_MOCK_MODEL)
        yield (base_url, session_id, runner_id, runner_log_path)
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        for child in (runner_proc, proc):
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        log_handle.close()


def _terminal_ids(base_url: str, session_id: str) -> list[str]:
    """Ids of the session's live terminal resources (empty once reaped)."""
    try:
        r = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources/terminals", timeout=5)
        if r.status_code == 200:
            return [t.get("id", "") for t in r.json().get("data", [])]
    except httpx.HTTPError:
        pass
    return []


def _reaped(runner_log_path: object, session_id: str) -> bool:
    if not os.path.exists(str(runner_log_path)):
        return False
    pattern = re.compile(rf"reaping idle native pane for conversation {re.escape(session_id)} \(codex")
    with open(str(runner_log_path)) as handle:
        return any(pattern.search(line) for line in handle)


@pytest.mark.timeout(300)
def test_codex_reaped_pane_auto_recovers_on_terminal_reopen(
    browser: Browser,
    codex_reaper_session: tuple[str, str, str, object],
) -> None:
    base_url, session_id, _runner_id, runner_log_path = codex_reaper_session

    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    context_kwargs: dict[str, object] = {"viewport": {"width": 1280, "height": 800}}
    if record_dir:
        os.makedirs(record_dir, exist_ok=True)
        context_kwargs["record_video_dir"] = record_dir
    context = browser.new_context(**context_kwargs)
    page: Page = context.new_page()
    try:
        page.goto(f"{base_url}/c/{session_id}")
        _open_terminal_view(page)
        _wait_terminal_connected(page)
        assert _terminal_ids(base_url, session_id), "codex terminal resource should be live"

        # Navigate away: the terminal WS (and its tmux control client) detaches,
        # leaving the pane unattended and idle-eligible.
        page.goto(f"{base_url}/")
        page.wait_for_timeout(1000)

        start = time.monotonic()
        while time.monotonic() - start < _REAP_WAIT_S:
            if _reaped(runner_log_path, session_id):
                break
            time.sleep(3)

        assert _reaped(runner_log_path, session_id), (
            f"codex pane was not reaped within {_REAP_WAIT_S}s after detaching "
            f"(idle_timeout={_IDLE_TIMEOUT_S}s)"
        )
        # The reaper closed the terminal resource; it is gone from the registry.
        assert _terminal_ids(base_url, session_id) == [], "reaped codex terminal should be gone"

        # Return to the session (a fresh page load, as reopening a tab would be)
        # and open the Terminal view again. The reaped pane must be re-ensured
        # transparently and the live TUI reconnect — no manual Resume click.
        page.goto(f"{base_url}/c/{session_id}")
        _open_terminal_view(page)
        _wait_terminal_connected(page)
        expect(page.get_by_role("button", name="Resume session")).to_have_count(0)
        assert _terminal_ids(base_url, session_id), "codex terminal should be re-ensured on reopen"
    finally:
        context.close()
