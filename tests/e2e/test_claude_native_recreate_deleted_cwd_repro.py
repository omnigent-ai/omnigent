"""Native Claude terminal creation must use the session workspace when the
runner's launch directory has been deleted.

With the runner process alive but its launch cwd removed, auto-creating the
native Claude terminal must still succeed from the configured session workspace
instead of failing to start.

Unlike ``test_claude_native_deleted_runner_cwd_e2e`` (which creates the terminal
BEFORE unlinking the cwd, so Claude keeps the live workspace cwd), here the cwd
is removed BEFORE the terminal is ever created, so the auto-create itself runs
against the dead cwd. Only Anthropic responses are scripted; the server, runner,
Claude CLI and tmux all run normally.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.claude_native.bridge import (
    BRIDGE_ID_LABEL_KEY,
    bridge_dir_for_bridge_id,
)
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
from tests._helpers.session import bundle_files, post_session_bundle
from tests.server.integration.mock_llm_server import anthropic_sse_text_response

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "linux" or not shutil.which("claude") or not shutil.which("tmux"),
        reason="requires Linux /proc, the real Claude CLI, and tmux",
    ),
    pytest.mark.timeout(240),
]

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def gateway() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.split("?", 1)[0] == "/v1/messages" and body.get("stream"):
                payload = anthropic_sse_text_response(
                    "Local test", model=body.get("model", "claude-sonnet-4-6")
                ).encode()
                content_type = "text/event-stream"
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
        yield f"http://127.0.0.1:{server.server_port}"
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


def test_claude_native_terminal_recreates_after_deleted_runner_cwd(
    tmp_path: Path,
    resume_test_server: str,
    gateway: str,
) -> None:
    workspace = tmp_path / "workspace"
    runner_cwd = tmp_path / "runner-cwd"
    config_dir = tmp_path / "claude-config"
    for directory in (workspace, runner_cwd, config_dir):
        directory.mkdir()
    (workspace / "sentinel.txt").write_text(secrets.token_hex(16))
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
        ANTHROPIC_BASE_URL=gateway,
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
    print(f"Native recreate-deleted-cwd repro logs: {tmp_path}", flush=True)
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
        tmux_socket: str | None = None
        bridge_dir: Path | None = None
        try:
            _wait(
                lambda: client.get(f"/v1/runners/{runner_id}/status").json().get("online"),
                "runner tunnel",
            )
            spec = b"""name: recreate-deleted-runner-cwd
prompt: Reply briefly and use the requested tools.
executor:
  harness: claude-native
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""
            bundle_bytes = bundle_files({"recreate-deleted-runner-cwd.yaml": spec})
            response = post_session_bundle(
                client.post,
                "/v1/sessions",
                bundle_bytes,
                metadata={
                    "workspace": str(workspace),
                    "labels": {"omnigent.wrapper": "claude-code-native-ui"},
                },
            )
            response.raise_for_status()
            session_id = response.json()["session_id"]
            print(f"session_id={session_id}", flush=True)

            # Remove the runner's launch dir while the runner stays alive. The
            # process cwd is now gone, so os.getcwd()/Path.cwd() raise ENOENT.
            old_inode = runner_cwd.stat().st_ino
            runner_cwd.rmdir()
            runner_cwd.mkdir()
            assert runner_cwd.stat().st_ino != old_inode
            assert os.readlink(f"/proc/{runner.pid}/cwd") == f"{runner_cwd} (deleted)"
            # The authoritative workspace is still present and configured.
            assert workspace.is_dir()
            assert client.get(f"/v1/sessions/{session_id}").json()["workspace"] == str(workspace)
            print(f"Runner {runner.pid} launch cwd removed; workspace intact", flush=True)

            client.patch(
                f"/v1/sessions/{session_id}", json={"runner_id": runner_id}
            ).raise_for_status()

            ensure_resp = client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "ensure_native_terminal": True},
            )
            print(
                f"ensure terminal -> HTTP {ensure_resp.status_code}: {ensure_resp.text}",
                flush=True,
            )
            assert ensure_resp.status_code == 200, (
                "native Claude terminal failed to start on a deleted runner cwd despite a "
                f"valid configured workspace {workspace}: "
                f"HTTP {ensure_resp.status_code} {ensure_resp.text}"
            )
            snapshot = client.get(f"/v1/sessions/{session_id}").json()
            bridge_dir = bridge_dir_for_bridge_id(
                snapshot.get("labels", {}).get(BRIDGE_ID_LABEL_KEY) or session_id
            )
            _wait(lambda: (bridge_dir / "tmux.json").exists(), "Claude tmux target")
            tmux_socket = json.loads((bridge_dir / "tmux.json").read_text())["socket_path"]
        finally:
            try:
                print("=== runner.log tail ===", flush=True)
                print("\n".join(log_path.read_text().splitlines()[-80:]), flush=True)
            except OSError:
                pass
            runner.terminate()
            try:
                runner.wait(timeout=15)
            except subprocess.TimeoutExpired:
                runner.kill()
                runner.wait(timeout=5)
            if tmux_socket is None and bridge_dir is not None:
                # The wait for tmux.json may have timed out after the server
                # was already started; recover its socket so it isn't leaked.
                with contextlib.suppress(OSError, ValueError, KeyError):
                    tmux_socket = json.loads((bridge_dir / "tmux.json").read_text())["socket_path"]
            if tmux_socket is not None:
                subprocess.run(
                    ["tmux", "-S", tmux_socket, "kill-server"],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
