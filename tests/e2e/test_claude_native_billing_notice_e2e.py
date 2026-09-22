"""Recover Claude's real billing dialog with a deterministic local Messages API.

The unmodified Claude CLI decides to show and renders the notice when the
provider omits server classifier results. Only model responses are scripted;
its dialog, permission classifier, Bash execution, and Omnigent recovery run live.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native import bridge
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner.resource_registry import (
    CLAUDE_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
)
from omnigent.terminals import TerminalRegistry
from tests.server.integration.mock_llm_server import (
    anthropic_sse_text_response,
    anthropic_sse_tool_call_response,
)

pytestmark = [
    pytest.mark.skipif(
        shutil.which("claude") is None or shutil.which("tmux") is None,
        reason="requires Claude Code 2.1.278+ and tmux",
    ),
    pytest.mark.timeout(120),
]

_MODEL = "claude-opus-4-8"
_TOOL_ID = "toolu_billing_notice_e2e"
_NOTICE = "We're changing auto mode to no longer charge for classifier requests in Claude Code."
_UNCHANGED_BILLING = (
    "Nothing breaks: auto mode keeps working, and its classifier requests are billed as before."
)


class _Gateway:
    """A legacy gateway without server classifier support, as in the incident."""

    def __init__(self, command: str) -> None:
        self.command = command
        self.requests: list[dict[str, Any]] = []
        self.classifier_requests = 0
        self.tool_results: list[dict[str, Any]] = []

    def response(self, body: dict[str, Any]) -> tuple[bytes, str]:
        self.requests.append(body)
        blocks = [
            block
            for message in body.get("messages", [])
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict)
        ]
        results = [
            block
            for block in blocks
            if block.get("type") == "tool_result" and block.get("tool_use_id") == _TOOL_ID
        ]
        if results:
            self.tool_results = results
        model = body.get("model", _MODEL)
        if not body.get("stream"):
            # Claude's real local classifier runs only after the notice closes.
            self.classifier_requests += 1
            return json.dumps(
                {
                    "id": "msg_local_classifier",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": "<block>no</block>"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
            ).encode(), "application/json"
        if any(tool.get("name") == "Bash" for tool in body.get("tools", [])) and not results:
            response = anthropic_sse_tool_call_response(
                [
                    {
                        "call_id": _TOOL_ID,
                        "name": "Bash",
                        "arguments": json.dumps(
                            {
                                "command": self.command,
                                "description": "Write the requested local test marker",
                                "timeout": 10000,
                            }
                        ),
                    }
                ],
                model=model,
            )
        else:
            response = anthropic_sse_text_response("Billing notice test complete.", model=model)
        # Deliberately no safeguard_results: this makes Claude raise its own notice.
        return response.encode(), "text/event-stream"


@contextmanager
def _local_gateway(gateway: _Gateway) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.split("?", 1)[0] == "/v1/messages":
                payload, content_type = gateway.response(body)
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


async def _wait_for_pane(
    terminal: TerminalInstance,
    predicate: Callable[[str], bool],
    *,
    timeout: float = 30,
    description: str,
) -> str:
    deadline = time.monotonic() + timeout
    screen = ""
    while time.monotonic() < deadline:
        result = await terminal.read()
        screen = str(result.get("screen", result))
        if predicate(screen):
            return screen
        await asyncio.sleep(0.1)
    pytest.fail(f"Timed out waiting for {description}; actual Claude pane:\n{screen}")


@pytest.mark.parametrize("recovery", ["runner_watcher", "permission_mode"])
async def test_real_claude_billing_notice_unblocks_pending_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: str
) -> None:
    """A real blocked Bash call must resume without user input to the dialog."""
    config_dir = tmp_path / "claude-config"
    workspace = tmp_path / "workspace"
    config_dir.mkdir()
    workspace.mkdir()
    marker = workspace / "billing-notice-sentinel.txt"
    command = shlex.join(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "Path('billing-notice-sentinel.txt').write_text('confirmed'); "
            "print('billing-notice-tool-completed')",
        ]
    )
    (config_dir / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "cachedGrowthBookFeatures": {
                    "tengu_auto_mode_config": {"enabled": "enabled"},
                    "tengu_velvet_heron": True,
                },
                "projects": {str(workspace): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    (config_dir / "settings.json").write_text(
        json.dumps({"disableAllHooks": True, "skipAutoPermissionPrompt": True})
    )
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "omnigent-data"))
    # Bound the unfixed mode-switch timeout; its readiness behavior is unchanged.
    monkeypatch.setattr(bridge, "_TMUX_READY_SLOW_BOOT_TIMEOUT_S", 15)
    session_id = uuid.uuid4().hex
    bridge_dir = bridge.prepare_bridge_dir(session_id, workspace=workspace, launch_model=_MODEL)
    terminals = TerminalRegistry()
    resources = SessionResourceRegistry(terminals)
    statuses: list[tuple[str, str | None]] = []
    resources.set_session_status_publisher(
        lambda _session, status, blocked_on: statuses.append((status, blocked_on))
    )
    gateway = _Gateway(command)
    with _local_gateway(gateway) as base_url:
        env = {
            name: os.environ[name]
            for name in ("PATH", "HOME", "USER", "LANG", "TMPDIR")
            if name in os.environ
        }
        env.update(
            CLAUDE_CONFIG_DIR=str(config_dir),
            ANTHROPIC_AUTH_TOKEN="local-billing-notice-test",
            ANTHROPIC_BASE_URL=base_url,
            CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST="1",
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            CLAUDE_CODE_GB_DISK_CACHE_WHEN_TELEMETRY_OFF="1",
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
                    command=shutil.which("claude"),
                    args=[
                        "--model",
                        _MODEL,
                        "--permission-mode",
                        "auto",
                        "--strict-mcp-config",
                        "--mcp-config",
                        '{"mcpServers":{}}',
                        "--debug-file",
                        str(tmp_path / "claude-debug.log"),
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
                    "tmux",
                    "-S",
                    str(terminal.socket_path),
                    "resize-window",
                    "-t",
                    terminal.tmux_target,
                    "-x",
                    "140",
                    "-y",
                    "45",
                ],
                check=True,
                timeout=5,
            )
            bridge.write_tmux_target(
                bridge_dir, socket_path=terminal.socket_path, tmux_target=terminal.tmux_target
            )
            await _wait_for_pane(
                terminal,
                lambda pane: "❯" in pane and "auto mode on" in pane,
                description="the ready auto-mode composer",
            )
            assert await terminal.send(
                "Create billing-notice-sentinel.txt containing confirmed using Python in Bash.",
                keys="",
            ) == {"status": "sent"}
            await asyncio.sleep(0.3)
            assert await terminal.send(keys="Enter") == {"status": "sent"}
            notice = await _wait_for_pane(
                terminal,
                lambda pane: _NOTICE in pane and "Enter to continue · Esc to cancel" in pane,
                description="Claude's actual classifier-billing dialog",
            )
            (tmp_path / "native-billing-notice.txt").write_text(notice)
            assert _UNCHANGED_BILLING in notice
            assert base_url.removeprefix("http://") in notice
            assert "which isn't compatible with this update" in notice
            assert not marker.exists(), "The tool must be parked behind the real dialog"
            assert gateway.classifier_requests == 0
            assert not gateway.tool_results
            assert any("safeguards" in request for request in gateway.requests)

            if recovery == "runner_watcher":
                # Attach the real production watcher only after proving the stall.
                await resources.observe_required_terminal(
                    session_id,
                    "claude",
                    "main",
                    terminal,
                    resource_role=CLAUDE_NATIVE_TERMINAL_ROLE,
                )
            else:
                assert (
                    await asyncio.to_thread(
                        bridge.set_permission_mode, bridge_dir, mode="auto", timeout_s=15
                    )
                    == "auto"
                )

            await _wait_for_pane(
                terminal,
                lambda pane: (
                    marker.exists() and bool(gateway.tool_results) and _NOTICE not in pane
                ),
                timeout=20,
                description="automatic notice acknowledgement and completion of the pending Bash",
            )
            assert marker.read_text() == "confirmed"
            assert gateway.classifier_requests > 0
            assert all(not result.get("is_error") for result in gateway.tool_results)
            assert "billing-notice-tool-completed" in json.dumps(gateway.tool_results)
            config = json.loads((config_dir / ".claude.json").read_text())
            assert config.get("autoModeClassifierBillingNoticeAcknowledgedAt", 0) > 0
        finally:
            await terminals.shutdown()
