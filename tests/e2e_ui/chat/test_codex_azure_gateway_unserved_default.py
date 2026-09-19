"""E2E: a Databricks codex launch must not default to a model the workspace won't serve.

Azure Databricks workspaces list GPT models in the Unity Catalog
model-services API that the AI Gateway's codex Responses route does not
actually serve — posting to them returns ``404 RESOURCE_DOES_NOT_EXIST`` —
while a codex-compatible model (``system.ai.glm-5-2``) is genuinely served.
Launching codex on such a profile with no explicit model must still yield a
working first turn: the launch default has to resolve to a served model
instead of pinning an advertised-but-unserved GPT id and dying on the 404.

Journey (through real product code — omnigent server, runner, codex-native
launch resolution, and the real codex CLI): configure a Databricks profile
for such a workspace, create a codex-native session with no explicit model,
send the first chat message, and require the assistant's reply. Only the
workspace itself is faked, answering exactly as the Azure workspace does.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _codex_cli_supports_mocked_app_server,
    _create_native_codex_session,
    _find_free_port,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

# What the workspace's Unity Catalog model-services listing advertises. On
# Azure the GPT ids are listed but the codex route rejects them with 404;
# only the GLM id is genuinely served. The claude id is not codex-compatible.
_ADVERTISED_MODELS = (
    "system.ai.gpt-5-6-sol",
    "system.ai.gpt-5-5",
    "system.ai.glm-5-2",
    "system.ai.claude-fable-5",
)
_SERVED_CODEX_MODEL = "system.ai.glm-5-2"
_REPLY_TEXT = "AZURE_GATEWAY_REPLY_OK"

_RIG_READY_TIMEOUT_S = 120.0
_TURN_OUTCOME_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 2.0

# Loopback must bypass any forced egress proxy for this process (fixture
# helpers use ambient httpx) and for the spawned server/runner/codex tree.
for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))

_client = httpx.Client(trust_env=False)


def _bare_model_id(model: str) -> str:
    """Normalize a model id across catalog spellings for served-set membership."""
    lowered = model.strip().lower()
    for prefix in ("system.ai.", "databricks-"):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix) :]
            break
    return lowered.replace(".", "-")


def _is_served(model: str) -> bool:
    return _bare_model_id(model) == _bare_model_id(_SERVED_CODEX_MODEL)


def _response_object() -> dict[str, Any]:
    """A completed Responses-API response carrying the served model's reply."""
    message = {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": _REPLY_TEXT}],
    }
    return {
        "id": "resp-1",
        "object": "response",
        "status": "completed",
        "output": [message],
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": None,
            "output_tokens": 1,
            "output_tokens_details": None,
            "total_tokens": 2,
        },
    }


def _sse_reply() -> bytes:
    """Minimal Responses SSE stream: created -> assistant message -> completed."""
    completed = _response_object()
    events: list[tuple[str, dict[str, Any]]] = [
        ("response.created", {"response": {"id": completed["id"]}}),
        ("response.output_item.done", {"item": completed["output"][0]}),
        ("response.completed", {"response": completed}),
    ]
    return "".join(
        f"event: {name}\ndata: {json.dumps({'type': name, **payload})}\n\n"
        for name, payload in events
    ).encode()


