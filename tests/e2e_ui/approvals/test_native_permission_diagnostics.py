"""Trace a real Claude permission across a severed poll and a browser verdict.

Only the model's responses and the gateway's idle timeout are controlled.
Claude, its PermissionRequest subprocess, the server, runner, browser card,
and persisted debug-log delivery all run unmodified.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.runner.identity import token_bound_runner_id
from tests._helpers.live_server import find_free_port
from tests.e2e.test_native_terminal_start_error_log_correlation_e2e import _ZeroBusCapture
from tests.e2e_ui.conftest import (
    _BUILD_OUTPUT,
    _create_native_claude_session,
    configure_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view, _send

_REPO = Path(__file__).resolve().parents[3]
_MODEL = "claude-sonnet-4-20250514"
_TOOL_ID = "toolu_permission_diagnostics"
_DONE = "NATIVE_PERMISSION_DIAGNOSTICS_RESUMED"
_CLAUDE = shutil.which("claude")


def _wait_for(probe: Callable[[], Any], description: str, timeout: float = 45) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := probe():
            return result
        time.sleep(0.2)
    raise AssertionError(f"Timed out waiting for {description}")


class _SeverFirstPermissionPoll:
    """A gateway that times out one genuine held poll, then passes retries."""

    def __init__(self, upstream: str) -> None:
        self.requests: list[dict[str, Any]] = []
        self.timed_out = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                permission = self.path.endswith("/hooks/permission-request")
                if permission:
                    owner.requests.append(json.loads(body))
                first_poll = permission and len(owner.requests) == 1
                try:
                    # 12 seconds exceeds the real hook's held-poll floor (10s).
                    with httpx.Client(trust_env=False, timeout=12 if first_poll else 90) as client:
                        result = client.post(
                            upstream + self.path,
                            content=body,
                            headers={"Content-Type": "application/json"},
                        )
                    status, payload = result.status_code, result.content
                except httpx.ReadTimeout:
                    owner.timed_out.set()
                    status, payload = 504, b"gateway idle timeout"
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError):
                    self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@dataclass
class _Rig:
    base_url: str
    session_id: str
    workspace: Path
    capture: _ZeroBusCapture
    proxy: _SeverFirstPermissionPoll
    work: Path

    def rows(self, name: str) -> list[dict[str, Any]]:
        return [
            row
            for row in self.capture.rows()
            if row.get("session_id") == self.session_id and row.get("event_name") == name
        ]


@pytest.fixture
def permission_diagnostics_rig(
    built_spa: None, mock_llm_server_url: str, tmp_path: Path
) -> Iterator[_Rig]:
    """Own the processes and config so the proxy and sink cannot affect other tests."""
    if _CLAUDE is None or shutil.which("tmux") is None:
        pytest.skip("requires real Claude Code and tmux")
    workspace = tmp_path / "workspace"
    config = tmp_path / "config"
    native_home = tmp_path / "home"
    # tmux and native IPC socket paths must fit the Unix 108-byte limit.
    temporary = tempfile.TemporaryDirectory(prefix="opd-")
    temp = Path(temporary.name)
    for directory in (workspace, config, native_home):
        directory.mkdir()
    (native_home / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "projects": {str(workspace): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    (config / "config.yaml").write_text(
        json.dumps(
            {
                "providers": {
                    "mock-claude": {
                        "kind": "key",
                        "default": ["anthropic"],
                        "anthropic": {
                            "base_url": mock_llm_server_url,
                            "api_key": "mock-key",
                            "models": {"default": _MODEL},
                        },
                    }
                }
            }
        )
    )
    token = uuid.uuid4().hex
    runner_id = token_bound_runner_id(token)
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    capture = _ZeroBusCapture()
    proxy = _SeverFirstPermissionPoll(base_url)
    env = {
        **{
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}
        },
        "HOME": str(native_home),
        "TMPDIR": str(temp),
        "OMNIGENT_CONFIG_HOME": str(config),
        "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
        "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "OMNIGENT_CLAUDE_PATH": str(_CLAUDE),
        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "ANTHROPIC_AUTH_TOKEN": "mock-key",
        "ANTHROPIC_BASE_URL": mock_llm_server_url,
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
        "PYTHONPATH": str(_REPO),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "OMNIGENT_DEBUG_LOG_CLIENT_ID": "e2e-client",
        "OMNIGENT_DEBUG_LOG_CLIENT_SECRET": "e2e-secret",
        "OMNIGENT_DEBUG_LOG_WORKSPACE_URL": capture.base_url,
        "OMNIGENT_DEBUG_LOG_ENDPOINT": capture.insert_url,
    }
    processes: list[subprocess.Popen[bytes]] = []
    session_id = ""
    with (
        (tmp_path / "server.log").open("w") as server_log,
        (tmp_path / "runner.log").open("w") as runner_log,
        httpx.Client(base_url=base_url, timeout=10, trust_env=False) as client,
    ):
        try:
            processes.append(
                subprocess.Popen(
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
                        f"sqlite:///{tmp_path / 'test.db'}",
                        "--artifact-location",
                        str(tmp_path / "artifacts"),
                    ],
                    cwd=_REPO,
                    env={**env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": token},
                    stdout=server_log,
                    stderr=subprocess.STDOUT,
                )
            )
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-m", "omnigent.runner._entry"],
                    cwd=_REPO,
                    env={
                        **env,
                        "OMNIGENT_RUNNER_ID": runner_id,
                        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": token,
                        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                        "RUNNER_SERVER_URL": base_url,
                    },
                    stdout=runner_log,
                    stderr=subprocess.STDOUT,
                )
            )

            def ready() -> bool:
                assert all(process.poll() is None for process in processes), (
                    "server or runner exited; inspect " + str(tmp_path)
                )
                with contextlib.suppress(httpx.HTTPError):
                    response = client.get(f"/v1/runners/{runner_id}/status", timeout=2)
                    return response.status_code == 200 and response.json().get("online")
                return False

            _wait_for(ready, "server and runner", timeout=60)
            session_id = _create_native_claude_session(
                base_url, runner_id, terminal_launch_args=["--permission-mode", "default"]
            )

            def hook_config() -> Path | None:
                for path in temp.rglob("permission_hook.json"):
                    config_path = path.with_name("bridge.json")
                    if config_path.is_file():
                        values = json.loads(config_path.read_text())
                        if values.get("active_session_id") == session_id:
                            return path
                return None

            path = _wait_for(
                hook_config, "Claude's real permission hook configuration", timeout=90
            )
            values = json.loads(path.read_text())
            values["ap_server_url"] = proxy.url
            replacement = path.with_suffix(".replacement")
            replacement.write_text(json.dumps(values))
            replacement.chmod(0o600)
            replacement.replace(path)
            yield _Rig(base_url, session_id, workspace, capture, proxy, tmp_path)
        finally:
            if session_id:
                with contextlib.suppress(httpx.HTTPError):
                    client.delete(f"/v1/sessions/{session_id}")
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            (tmp_path / "debug-rows.json").write_text(json.dumps(capture.rows(), indent=2))
            proxy.close()
            capture.close()
            temporary.cleanup()


@pytest.mark.timeout(240)
def test_real_claude_permission_diagnostics_survive_gateway_timeout(
    page: Page,
    permission_diagnostics_rig: _Rig,
    mock_llm_server_url: str,
) -> None:
    """Join the real blocked tool, browser verdict, retry, and resumed native work."""
    rig = permission_diagnostics_rig
    marker = rig.workspace / "approved.txt"
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": _TOOL_ID,
                        "name": "Write",
                        "arguments": json.dumps({"file_path": str(marker), "content": "approved"}),
                    }
                ]
            },
            {"text": _DONE},
        ],
        key=_MODEL,
    )
    for index, title_marker in enumerate(("<session>", "<user_message>")):
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": "Permission diagnostics"}],
            key=f"permission-diagnostics-title-{index}",
            match=title_marker,
        )
    page.goto(f"{rig.base_url}/c/{rig.session_id}")
    _ensure_chat_view(page)
    _send(page, "Write approved.txt containing approved, then confirm completion.")
    card = page.get_by_test_id("approval-card").filter(has_text="Write").first
    expect(card).to_be_visible(timeout=90_000)
    expect(card).to_have_attribute("data-state", "pending")
    assert not marker.exists(), "Write must still be blocked on Claude's real permission hook"

    _wait_for(lambda: rig.proxy.timed_out.is_set(), "the gateway idle timeout")
    _wait_for(lambda: len(rig.proxy.requests) >= 2, "the real hook to retry its severed poll")
    ids = {request["_omnigent_elicitation_id"] for request in rig.proxy.requests}
    assert len(ids) == 1, "reattachment must retain the approval identity"
    elicitation_id = ids.pop()
    assert rig.proxy.requests[0]["tool_name"] == "Write"
    assert rig.proxy.requests[0]["permission_mode"] == "default"
    expect(card).to_have_attribute("data-state", "pending")
    assert not marker.exists(), "the gateway timeout must never become permission to execute"
    card.get_by_role("button", name="Approve", exact=True).click()
    _wait_for(marker.exists, "Claude to execute Write after the browser verdict")
    assert marker.read_text() == "approved"
    expect(page.get_by_test_id("message-bubble").filter(has_text=_DONE).first).to_be_visible(
        timeout=60_000
    )

    expected = {
        "approval_wait_started",
        "approval_published",
        "approval_wait_ended",
        "approval_verdict_received",
        "approval_verdict_applied",
        "approval_runner_delivery",
        "approval_runner_received",
        "approval_hook_attempt",
        "approval_hook_response",
        "approval_hook_retry",
        "browser_approval_received",
        "browser_approval_applied",
        "browser_approval_rendered",
        "browser_approval_visibility",
        "browser_approval_verdict_submitted",
        "browser_approval_verdict_request_completed",
    }
    _wait_for(
        lambda: all(rig.rows(name) for name in expected),
        "the complete diagnostic chain in the real debug-log sink; missing "
        + ", ".join(sorted(name for name in expected if not rig.rows(name))),
        timeout=30,
    )
    for name in expected:
        assert any(
            row["attributes"].get("elicitation_id") == elicitation_id for row in rig.rows(name)
        ), name
    waits = rig.rows("approval_wait_started")
    assert len({row["attributes"]["wait_attempt_id"] for row in waits}) >= 2
    outcomes = {row["attributes"]["outcome"] for row in rig.rows("approval_wait_ended")}
    assert {"disconnect", "web_verdict"} <= outcomes
    assert all(row["attributes"]["tool_name"] == "Write" for row in waits)
    assert all(row["attributes"]["permission_mode"] == "default" for row in waits)
    assert all(row["attributes"]["native_reason"] == "not_exposed" for row in waits)
    assert any(
        row["attributes"].get("reason") == "gateway_sever"
        for row in rig.rows("approval_hook_retry")
    )
    assert any(
        row["attributes"].get("response_kind") == "allow"
        for row in rig.rows("approval_hook_response")
    )
    assert any(
        row["attributes"].get("actionable") == "True"
        and row["attributes"].get("in_view") == "True"
        for row in rig.rows("browser_approval_visibility")
    )
