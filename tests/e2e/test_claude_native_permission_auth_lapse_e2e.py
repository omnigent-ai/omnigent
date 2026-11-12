"""A held Claude permission prompt returns to the web after its bearer lapses.

Real server (accounts auth), runner, Claude CLI, tmux, and hooks. Arranged:
scripted Anthropic responses, a 60s runner-token TTL, a server restart to cut
the held poll, and a sign-in stored the way ``omnigent login`` stores it.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
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
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable
from tests._helpers.live_server import find_free_port
from tests._helpers.session import bundle_files, post_session_bundle
from tests.server.integration.mock_llm_server import (
    anthropic_sse_text_response,
    anthropic_sse_tool_call_response,
)

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "linux" or not shutil.which("claude") or not shutil.which("tmux"),
        reason="requires Linux, the real Claude CLI, and tmux",
    ),
    pytest.mark.timeout(420),
]

_REPO = Path(__file__).resolve().parents[2]
_READ_ID = "toolu_auth_lapse_read"
_OWNER = "alice"
_OWNER_PASSWORD = "e2e-owner-password-1234"
_COOKIE_SECRET_HEX = "b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2"
# Short enough to lapse during the test, long enough for setup to finish.
_RUNNER_TOKEN_TTL_S = 60
# Patch the server's runner-token TTL, then run the normal CLI entry point.
_SERVER_BOOT = (
    "import sys\n"
    "import omnigent.server.routes.runner_tunnel as tunnel\n"
    "tunnel._MANAGED_RUNNER_TOKEN_TTL_S = int(sys.argv.pop(1))\n"
    "from omnigent.cli import main\n"
    "sys.argv[0] = 'omnigent'\n"
    "main()\n"
)


class _Gateway:
    """Request one Read of a file outside the workspace, then reply."""

    def __init__(self) -> None:
        self.read_path: str | None = None
        self.reply = "ready"
        self.results: list[dict[str, Any]] = []

    def response(self, body: dict[str, Any]) -> str:
        model = body.get("model", "claude-sonnet-4-6")
        if not body.get("tools"):
            return anthropic_sse_text_response("Local test", model=model)
        reads = [
            block
            for message in body.get("messages", [])
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if block.get("type") == "tool_result" and block.get("tool_use_id") == _READ_ID
        ]
        if reads:
            self.results = reads
        if self.read_path is None or reads:
            return anthropic_sse_text_response(self.reply, model=model)
        return anthropic_sse_tool_call_response(
            [
                {
                    "call_id": _READ_ID,
                    "name": "Read",
                    "arguments": json.dumps({"file_path": self.read_path, "limit": 3}),
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


def _wait(check: Callable[[], Any], description: str, timeout: float = 60) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.5)
    raise AssertionError(f"Timed out waiting for {description}")


class _AccountsServer:
    """A real accounts-auth ``omnigent server`` that can be restarted in place."""

    def __init__(self, tmp_path: Path) -> None:
        self.port = find_free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.db_uri = f"sqlite:///{tmp_path / 'server.db'}"
        self._tmp_path = tmp_path
        self._artifacts = tmp_path / "artifacts"
        self._artifacts.mkdir()
        self._proc: subprocess.Popen[bytes] | None = None
        self._starts = 0
        self.env = {
            key: os.environ[key] for key in ("PATH", "USER", "LANG", "TMPDIR") if key in os.environ
        }
        self.env.update(
            HOME=str(tmp_path / "server-home"),
            OMNIGENT_AUTH_PROVIDER="accounts",
            OMNIGENT_ACCOUNTS_COOKIE_SECRET=_COOKIE_SECRET_HEX,
            OMNIGENT_ACCOUNTS_BASE_URL=self.url,
            OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME=_OWNER,
            OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD=_OWNER_PASSWORD,
            OMNIGENT_ACCOUNTS_AUTO_OPEN="0",
            OMNIGENT_ADMIN_CREDENTIALS_PATH=str(tmp_path / "admin-credentials"),
            OMNIGENT_CONFIG_HOME=str(tmp_path / "server-config"),
            OMNIGENT_DATA_DIR=str(tmp_path / "server-data"),
            OMNIGENT_DISABLE_CATALOG_LOOKUP="1",
        )
        apply_server_env(self.env, _REPO)

    def start(self) -> None:
        self._starts += 1
        log = (self._tmp_path / f"server-{self._starts}.log").open("w")
        self._proc = subprocess.Popen(
            [
                server_executable(),
                "-c",
                _SERVER_BOOT,
                str(_RUNNER_TOKEN_TTL_S),
                "server",
                "--port",
                str(self.port),
                "--database-uri",
                self.db_uri,
                "--artifact-location",
                str(self._artifacts),
            ],
            env=self.env,
            cwd=compat_server_cwd(),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        log.close()

        def healthy() -> bool:
            assert self._proc is not None and self._proc.poll() is None, "server exited"
            try:
                return httpx.get(f"{self.url}/health", timeout=2).status_code == 200
            except httpx.HTTPError:
                return False

        _wait(healthy, "server health", timeout=60)

    def access_log_has(self, path: str, status: int) -> bool:
        """Whether the server's access log records *path* answered with *status*."""
        needle = f'{path} HTTP/1.1" {status} '
        return any(
            needle in log.read_text(errors="replace")
            for log in (self._tmp_path / "server-data" / "logs" / "server").glob("*.log")
        )

    def stop(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        self._proc.send_signal(signal.SIGTERM)
        try:
            self._proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)