class _AzureishWorkspace(http.server.ThreadingHTTPServer):
    """Loopback stand-in for an Azure Databricks workspace + AI Gateway.

    The Unity Catalog model-services listing advertises GPT ids; the codex
    Responses route serves only ``_SERVED_CODEX_MODEL`` and answers anything
    else with the gateway's real-world ``404 RESOURCE_DOES_NOT_EXIST`` shape.
    """

    def __init__(self) -> None:
        #: Every /responses POST seen: ``(model, status_answered)``.
        self.responses_seen: list[tuple[str, int]] = []
        super().__init__(("127.0.0.1", 0), _AzureishWorkspaceHandler)

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _AzureishWorkspaceHandler(http.server.BaseHTTPRequestHandler):
    server: _AzureishWorkspace
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reply_not_found(self, name: str) -> None:
        body = json.dumps(
            {
                "error_code": "RESOURCE_DOES_NOT_EXIST",
                "message": f"Endpoint with name '{name}' does not exist.",
            }
        ).encode()
        self._reply(404, "application/json", body)

    def do_GET(self) -> None:
        if self.path.startswith("/api/2.1/unity-catalog/model-services"):
            payload = {
                "model_services": [
                    {"name": f"model-services/{model}"} for model in _ADVERTISED_MODELS
                ]
            }
            self._reply(200, "application/json", json.dumps(payload).encode())
            return
        if self.path.startswith("/ai-gateway/codex/v1/models"):
            payload = {
                "object": "list",
                "data": [{"id": _SERVED_CODEX_MODEL, "object": "model"}],
            }
            self._reply(200, "application/json", json.dumps(payload).encode())
            return
        self._reply_not_found(self.path)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not self.path.startswith("/ai-gateway/codex/v1/responses"):
            self._reply_not_found(self.path)
            return
        try:
            body: dict[str, Any] = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        model = str(body.get("model") or "")
        served = _is_served(model)
        self.server.responses_seen.append((model, 200 if served else 404))
        if not served:
            self._reply_not_found(model)
            return
        if body.get("stream") is False:
            self._reply(200, "application/json", json.dumps(_response_object()).encode())
            return
        self._reply(200, "text/event-stream", _sse_reply())

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        pass


def _stage_azure_profile(work: Path, host: str) -> tuple[Path, Path]:
    """Stage the user's Databricks state: a profile for the workspace plus a
    ``databricks`` CLI on PATH whose token mint always succeeds.

    :returns: ``(bin dir, home dir)`` for the spawned processes' env.
    """
    bindir = work / "bin"
    bindir.mkdir()
    fake_cli = bindir / "databricks"
    fake_cli.write_text(
        "#!/bin/sh\n"
        'case "$*" in *--help*) echo "usage"; exit 0;; esac\n'
        'echo \'{"access_token":"fake-azure-token"}\'\n'
    )
    fake_cli.chmod(0o755)
    home = work / "home"
    home.mkdir()
    (home / ".databrickscfg").write_text(f"[azure]\nhost = {host}\ntoken = fake-azure-pat\n")
    return bindir, home


@dataclass(frozen=True)
class _AzureCodexRig:
    """Live rig handles for one test run."""

    base_url: str
    session_id: str
    workspace: _AzureishWorkspace
    server_log: Path
    runner_log: Path


@pytest.fixture
def azure_codex_databricks_session(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[_AzureCodexRig]:
    """Server + runner configured with a Databricks profile for the fake
    Azure-behaving workspace, and a codex-native session.

    The session pins no model (the reported journey) unless the test
    indirect-parametrizes one.
    """
    if request.config.getoption("--ui-base-url"):
        pytest.skip("this journey requires an isolated spawned server")
    codex_path = shutil.which("codex")
    if codex_path is None:
        pytest.skip("codex CLI is required for the Databricks codex launch journey")
    if not _codex_cli_supports_mocked_app_server(codex_path):
        pytest.skip("codex CLI >= 0.139.0 is required for mocked app-server e2e")

    workspace = _AzureishWorkspace()
    threading.Thread(target=workspace.serve_forever, daemon=True).start()

    work = tmp_path_factory.mktemp("azure_codex_gateway")
    bindir, home_dir = _stage_azure_profile(work, workspace.host)
    config_home = work / "config-home"
    source_codex_home = work / "source-codex-home"
    state_dir = work / "codex-native-state"
    artifacts = work / "artifacts"
    for path in (config_home, source_codex_home, state_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        "providers:\n"
        "  azure-workspace:\n"
        "    kind: databricks\n"
        "    profile: azure\n"
        "    default: openai\n",
        encoding="utf-8",
    )

    from omnigent.runner.identity import token_bound_runner_id

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    shared_env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "CODEX_HOME": str(source_codex_home),
        "HOME": str(home_dir),
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
    }
    # Ambient workspace credentials would override the staged profile.
    for var in (
        "DATABRICKS_HOST",
        "DATABRICKS_TOKEN",
        "DATABRICKS_BEARER",
        "DATABRICKS_CONFIG_PROFILE",
        "DATABRICKS_CONFIG_FILE",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
    ):
        shared_env.pop(var, None)
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

        deadline = time.monotonic() + _RIG_READY_TIMEOUT_S
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
                "azure codex rig did not come online within "
                f"{_RIG_READY_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )

        fixture_param = getattr(request, "param", None)
        pinned_model = fixture_param if isinstance(fixture_param, str) else None
        session_id = _create_native_codex_session(base_url, runner_id, model=pinned_model)
        yield _AzureCodexRig(
            base_url=base_url,
            session_id=session_id,
            workspace=workspace,
            server_log=server_log,
            runner_log=runner_log,
        )
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
        workspace.shutdown()
        workspace.server_close()


