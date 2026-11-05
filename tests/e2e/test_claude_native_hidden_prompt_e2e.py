"""A real Claude dialog opened mid-reply must not leave chat on a bare spinner.

Claude reports its ``/model`` picker as waiting only while no reply streams. The
local Messages API streams one slow reply; Claude, tmux and the runner watcher run live.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.runner import resource_registry
from omnigent.runner.resource_registry import (
    CLAUDE_NATIVE_TERMINAL_ROLE,
    NATIVE_TERMINAL_PROMPT_BLOCKED_ON,
    SessionResourceRegistry,
)
from omnigent.terminals import TerminalRegistry
from tests._helpers.polling import poll_until_async, wait_for_terminal_screen

_CLAUDE_BINARY = shutil.which("claude")

pytestmark = [
    pytest.mark.skipif(
        _CLAUDE_BINARY is None or shutil.which("tmux") is None,
        reason="requires Claude Code and tmux",
    ),
    pytest.mark.timeout(120),
]

_MODEL = "claude-opus-4-8"
_STREAMED_WORDS = 40


def _sse(event: str, data: dict[str, object]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


@contextmanager
def _slow_messages_api() -> Iterator[str]:
    """Stream each streaming Messages reply at a word per second; answer others plainly."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.split("?", 1)[0] != "/v1/messages" or not body.get("stream"):
                payload = json.dumps(
                    {
                        "id": "msg_side_request",
                        "type": "message",
                        "role": "assistant",
                        "model": _MODEL,
                        "content": [{"type": "text", "text": "ok"}],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            message = {
                "id": "msg_slow_reply",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": _MODEL,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            }
            self.wfile.write(_sse("message_start", {"type": "message_start", "message": message}))
            block = {"type": "text", "text": ""}
            start = {"type": "content_block_start", "index": 0, "content_block": block}
            self.wfile.write(_sse("content_block_start", start))
            self.wfile.flush()
            try:
                for index in range(_STREAMED_WORDS):
                    delta = {"type": "text_delta", "text": f"word{index} "}
                    self.wfile.write(
                        _sse(
                            "content_block_delta",
                            {"type": "content_block_delta", "index": 0, "delta": delta},
                        )
                    )
                    self.wfile.flush()
                    time.sleep(1)
                stop = {"stop_reason": "end_turn", "stop_sequence": None}
                self.wfile.write(
                    _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
                )
                self.wfile.write(
                    _sse(
                        "message_delta",
                        {"type": "message_delta", "delta": stop, "usage": {"output_tokens": 40}},
                    )
                )
                self.wfile.write(_sse("message_stop", {"type": "message_stop"}))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_real_claude_dialog_opened_mid_reply_points_chat_at_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner publishes the terminal-prompt reason, then clears it when answered."""
    config_dir = tmp_path / "claude-config"
    workspace = tmp_path / "workspace"
    config_dir.mkdir()
    workspace.mkdir()
    (config_dir / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "projects": {str(workspace): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    (config_dir / "settings.json").write_text(json.dumps({"disableAllHooks": True}))
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "omnigent-data"))
    # Keep the slow reply short; the production delay only matters for unknown surfaces.
    monkeypatch.setattr(resource_registry, "_HIDDEN_INPUT_BOX_ALERT_SECONDS", 3.0)
    session_id = uuid.uuid4().hex
    bridge_dir = bridge.prepare_bridge_dir(session_id, workspace=workspace, launch_model=_MODEL)
    terminals = TerminalRegistry()
    resources = SessionResourceRegistry(terminals)
    statuses: list[tuple[str, str | None]] = []
    resources.set_session_status_publisher(
        lambda _session, status, blocked_on: statuses.append((status, blocked_on))
    )
    alert = ("running", NATIVE_TERMINAL_PROMPT_BLOCKED_ON)
    with _slow_messages_api() as base_url:
        env = {
            name: os.environ[name]
            for name in ("PATH", "HOME", "USER", "LANG", "TMPDIR")
            if name in os.environ
        }
        env.update(
            CLAUDE_CONFIG_DIR=str(config_dir),
            ANTHROPIC_AUTH_TOKEN="local-hidden-prompt-test",
            ANTHROPIC_BASE_URL=base_url,
            CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST="1",
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            CLAUDE_CODE_DISABLE_CLAUDE_MDS="1",
            DISABLE_AUTOUPDATER="1",
            DISABLE_TELEMETRY="1",
            DISABLE_ERROR_REPORTING="1",
            TERM="xterm-256color",
        )
        try:
            terminal = await terminals.launch(
                session_id,
                "claude",
                "main",
                TerminalEnvSpec(
                    command=_CLAUDE_BINARY,
                    args=[
                        "--model",
                        _MODEL,
                        "--strict-mcp-config",
                        "--mcp-config",
                        '{"mcpServers":{}}',
                    ],
                    env=env,
                    inherit_env=False,
                    os_env=OSEnvSpec(
                        type="caller_process",
                        cwd=str(workspace),
                        sandbox=OSEnvSandboxSpec(type="none"),
                    ),
                ),
            )
            subprocess.run(
                [
                    *("tmux", "-S", str(terminal.socket_path), "resize-window"),
                    *("-t", terminal.tmux_target, "-x", "120", "-y", "40"),
                ],
                check=True,
                timeout=5,
            )
            bridge.write_tmux_target(
                bridge_dir, socket_path=terminal.socket_path, tmux_target=terminal.tmux_target
            )
            await resources.observe_required_terminal(
                session_id, "claude", "main", terminal, resource_role=CLAUDE_NATIVE_TERMINAL_ROLE
            )
            await wait_for_terminal_screen(
                terminal, bridge.claude_pane_text_ready, description="Claude's ready input box"
            )
            assert await terminal.send("Write a long reply.", keys="") == {"status": "sent"}
            await wait_for_terminal_screen(
                terminal,
                lambda pane: "Write a long reply." in pane,
                description="the typed prompt",
            )
            assert await terminal.send(keys="Enter") == {"status": "sent"}
            await wait_for_terminal_screen(
                terminal, lambda pane: "word2" in pane, description="the reply streaming"
            )
            await poll_until_async(lambda: statuses[-1:] == [("running", None)], timeout_s=10)

            assert await terminal.send("/model", keys="") == {"status": "sent"}
            await wait_for_terminal_screen(
                terminal, lambda pane: "/model" in pane, description="/model typed"
            )
            assert await terminal.send(keys="Enter") == {"status": "sent"}
            picker = await wait_for_terminal_screen(
                terminal,
                lambda pane: "Select model" in pane and not bridge.claude_pane_text_ready(pane),
                description="the model picker covering the input box",
            )
            (tmp_path / "native-hidden-prompt.txt").write_text(picker)
            # Claude itself still reports a plain busy turn while the reply streams.
            records = [
                json.loads(path.read_text()) for path in (config_dir / "sessions").glob("*.json")
            ]
            assert [(record["status"], record.get("waitingFor")) for record in records] == [
                ("busy", None)
            ]
            await poll_until_async(lambda: statuses[-1:] == [alert], timeout_s=15)
            assert statuses.count(alert) == 1

            assert await terminal.send(keys="Escape") == {"status": "sent"}
            await wait_for_terminal_screen(
                terminal, bridge.claude_pane_text_ready, description="the input box after Esc"
            )
            await poll_until_async(lambda: statuses[-1:] == [("running", None)], timeout_s=10)
            assert resources.session_turn_is_active(session_id)
        finally:
            await terminals.shutdown()
