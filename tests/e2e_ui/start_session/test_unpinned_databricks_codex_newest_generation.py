"""E2E: an unpinned Databricks Codex session launches on the newest advertised
uncurated GPT.

With a Unity Catalog model-services listing that advertises
``system.ai.gpt-6-terra`` (a major-only tiered arm) next to older, uncurated
GPT-5.x ids, a new native Codex session that pins no model must launch on the
newest advertised generation instead of lagging on a GPT-5.x id. Every
advertised id is uncurated, so ranking turns purely on generation; under the
retained curated-first policy an older curated arm would still outrank an
uncurated GPT-6. The advertised arm is none of Omnigent's launch-default
preference ids, so a launch that fell back to a static default instead of
ranking the live listing would paint a different model and be caught here.
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
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page

from omnigent.models.codex_model_vocabulary import comparable_model_id
from omnigent.models.model_fallbacks import CODEX_LAUNCH_DEFAULT_PREFERENCE
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
_NEWEST_ADVERTISED = "system.ai.gpt-6-terra"
_MODEL_SERVICES_PATH = "/api/2.1/unity-catalog/model-services"
_HEALTH_TIMEOUT_S = 60.0
_HEALTH_POLL_INTERVAL_S = 0.5
# A cold launch pays workspace discovery plus a codex catalog probe before the
# session's own codex TUI boots and paints its startup banner.
_TUI_BANNER_TIMEOUT_MS = 120_000


# A workspace advertising the newest major-only tiered arm beside older,
# uncurated GPT-5.x ids: ranking turns purely on generation, so the arm wins
# only when discovery reads it as GPT-6, and it is no launch-default fallback.
_ADVERTISED_MODEL_IDS = (
    "system.ai.gpt-6-terra",
    "system.ai.gpt-5-6-mini",
    "system.ai.gpt-5-5-mini",
)


class _MockWorkspaceHandler(BaseHTTPRequestHandler):
    server: MockWorkspace

    def do_GET(self) -> None:
        self.server.requests.append(self.path)
        if self.path.split("?", 1)[0] != _MODEL_SERVICES_PATH:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(
            {
                "model_services": [
                    {"name": f"model-services/{model_id}"} for model_id in self.server.model_ids
                ]
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


class MockWorkspace(ThreadingHTTPServer):
    """A Databricks workspace that only answers the Unity Catalog model-services listing."""

    def __init__(self, model_ids: tuple[str, ...]) -> None:
        # Set the attributes the handler reads before the socket can accept.
        self.model_ids = model_ids
        self.requests: list[str] = []
        super().__init__(("127.0.0.1", 0), _MockWorkspaceHandler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


@dataclass(frozen=True)
class CodexStack:
    """A dedicated server + runner routing native Codex through a mock Databricks workspace."""

    base_url: str
    runner_id: str
    workspace: MockWorkspace
    tmp_dir: Path
    server_log: Path
    runner_log: Path


def _write_databricks_provider_config(config_home: Path) -> None:
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
    (home_dir / ".databrickscfg").write_text(
        f"[{_DATABRICKS_PROFILE}]\nhost = {workspace_url}\ntoken = dapi-e2e-mock-token\n",
        encoding="utf-8",
    )


def _wait_healthy(
    base_url: str,
    runner_id: str,
    proc: subprocess.Popen[bytes],
    runner_proc: subprocess.Popen[bytes],
    server_log: Path,
    runner_log: Path,
) -> None:
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    last_error = "not polled yet"
    while True:
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"server/runner not healthy within {_HEALTH_TIMEOUT_S:.0f}s "
                f"(last_error={last_error}).\n"
                f"Server log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )
        # A child that already exited can never become healthy; fail fast with
        # its log instead of spinning until the deadline.
        if proc.poll() is not None:
            raise RuntimeError(
                f"server exited with {proc.returncode} before becoming healthy.\n"
                f"Server log:\n{server_log.read_text()[-3000:]}"
            )
        if runner_proc.poll() is not None:
            raise RuntimeError(
                f"runner exited with {runner_proc.returncode} before becoming healthy.\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                if status.status_code == 200 and status.json().get("online") is True:
                    return
                last_error = f"runner status {status.status_code}: {status.text[:200]}"
        except httpx.TransportError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        except json.JSONDecodeError as exc:
            last_error = f"runner status returned non-JSON: {exc}"
        time.sleep(_HEALTH_POLL_INTERVAL_S)


@contextmanager
def dedicated_databricks_codex_stack(
    model_ids: tuple[str, ...], server_tmp: Path, codex_path: str
) -> Iterator[CodexStack]:
    """Spawn a server + runner whose only provider is a Databricks profile at a mock workspace.

    A dedicated pair is needed because the private ``HOME`` (``.databrickscfg``)
    and ``OMNIGENT_CONFIG_HOME`` must exist before the runner starts, and a cold
    shared catalog store keeps the launch on live workspace discovery.
    """
    config_home = server_tmp / "config-home"
    source_codex_home = server_tmp / "source-codex-home"
    home_dir = server_tmp / "home"
    state_dir = server_tmp / "codex-native-state"
    artifact_dir = server_tmp / "artifacts"
    for path in (config_home, source_codex_home, home_dir, state_dir, artifact_dir):
        path.mkdir(parents=True, exist_ok=True)
    tmp_dir: Path | None = None
    workspace: MockWorkspace | None = None
    workspace_thread: threading.Thread | None = None
    serving = False
    proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    try:
        # tmux.sock must fit the ~108-char unix socket limit the pytest basetemp tree exceeds.
        tmp_dir = Path(tempfile.mkdtemp(prefix="codexgpt6-"))
        workspace = MockWorkspace(model_ids)
        workspace_thread = threading.Thread(target=workspace.serve_forever, daemon=True)
        workspace_thread.start()
        serving = True
        _write_databricks_provider_config(config_home)
        _write_databrickscfg(home_dir, workspace.url)

        port = _find_free_port()
        base_url = f"http://127.0.0.1:{port}"
        server_log = server_tmp / "server.log"
        runner_log = server_tmp / "runner.log"
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
            f"sqlite:///{server_tmp / 'test.db'}",
            "--artifact-location",
            str(artifact_dir),
        ]

        with open(server_log, "w") as log_handle, open(runner_log, "w") as runner_log_handle:
            proc = subprocess.Popen(
                server_command, env=server_env, stdout=log_handle, stderr=subprocess.STDOUT
            )
            runner_proc = subprocess.Popen(
                [sys.executable, "-m", "omnigent.runner._entry"],
                env=runner_env,
                stdout=runner_log_handle,
                stderr=subprocess.STDOUT,
            )
            _wait_healthy(base_url, runner_id, proc, runner_proc, server_log, runner_log)
            yield CodexStack(
                base_url=base_url,
                runner_id=runner_id,
                workspace=workspace,
                tmp_dir=tmp_dir,
                server_log=server_log,
                runner_log=runner_log,
            )
    finally:
        for child in (runner_proc, proc):
            if child is not None and child.poll() is None:
                # Isolate each child's teardown so one failure still runs the rest.
                with suppress(OSError, subprocess.TimeoutExpired):
                    child.send_signal(signal.SIGTERM)
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
        # shutdown() blocks until serve_forever is running, so skip it if the
        # thread never started; server_close still frees the bound socket.
        if serving and workspace is not None and workspace_thread is not None:
            workspace.shutdown()
            workspace_thread.join(timeout=5)
        if workspace is not None:
            workspace.server_close()
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _launch_candidates(model_ids: tuple[str, ...]) -> dict[str, str]:
    """Ids the TUI could plausibly name: the listing plus Omnigent's launch-default preference."""
    return {
        comparable_model_id(model_id): model_id
        for model_id in (*model_ids, *CODEX_LAUNCH_DEFAULT_PREFERENCE)
    }


