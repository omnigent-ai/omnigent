"""E2E: an unpinned native Codex session launches on Omnigent's Codex default.

With a provider that pins no model and a Codex catalog whose own default
marker sits on ``gpt-6-astra`` while ``gpt-5.6-sol`` (Omnigent's
``CODEX_DEFAULT_MODEL``) is also servable, a new native Codex session without
a model pick must launch on Sol, not adopt the catalog's Astra default row.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page

from omnigent.models.model_fallbacks import CODEX_DEFAULT_MODEL
from omnigent.runner.identity import token_bound_runner_id
from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _codex_cli_supports_mocked_app_server,
    _create_native_codex_session,
    _find_free_port,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

_CODEX_CATALOG_DEFAULT = "gpt-6-astra"
_HEALTH_TIMEOUT_S = 60.0
_HEALTH_POLL_INTERVAL_S = 0.5
# A cold launch pays a codex catalog probe before the session's own codex
# TUI boots and paints its startup banner.
_TUI_BANNER_TIMEOUT_MS = 120_000


def _write_source_codex_home(codex_path: str, source_home: Path) -> None:
    """Write a Codex home whose catalog marks Astra as Codex's own default."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OPENAI_", "DATABRICKS_"))
    }
    env["CODEX_HOME"] = str(source_home)
    bundled = json.loads(
        subprocess.run(
            [codex_path, "debug", "models", "--bundled"],
            capture_output=True,
            check=True,
            env=env,
            timeout=30,
        ).stdout
    )
    template = next(model for model in bundled["models"] if model["visibility"] == "list")
    # Codex crowns the lowest-priority visible row as its default, so Astra at
    # priority 0 reproduces the report's ``isDefault: true`` catalog row
    # without any user ``model =`` pin.
    rows = [
        {
            **template,
            "slug": slug,
            "display_name": slug,
            "visibility": "list",
            "supported_in_api": True,
            "availability_nux": None,
            "upgrade": None,
            "priority": index * 2,
        }
        for index, slug in enumerate((_CODEX_CATALOG_DEFAULT, CODEX_DEFAULT_MODEL))
    ]
    catalog = source_home / "models.json"
    catalog.write_text(json.dumps({**bundled, "models": rows}), encoding="utf-8")
    (source_home / "config.toml").write_text(
        f"model_catalog_json = {json.dumps(str(catalog))}\n", encoding="utf-8"
    )


def _write_unpinned_provider_config(config_home: Path) -> None:
    """Route native Codex through a key provider that names no default model."""
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        """\
providers:
  codex-e2e-unpinned:
    kind: key
    default: openai
    openai:
      base_url: "http://127.0.0.1:9/v1"
      api_key: "sk-e2e-mock"
      wire_api: responses
""",
        encoding="utf-8",
    )


