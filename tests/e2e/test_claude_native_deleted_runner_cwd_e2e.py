"""Real native web turns survive an unlinked runner cwd with a live workspace.

Only Anthropic responses are scripted. The server, runner, Claude CLI, tmux,
hooks, MCP relay, and workspace file read all run normally.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.claude_native.bridge import (
    BRIDGE_ID_LABEL_KEY,
    bridge_dir_for_bridge_id,
)
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
from tests.e2e.test_host_claude_native_e2e import _poll_for_assistant_marker
from tests.e2e.test_host_claude_native_fork_e2e import _send_user_message
from tests.server.integration.mock_llm_server import (
    anthropic_sse_text_response,
    anthropic_sse_tool_call_response,
)

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "linux" or not shutil.which("claude") or not shutil.which("tmux"),
        reason="requires Linux /proc, the real Claude CLI, and tmux",
    ),
    pytest.mark.timeout(240),
]

_REPO = Path(__file__).resolve().parents[2]
_READ_ID = "toolu_deleted_cwd_read"


class _Gateway:
    """Script actual tool-bearing turns independently of Claude's title requests."""

    def __init__(self) -> None:
        self.reply = "ready"
        self.read_path: str | None = None
        self.results: list[dict[str, Any]] = []

    def response(self, body: dict[str, Any]) -> str:
        model = body.get("model", "claude-sonnet-4-6")
        if not body.get("tools"):
            return anthropic_sse_text_response("Local test", model=model)
        results = [
            block
            for message in body.get("messages", [])
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if block.get("type") == "tool_result"
        ]
        reads = [result for result in results if result.get("tool_use_id") == _READ_ID]
        if reads:
            self.results = reads
        if self.read_path is None or reads:
            return anthropic_sse_text_response(self.reply, model=model)
        searched = any(result.get("tool_use_id") == "toolu_cwd_search" for result in results)
        return anthropic_sse_tool_call_response(
            [
                {
                    "call_id": _READ_ID if searched else "toolu_cwd_search",
                    "name": "mcp__omnigent__sys_os_read" if searched else "ToolSearch",
                    "arguments": json.dumps(
                        {"path": self.read_path}
                        if searched
                        else {"query": "select:mcp__omnigent__sys_os_read", "max_results": 1}
                    ),
                }
            ],
            model=model,
        )


@pytest.fixture
def gateway() -> Iterator[tuple[_Gateway, str]]:
    scripted = _Gateway()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.split("?", 1)[0] == "/v1/messages":
                if body.get("stream"):
                    payload = scripted.response(body).encode()
                    content_type = "text/event-stream"
                else:
                    payload = json.dumps(
                        {
                            "id": "msg_auxiliary",
                            "type": "message",
                            "role": "assistant",
                            "model": body.get("model"),
                            "content": [{"type": "text", "text": "Local test"}],
                            "stop_reason": "end_turn",
                            "stop_sequence": None,
                            "usage": {"input_tokens": 10, "output_tokens": 2},
                        }
                    ).encode()
                    content_type = "application/json"
            else:
                payload, content_type = b'{"input_tokens":10}', "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield scripted, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _wait(check: Callable[[], object], description: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.25)
    raise AssertionError(f"Timed out waiting for {description}")