#: Startup lines that merely name the model a launch could not reach. Matching a
#: candidate on such a line would read a failure banner as a successful launch.
_LAUNCH_ERROR_RE = re.compile(
    r"\b(error|unavailable|failed|not found|invalid|rejected)\b", re.IGNORECASE
)

#: The TUI names the launched model as a ``model: <id>`` banner or an
#: ``<id> <effort> · <cwd>`` footer; prose or an error line that merely mentions a
#: model must not count as a launch.
_LAUNCH_MODEL_RE = re.compile(r"model:\s*(?P<banner>[\w./-]+)|(?P<footer>[\w./-]+)(?:\s+\S+)?\s+·")


def _launched_model(pane_text: str, candidates: dict[str, str]) -> str | None:
    for line in pane_text.splitlines():
        if _LAUNCH_ERROR_RE.search(line):
            continue
        for match in _LAUNCH_MODEL_RE.finditer(line):
            token = match.group("banner") or match.group("footer")
            if token and comparable_model_id(token) in candidates:
                return token
    return None


def test_launched_model_counts_only_a_model_banner_or_footer() -> None:
    """Only the model banner or footer counts as a launch, not an error or prose."""
    candidates = _launch_candidates(_ADVERTISED_MODEL_IDS)
    assert _launched_model("ERROR: model gpt-6-terra unavailable", candidates) is None
    assert _launched_model("fetching catalog for system.ai.gpt-6-terra", candidates) is None
    assert _launched_model("model: system.ai.gpt-6-terra", candidates) == "system.ai.gpt-6-terra"
    assert (
        _launched_model("  system.ai.gpt-6-terra  default · /repo", candidates)
        == "system.ai.gpt-6-terra"
    )