def _login(server_url: str) -> dict[str, Any]:
    response = httpx.post(
        f"{server_url}/auth/login",
        json={"username": _OWNER, "password": _OWNER_PASSWORD},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _read_prompts(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/v1/sessions/{session_id}")
    response.raise_for_status()
    return [
        event
        for event in response.json().get("pending_elicitations", [])
        if event["params"].get("tool_name") == "Read"
    ]


def test_permission_prompt_returns_to_web_after_bearer_lapse(
    tmp_path: Path, gateway: tuple[_Gateway, str]
) -> None:
    scripted, gateway_url = gateway
    # Claude reads the temp directory without asking, so the gated file must
    # live elsewhere. The repo checkout is outside the session's workspace.
    outside_file = _REPO / "README.md"
    assert not str(outside_file).startswith(("/tmp/", os.environ.get("TMPDIR", "/tmp"))), (
        "Run from a checkout outside the temp directory; Claude never prompts for temp reads"
    )
    workspace = tmp_path / "workspace"
    config_dir = tmp_path / "claude-config"
    runner_home = tmp_path / "runner-home"
    runner_data = tmp_path / "runner-data"
    for directory in (workspace, config_dir, runner_home, runner_data):
        directory.mkdir()
    (config_dir / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "projects": {str(workspace): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    print(f"Permission auth-lapse logs: {tmp_path}", flush=True)

    server = _AccountsServer(tmp_path)
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    # The runner has no user credential: like a managed launch, it mints
    # short-lived owner bearers from its binding token.
    runner_env = {key: os.environ[key] for key in ("PATH", "USER", "LANG") if key in os.environ}
    runner_env.update(
        HOME=str(runner_home),
        PYTHONPATH=os.pathsep.join(
            str(path) for path in (_REPO, _REPO / "sdks/python-client", _REPO / "sdks/ui")
        ),
        OMNIGENT_CONFIG_HOME=str(tmp_path / "runner-config"),
        OMNIGENT_DATA_DIR=str(runner_data),
        OMNIGENT_PROCESS_LOG_FILE=str(tmp_path / "runner.log"),
        OMNIGENT_DISABLE_CATALOG_LOOKUP="1",
        OMNIGENT_RUNNER_ID=runner_id,
        OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN=binding_token,
        OMNIGENT_RUNNER_PARENT_PID=str(os.getpid()),
        OMNIGENT_RUNNER_WORKSPACE=str(workspace),
        RUNNER_SERVER_URL=server.url,
        CLAUDE_CONFIG_DIR=str(config_dir),
        ANTHROPIC_AUTH_TOKEN="local-auth-lapse-test",
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
    runner: subprocess.Popen[bytes] | None = None
    tmux: list[str] | None = None
    target = ""
    try:
        server.start()
        owner_token = _login(server.url)["token"]
        with httpx.Client(
            base_url=server.url,
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
            },
            timeout=90,
            trust_env=False,
        ) as client:
            spec = (
                b"name: permission-auth-lapse\n"
                b"prompt: Reply briefly and use the requested tools.\n"
                b"executor:\n  harness: claude-native\n"
                b"os_env:\n  type: caller_process\n  cwd: .\n  sandbox:\n    type: none\n"
            )
            response = post_session_bundle(
                client.post,
                "/v1/sessions",
                bundle_files({"permission-auth-lapse.yaml": spec}),
                metadata={
                    "workspace": str(workspace),
                    "labels": {"omnigent.wrapper": "claude-code-native-ui"},
                },
            )
            response.raise_for_status()
            session_id = response.json()["session_id"]
            # Bind the runner to Alice's session the way a managed launch does,
            # so its binding token mints Alice's bearer.
            host_id = secrets.token_hex(16)
            HostStore(server.db_uri).upsert_on_connect(host_id, "auth-lapse-host", _OWNER)
            conversations = SqlAlchemyConversationStore(server.db_uri)
            conversations.set_host_id(session_id, host_id, workspace=str(workspace))
            conversations.replace_runner_id(session_id, runner_id)

            with (tmp_path / "runner-stdout.log").open("w") as log:
                runner = subprocess.Popen(
                    [sys.executable, "-m", "omnigent.runner._entry"],
                    cwd=workspace,
                    env=runner_env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            _wait(
                lambda: client.get(f"/v1/runners/{runner_id}/status").json().get("online"),
                "runner tunnel",
            )
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

            def screen() -> str:
                assert tmux is not None
                return subprocess.check_output(
                    [*tmux, "capture-pane", "-p", "-t", target], text=True, timeout=5
                )

            _wait(lambda: "❯" in screen(), "Claude composer")
            marker = "READ_DONE_" + secrets.token_hex(8)
            scripted.reply = marker
            scripted.read_path = str(outside_file)
            client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Read the README."}],
                    },
                },
            ).raise_for_status()
            first = _wait(lambda: _read_prompts(client, session_id), "the Read card")[0]
            assert str(outside_file) in first["params"]["content_preview"], first
            (tmp_path / "pane-prompt.txt").write_text(screen())

            # Let the bearer baked into the hook expire, then cut its held poll.
            hook_config = json.loads((bridge_dir / "permission_hook.json").read_text())
            assert hook_config["ap_auth_headers"].get("Authorization"), hook_config
            time.sleep(_RUNNER_TOKEN_TTL_S + 5)
            server.stop()
            server.start()
            # The hook reconnects with a dead bearer and cannot mint another:
            # the user has not signed in on this machine.
            _wait(
                lambda: server.access_log_has(
                    f"/v1/sessions/{session_id}/hooks/permission-request", 401
                ),
                "the server to reject the hook's lapsed bearer",
                timeout=90,
            )
            time.sleep(5)
            assert not _read_prompts(client, session_id), "The card outlived its rejected hook"
            assert not scripted.results, "Claude answered the Read without a verdict"

            # The user signs in, as `omnigent login` would store it.
            login = _login(server.url)
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys, time\n"
                    "from omnigent.cli_auth import store_token\n"
                    "store_token(sys.argv[1], sys.argv[2], sys.argv[3],"
                    " time.time() + float(sys.argv[4]))\n",
                    server.url,
                    login["token"],
                    _OWNER,
                    str(login["expires_in"]),
                ],
                env=runner_env,
                check=True,
                timeout=30,
            )
            back = _wait(
                lambda: _read_prompts(client, session_id),
                "the Read card to return after sign-in",
                timeout=90,
            )[0]
            assert back["elicitation_id"] == first["elicitation_id"], (first, back)
            client.post(
                f"/v1/sessions/{session_id}/elicitations/{back['elicitation_id']}/resolve",
                json={"action": "accept"},
            ).raise_for_status()
            _wait(lambda: scripted.results, "Claude's Read result after the web verdict")
            result = scripted.results[0]
            assert not result.get("is_error"), result
            first_line = outside_file.read_text().splitlines()[0]
            assert first_line in str(result.get("content")), result
            _wait(lambda: not _read_prompts(client, session_id), "the answered card to clear")
    finally:
        if tmux is not None and target:
            subprocess.run([*tmux, "kill-server"], check=False, timeout=10)
        if runner is not None and runner.poll() is None:
            runner.terminate()
            try:
                runner.wait(timeout=15)
            except subprocess.TimeoutExpired:
                runner.kill()
                runner.wait(timeout=5)
        server.stop()
