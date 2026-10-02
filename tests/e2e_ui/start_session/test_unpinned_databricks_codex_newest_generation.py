"""E2E: Databricks codex discovery must not default past a GPT-6 tier arm.

With a workspace whose Unity Catalog model-services listing advertises
``system.ai.gpt-6-luna`` (the current major-only tiered arm) alongside older
GPT-5.x variants, an unpinned native Codex session must launch on the newest
advertised generation — not lag on a GPT-5.x id.
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
import threading
import time
from collections.abc import Iterator
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page

from omnigent.models.codex_model_vocabulary import comparable_model_id
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
from tests.e2e_ui.start_session.test_unpinned_codex_default_model import _codex_pane_text

_DATABRICKS_PROFILE = "e2e-mock"
_WORKSPACE_MODELS = (
    "system.ai.gpt-6-luna",
    "system.ai.gpt-5-5",
    "system.ai.gpt-5-4-mini",
)
_NEWEST_ADVERTISED = "system.ai.gpt-6-luna"
# Models the codex TUI could plausibly launch on: the advertised workspace
# listing plus Omnigent's static launch default (a discovery bypass).
_LAUNCH_CANDIDATES = {
    comparable_model_id(model_id): model_id
    for model_id in (*_WORKSPACE_MODELS, CODEX_DEFAULT_MODEL)
}
_MODEL_SERVICES_PATH = "/api/2.1/unity-catalog/model-services"
_HEALTH_TIMEOUT_S = 60.0
_HEALTH_POLL_INTERVAL_S = 0.5
# A cold launch pays workspace discovery plus a codex catalog probe before
# the session's own codex TUI boots and paints its startup banner.
_TUI_BANNER_TIMEOUT_MS = 120_000


class _MockWorkspaceHandler(BaseHTTPRequestHandler):
    """Unity Catalog model-services listing for a fake Databricks workspace."""

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == _MODEL_SERVICES_PATH:
            body = json.dumps(
                {
                    "model_services": [
                        {"name": f"model-services/{model_id}"} for model_id in _WORKSPACE_MODELS
                    ]
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        return


def _launched_model(pane_text: str) -> str | None:
    """Extract the model id the codex TUI reports it launched on.

    The TUI has painted the model as ``model: <id>`` and as a ``<id> <effort>``
    status-footer across versions, so match any pane token that folds to a
    launch candidate instead of one banner shape.
    """
    for token in re.findall(r"[A-Za-z0-9._/\[\]-]+", pane_text):
        if comparable_model_id(token) in _LAUNCH_CANDIDATES:
            return token
    return None


def _write_databricks_provider_config(config_home: Path) -> None:
    """Route native Codex through a Databricks profile provider."""
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        f"""\
providers:
  codex-e2e-databricks:
    kind: databricks
    default: true
    profile: {_DATABRICKS_PROFILE}
""",
        encoding="utf-8",
    )


def _write_databrickscfg(home_dir: Path, workspace_url: str) -> None:
    """Point the launch profile at the mock workspace with a plain PAT."""
    (home_dir / ".databrickscfg").write_text(
        f"[{_DATABRICKS_PROFILE}]\nhost = {workspace_url}\ntoken = dapi-e2e-mock-token\n",
        encoding="utf-8",
    )


@pytest.fixture
def databricks_codex_gpt6_session(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, str, Path]]:
    """A runner-bound native Codex session on a GPT-6-advertising workspace.

    Spawns a dedicated server + runner because the private ``HOME`` (holding
    ``.databrickscfg``) and ``OMNIGENT_CONFIG_HOME`` must exist before the
    runner starts, and a cold shared model-catalog store keeps the launch on
    this test's live workspace discovery.
    """
    if request.config.getoption("--ui-base-url"):
        pytest.skip("Databricks-discovery native Codex e2e requires an isolated spawned server")
    codex_path = os.environ.get("OMNIGENT_CODEX_PATH") or shutil.which("codex")
    if codex_path is None:
        pytest.skip("codex CLI is required for native Codex e2e")
    if not _codex_cli_supports_mocked_app_server(codex_path):
        pytest.skip("codex CLI >= 0.139.0 is required for mocked app-server e2e")
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for native Codex terminals")

    server_tmp = tmp_path_factory.mktemp("e2e_ui_dbx_codex_gpt6_server")
    config_home = server_tmp / "config-home"
    source_codex_home = server_tmp / "source-codex-home"
    home_dir = server_tmp / "home"
    state_dir = server_tmp / "codex-native-state"
    artifact_dir = server_tmp / "artifacts"
    for path in (config_home, source_codex_home, home_dir, state_dir, artifact_dir):
        path.mkdir(parents=True, exist_ok=True)
    # Keep managed-terminal private dirs on a SHORT path: tmux.sock must fit
    # the ~108-char unix socket limit, which the pytest basetemp tree exceeds.
    tmp_dir = Path(tempfile.mkdtemp(prefix="codexgpt6-"))

    workspace = ThreadingHTTPServer(("127.0.0.1", 0), _MockWorkspaceHandler)
    workspace_thread = threading.Thread(target=workspace.serve_forever, daemon=True)
    workspace_thread.start()
    workspace_url = f"http://127.0.0.1:{workspace.server_address[1]}"

    _write_databricks_provider_config(config_home)
    _write_databrickscfg(home_dir, workspace_url)

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
        workspace.shutdown()
        workspace_thread.join(timeout=5)
        runner_log_handle.close()
        log_handle.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.mark.timeout(420)
def test_unpinned_databricks_codex_session_launches_newest_advertised_generation(
    page: Page,
    databricks_codex_gpt6_session: tuple[str, str, Path],
) -> None:
    """An unpinned Databricks Codex launch runs GPT-6 Luna, not an older GPT-5.x."""
    base_url, session_id, tmp_dir = databricks_codex_gpt6_session
    page.goto(f"{base_url}/c/{session_id}")

    # Attaching the Terminal view is what makes the runner spawn Codex for a
    # terminal-first wrapper session; the launch resolves the model under test.
    _open_terminal_view(page)
    _wait_terminal_connected(page)

    # The Codex TUI startup banner/footer names the model the session
    # launched on. The SPA renders the pane on a WebGL canvas, so read the
    # same text from the managed tmux pane.
    deadline = time.monotonic() + _TUI_BANNER_TIMEOUT_MS / 1000
    pane_text = ""
    launched: str | None = None
    while time.monotonic() < deadline:
        pane_text = _codex_pane_text(tmp_dir)
        launched = _launched_model(pane_text)
        if launched is not None:
            # Let the SPA terminal mirror the banner so a recording of this
            # run ends on the observable outcome.
            page.wait_for_timeout(3_000)
            break
        page.wait_for_timeout(1_000)
    assert launched is not None, (
        f"Codex TUI never painted its launch model; last pane text:\n{pane_text}"
    )
    assert comparable_model_id(launched) == comparable_model_id(_NEWEST_ADVERTISED), (
        f"workspace advertises {', '.join(_WORKSPACE_MODELS)} but the unpinned "
        f"native Codex session launched on {launched!r} instead of the newest "
        f"advertised generation {_NEWEST_ADVERTISED!r}; pane text:\n{pane_text}"
    )