@pytest.mark.parametrize("warmed_web_cache", [True, False], ids=["warm", "cold"])
def test_claude_web_turn_survives_deleted_runner_cwd(
    tmp_path: Path,
    resume_test_server: str,
    gateway: tuple[_Gateway, str],
    warmed_web_cache: bool,
) -> None:
    scripted, gateway_url = gateway
    workspace = tmp_path / "workspace"
    runner_cwd = tmp_path / "runner-cwd"
    config_dir = tmp_path / "claude-config"
    for directory in (workspace, runner_cwd, config_dir):
        directory.mkdir()
    sentinel = secrets.token_hex(16)
    (workspace / "sentinel.txt").write_text(sentinel)
    (config_dir / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "projects": {str(workspace): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    (config_dir / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["mcp__omnigent__sys_os_read"]}})
    )
    token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(token)
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "USER", "LANG", "TMPDIR")
        if key in os.environ
    }
    env.update(
        PYTHONPATH=os.pathsep.join(
            str(path) for path in (_REPO, _REPO / "sdks/python-client", _REPO / "sdks/ui")
        ),
        OMNIGENT_CONFIG_HOME=str(tmp_path / "omnigent-config"),
        OMNIGENT_DATA_DIR=str(tmp_path / "omnigent-data"),
        OMNIGENT_PROCESS_LOG_FILE=str(tmp_path / "runner.log"),
        OMNIGENT_AUTH_PROVIDER="header",
        OMNIGENT_LOCAL_SINGLE_USER="1",
        OMNIGENT_DISABLE_CATALOG_LOOKUP="1",
        OMNIGENT_RUNNER_ID=runner_id,
        OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN=token,
        OMNIGENT_RUNNER_PARENT_PID=str(os.getpid()),
        OMNIGENT_RUNNER_WORKSPACE=str(workspace),
        RUNNER_SERVER_URL=resume_test_server,
        CLAUDE_CONFIG_DIR=str(config_dir),
        ANTHROPIC_AUTH_TOKEN="local-cwd-test",
        ANTHROPIC_BASE_URL=gateway_url,
        CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST="1",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        CLAUDE_CODE_DISABLE_CLAUDE_MDS="1",
        DISABLE_AUTOUPDATER="1",
        DISABLE_TELEMETRY="1",
        DISABLE_ERROR_REPORTING="1",
        NO_PROXY="127.0.0.1,localhost",
        TERM="xterm-256color",
    )
    log_path = tmp_path / "runner.log"
    print(f"Native cwd regression logs: {tmp_path}", flush=True)
    with (
        log_path.open("w") as log,
        httpx.Client(
            base_url=resume_test_server,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            timeout=90,
            trust_env=False,
        ) as client,
    ):
        runner = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            cwd=runner_cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        tmux: list[str] | None = None
        target = ""
        try:
            _wait(
                lambda: client.get(f"/v1/runners/{runner_id}/status").json().get("online"),
                "runner tunnel",
            )
            spec = b"""name: deleted-runner-cwd
prompt: Reply briefly and use the requested tools.
executor:
  harness: claude-native
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""
            bundle = io.BytesIO()
            with tarfile.open(fileobj=bundle, mode="w:gz") as archive:
                info = tarfile.TarInfo("deleted-runner-cwd.yaml")
                info.size = len(spec)
                archive.addfile(info, io.BytesIO(spec))
            response = client.post(
                "/v1/sessions",
                data={
                    "metadata": json.dumps(
                        {
                            "workspace": str(workspace),
                            "labels": {"omnigent.wrapper": "claude-code-native-ui"},
                        }
                    )
                },
                files={"bundle": ("agent.tar.gz", bundle.getvalue(), "application/gzip")},
            )
            response.raise_for_status()
            session_id = response.json()["session_id"]
            client.patch(
                f"/v1/sessions/{session_id}", json={"runner_id": runner_id}
            ).raise_for_status()
            client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "ensure_native_terminal": True},
            ).raise_for_status()
            snapshot = client.get(f"/v1/sessions/{session_id}").json()
            bridge_dir = bridge_dir_for_bridge_id(
                snapshot.get("labels", {}).get(BRIDGE_ID_LABEL_KEY) or session_id
            )
            _wait(lambda: (bridge_dir / "tmux.json").exists(), "Claude tmux target")
            pane = json.loads((bridge_dir / "tmux.json").read_text())
            tmux = ["tmux", "-S", pane["socket_path"]]
            target = pane["tmux_target"]

            def send_direct(text: str) -> None:
                subprocess.run(
                    [*tmux, "load-buffer", "-"], input=text.encode(), check=True, timeout=5
                )
                subprocess.run(
                    [*tmux, "paste-buffer", "-p", "-d", "-t", target], check=True, timeout=5
                )
                time.sleep(0.3)
                subprocess.run([*tmux, "send-keys", "-t", target, "Enter"], check=True, timeout=5)

            def wait_reply(marker: str) -> None:
                _poll_for_assistant_marker(
                    client, session_id=session_id, marker=marker, timeout=60
                )
                _wait(
                    lambda: (
                        client.get(f"/v1/sessions/{session_id}").json().get("status") == "idle"
                    ),
                    "idle native session",
                )

            _wait(
                lambda: (
                    "❯"
                    in subprocess.check_output(
                        [*tmux, "capture-pane", "-p", "-t", target], text=True, timeout=5
                    )
                ),
                "Claude composer",
            )
            baseline = "BASELINE_" + secrets.token_hex(8)
            scripted.reply = baseline
            if warmed_web_cache:
                _send_user_message(client, session_id=session_id, text=baseline)
            else:
                send_direct(baseline)
            wait_reply(baseline)
            print("Baseline native reply received", flush=True)
            claude_pid = int(
                subprocess.check_output(
                    [*tmux, "display-message", "-p", "-t", target, "#{pane_pid}"],
                    text=True,
                    timeout=5,
                )
            )
            assert Path(f"/proc/{claude_pid}/comm").read_text().strip() == "claude"
            assert os.readlink(f"/proc/{claude_pid}/cwd") == str(workspace)
            assert os.readlink(f"/proc/{runner.pid}/cwd") == str(runner_cwd)
            old_inode = runner_cwd.stat().st_ino
            runner_cwd.rmdir()
            runner_cwd.mkdir()
            assert runner_cwd.stat().st_ino != old_inode
            assert os.readlink(f"/proc/{runner.pid}/cwd") == f"{runner_cwd} (deleted)"
            assert client.get(f"/v1/sessions/{session_id}").json()["workspace"] == str(workspace)
            assert workspace.is_dir()
            print(f"Runner {runner.pid} cwd deleted; Claude {claude_pid} cwd intact", flush=True)

            direct = "DIRECT_" + secrets.token_hex(8)
            scripted.reply = direct
            send_direct(direct)
            wait_reply(direct)
            print("Post-unlink direct native reply received", flush=True)

            marker = "WEB_READ_" + secrets.token_hex(8)
            scripted.reply = marker
            scripted.read_path = str(workspace / "sentinel.txt")
            _send_user_message(client, session_id=session_id, text=marker)
            wait_reply(marker)
            results = scripted.results
            assert results and all(not result.get("is_error") for result in results), results
            assert sentinel in json.dumps(results), results
            assert os.readlink(f"/proc/{runner.pid}/cwd").endswith(" (deleted)")
            assert os.readlink(f"/proc/{claude_pid}/cwd") == str(workspace)
            print("Post-unlink web reply and real MCP sentinel read verified", flush=True)
        finally:
            if tmux is not None:
                screen = subprocess.run(
                    [*tmux, "capture-pane", "-p", "-S", "-200", "-t", target],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                (tmp_path / "pane.txt").write_text(screen.stdout + screen.stderr)
            runner.terminate()
            try:
                runner.wait(timeout=15)
            except subprocess.TimeoutExpired:
                runner.kill()
                runner.wait(timeout=5)
            if tmux is not None:
                subprocess.run([*tmux, "kill-server"], check=False, capture_output=True, timeout=5)