def _wait_turn_outcome(rig: _AzureCodexRig) -> tuple[list[str], bool]:
    """Poll the transcript until the first turn reaches a terminal outcome.

    :returns: ``(error item messages, assistant replied)``.
    """
    errors: list[str] = []
    assistant_replied = False
    deadline = time.monotonic() + _TURN_OUTCOME_TIMEOUT_S
    while time.monotonic() < deadline:
        items = _client.get(
            f"{rig.base_url}/v1/sessions/{rig.session_id}/items?limit=50", timeout=10.0
        )
        if items.status_code == 200:
            data = items.json()["data"]
            errors = [str(item.get("message", "")) for item in data if item.get("type") == "error"]
            assistant_replied = any(
                item.get("type") == "message" and item.get("role") == "assistant" for item in data
            )
        if errors or assistant_replied:
            break
        time.sleep(_POLL_INTERVAL_S)
    return errors, assistant_replied


@pytest.mark.timeout(600)
def test_codex_databricks_default_model_survives_unserved_listing(
    page: Page,
    azure_codex_databricks_session: _AzureCodexRig,
) -> None:
    """The first message must complete on a served model, not die on the 404."""
    rig = azure_codex_databricks_session
    page.goto(f"{rig.base_url}/c/{rig.session_id}")

    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    _send(page, "hello?")
    errors, assistant_replied = _wait_turn_outcome(rig)
    # Show the TUI's outcome (where the ucode user sees the turn's fate)
    # before asserting, so a failure leaves the terminal state on screen.
    _open_terminal_view(page)
    page.wait_for_timeout(3_000)

    gateway_404_errors = [
        message for message in errors if "RESOURCE_DOES_NOT_EXIST" in message or "404" in message
    ]
    assert not gateway_404_errors, (
        "first codex turn died on the gateway's 404: the launch pinned a model the "
        "workspace does not serve (models codex posted, with the status each got: "
        f"{rig.workspace.responses_seen}); error item: {gateway_404_errors[0][:600]}"
    )
    assert assistant_replied, (
        "first codex turn reached no assistant reply within "
        f"{_TURN_OUTCOME_TIMEOUT_S:.0f}s (errors: {[e[:200] for e in errors]}; "
        f"models codex posted: {rig.workspace.responses_seen})\n"
        f"runner log tail:\n{rig.runner_log.read_text()[-1500:]}"
    )
    _ensure_chat_view(page)
    expect(page.locator(_ASSISTANT, has_text=_REPLY_TEXT).first).to_be_visible(timeout=30_000)
    served_posts = [model for model, status in rig.workspace.responses_seen if status == 200]
    assert served_posts, (
        "no codex request ever reached a model the workspace serves: "
        f"{rig.workspace.responses_seen}"
    )