@pytest.fixture
def databricks_codex_gpt6_session(
    request: pytest.FixtureRequest,
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[CodexStack, str]]:
    """A runner-bound, unpinned native Codex session on a GPT-6-advertising workspace."""
    if request.config.getoption("--ui-base-url"):
        pytest.skip("Databricks-discovery native Codex e2e requires an isolated spawned server")
    codex_path = os.environ.get("OMNIGENT_CODEX_PATH") or shutil.which("codex")
    if codex_path is None:
        pytest.skip("codex CLI is required for native Codex e2e")
    if not _codex_cli_supports_mocked_app_server(codex_path):
        pytest.skip("codex CLI >= 0.139.0 is required for mocked app-server e2e")
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for native Codex terminals")

    server_tmp = tmp_path_factory.mktemp("e2e_ui_dbx_codex")
    with dedicated_databricks_codex_stack(_ADVERTISED_MODEL_IDS, server_tmp, codex_path) as stack:
        session_id = _create_native_codex_session(stack.base_url, stack.runner_id)
        try:
            yield stack, session_id
        finally:
            with suppress(httpx.HTTPError):
                httpx.delete(f"{stack.base_url}/v1/sessions/{session_id}", timeout=10.0)


@pytest.mark.timeout(420)
def test_unpinned_databricks_codex_session_launches_newest_advertised_generation(
    request: pytest.FixtureRequest,
    databricks_codex_gpt6_session: tuple[CodexStack, str],
) -> None:
    """An unpinned Databricks Codex launch runs the newest advertised GPT, not an older GPT-5.x."""
    stack, session_id = databricks_codex_gpt6_session
    # Request the page only now so a recording starts at the user's first
    # navigation rather than during server boot.
    page: Page = request.getfixturevalue("page")
    page.goto(f"{stack.base_url}/c/{session_id}")

    # Attaching the Terminal view is what makes the runner spawn Codex for a
    # terminal-first wrapper session; the launch resolves the model under test.
    _open_terminal_view(page)
    _wait_terminal_connected(page)

    # The SPA renders the pane on a WebGL canvas, so read the TUI text from the
    # managed tmux pane instead.
    candidates = _launch_candidates(_ADVERTISED_MODEL_IDS)
    deadline = time.monotonic() + _TUI_BANNER_TIMEOUT_MS / 1000
    pane_text = ""
    launched: str | None = None
    previous: str | None = None
    while time.monotonic() < deadline:
        pane_text = _codex_pane_text(stack.tmp_dir)
        current = _launched_model(pane_text, candidates)
        # Require the same model across two consecutive polls so a transient
        # mid-boot banner never settles the launch model under test; fold the ids
        # so a banner/footer respelling between polls still counts as stable.
        if (
            current is not None
            and previous is not None
            and comparable_model_id(current) == comparable_model_id(previous)
        ):
            launched = current
            # Let the SPA terminal mirror the banner so a recording ends on the outcome.
            page.wait_for_timeout(3_000)
            break
        previous = current
        page.wait_for_timeout(1_000)

    listed = ", ".join(_ADVERTISED_MODEL_IDS)
    assert any(
        path.split("?", 1)[0] == _MODEL_SERVICES_PATH for path in stack.workspace.requests
    ), "the launch never consulted the workspace model-services listing"
    assert launched is not None, (
        f"Codex TUI never painted its launch model; last pane text:\n{pane_text}"
    )
    assert comparable_model_id(launched) == comparable_model_id(_NEWEST_ADVERTISED), (
        f"workspace advertises {listed} but the unpinned native Codex session launched on "
        f"{launched!r} instead of the newest advertised generation {_NEWEST_ADVERTISED!r}; "
        f"pane text:\n{pane_text}"
    )