def _codex_pane_text(tmp_dir: Path) -> str:
    """Capture the managed codex tmux pane's visible text, or ``""``."""
    for socket in sorted(tmp_dir.glob("omnigent-terminal-*/tmux.sock")):
        try:
            sessions = subprocess.run(
                ["tmux", "-S", str(socket), "list-sessions", "-F", "#{session_name}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for name in sessions.stdout.split():
                captured = subprocess.run(
                    ["tmux", "-S", str(socket), "capture-pane", "-p", "-t", name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if captured.returncode == 0 and captured.stdout.strip():
                    return captured.stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
    return ""


@pytest.fixture
def unpinned_codex_astra_session(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, str, Path]]:
    """A runner-bound native Codex session with no model pin anywhere.

    Spawns a dedicated server + runner because the source ``CODEX_HOME`` /
    ``OMNIGENT_CONFIG_HOME`` / ``HOME`` must be present before the runner
    starts, and a private ``HOME`` keeps the shared model-catalog store cold so
    the launch probes this test's catalog.
    """
    if request.config.getoption("--ui-base-url"):
        pytest.skip("unpinned native Codex e2e requires an isolated spawned server")
    codex_path = os.environ.get("OMNIGENT_CODEX_PATH") or shutil.which("codex")
    if codex_path is None:
        pytest.skip("codex CLI is required for native Codex e2e")
    if not _codex_cli_supports_mocked_app_server(codex_path):
        pytest.skip("codex CLI >= 0.139.0 is required for mocked app-server e2e")
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for native Codex terminals")

    server_tmp = tmp_path_factory.mktemp("e2e_ui_unpinned_codex_server")
    config_home = server_tmp / "config-home"
    source_codex_home = server_tmp / "source-codex-home"
    home_dir = server_tmp / "home"
    state_dir = server_tmp / "codex-native-state"
    artifact_dir = server_tmp / "artifacts"
    for path in (source_codex_home, home_dir, state_dir, artifact_dir):
        path.mkdir(parents=True, exist_ok=True)
    # Keep managed-terminal private dirs on a SHORT path: tmux.sock must fit
    # the ~108-char unix socket limit, which the pytest basetemp tree exceeds.
    tmp_dir = Path(tempfile.mkdtemp(prefix="codexdef-"))
    _write_source_codex_home(codex_path, source_codex_home)
    _write_unpinned_provider_config(config_home)

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = server_tmp / "server.log"
    runner_log_path = server_tmp / "runner.log"
    db_path = server_tmp / "test.db"

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    shared_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OPENAI_", "DATABRICKS_"))
    }
    shared_env.update(
        {
            "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
            "CODEX_HOME": str(source_codex_home),
            "HOME": str(home_dir),
            "OMNIGENT_CODEX_PATH": str(codex_path),
            # Scope managed-terminal private dirs (tmux sockets) to this
            # fixture so the test can capture the codex pane's text.
            "TMPDIR": str(tmp_dir),
        }
    )
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        # Pin the runner's process log next to the fixture logs so a launch
        # failure is diagnosable from the test's own artifacts.
        "OMNIGENT_PROCESS_LOG_FILE": str(server_tmp / "runner-process.log"),
    }

    server_command = [
        sys.executable,
        "-c",
        "from omnigent.cli import main; main()",
        "server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--database-uri",
        f"sqlite:///{db_path}",
        "--artifact-location",
        str(artifact_dir),
    ]

    log_handle = open(log_path, "w")  # noqa: SIM115
    runner_log_handle = open(runner_log_path, "w")  # noqa: SIM115
    proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    try:
        proc = subprocess.Popen(
            server_command, env=server_env, stdout=log_handle, stderr=subprocess.STDOUT
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_log_handle,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        last_error = "not polled yet"
        while True:
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"server/runner not healthy within {_HEALTH_TIMEOUT_S:.0f}s "
                    f"(last_error={last_error}).\n"
                    f"Server log:\n{log_path.read_text()[-3000:]}\n"
                    f"Runner log:\n{runner_log_path.read_text()[-3000:]}"
                )
            if proc.poll() is not None:
                last_error = f"server exited with {proc.returncode}"
            elif runner_proc.poll() is not None:
                last_error = f"runner exited with {runner_proc.returncode}"
            else:
                try:
                    if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                        status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                        if status.status_code == 200 and status.json()["online"] is True:
                            break
                        last_error = f"runner status {status.status_code}: {status.text[:200]}"
                except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(_HEALTH_POLL_INTERVAL_S)

        session_id = _create_native_codex_session(base_url, runner_id)
        yield base_url, session_id, tmp_dir
    finally:
        if session_id is not None:
            with suppress(httpx.HTTPError):
                httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        for child in (runner_proc, proc):
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        runner_log_handle.close()
        log_handle.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.mark.timeout(420)
def test_unpinned_native_codex_session_launches_on_omnigent_default(
    page: Page,
    unpinned_codex_astra_session: tuple[str, str, Path],
) -> None:
    """A Default (unpinned) launch runs Sol, not Codex's Astra catalog default."""
    base_url, session_id, tmp_dir = unpinned_codex_astra_session
    page.goto(f"{base_url}/c/{session_id}")

    # Attaching the Terminal view is what makes the runner spawn Codex for a
    # terminal-first wrapper session; the launch resolves the model under test.
    _open_terminal_view(page)
    _wait_terminal_connected(page)

    # The Codex TUI startup banner names the model the session launched on
    # ("model:     <id>   /model to change"). The SPA renders the pane on a
    # WebGL canvas, so read the same text from the managed tmux pane.
    banner = re.compile(r"model:\s*(\S+)")
    deadline = time.monotonic() + _TUI_BANNER_TIMEOUT_MS / 1000
    pane_text = ""
    launched: str | None = None
    while time.monotonic() < deadline:
        pane_text = _codex_pane_text(tmp_dir)
        match = banner.search(pane_text)
        if match:
            launched = match.group(1)
            # Let the SPA terminal mirror the banner so a recording of this
            # run ends on the observable outcome.
            page.wait_for_timeout(3_000)
            break
        page.wait_for_timeout(1_000)
    assert launched is not None, (
        f"Codex TUI never painted its model banner; last pane text:\n{pane_text}"
    )
    assert launched == CODEX_DEFAULT_MODEL, (
        f"unpinned native Codex session launched on {launched!r} instead of "
        f"Omnigent's default {CODEX_DEFAULT_MODEL!r}; pane text:\n{pane_text}"
    )
