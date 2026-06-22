"""Unit tests for the cursor-native (terminal-injection) harness.

Covers the executor's text extraction + capability flags, the tmux bridge's pure
helpers (paste-payload encoding, bridge dir, spawn env, tmux.json round-trip),
and harness registration. The live tmux injection is exercised by the e2e gate,
not here, so these need no tmux or cursor-agent.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omnigent.cursor_native_bridge import (
    BRIDGE_DIR_ENV_VAR,
    _paste_payload_bytes,
    allow_mcp_tools_in_cli_config,
    approve_mcp_server_for_workspace,
    bridge_dir_for_session_id,
    build_cursor_native_spawn_env,
    build_mcp_config,
    cursor_project_key,
    enable_mcp_for_workspace,
    read_tmux_info,
    write_mcp_bridge_config,
    write_mcp_config,
    write_tmux_target,
)
from omnigent.inner.cursor_native_executor import (
    CursorNativeExecutor,
    _content_to_text,
    _latest_user_text,
)


class TestContentExtraction:
    def test_string_content(self, tmp_path: Path) -> None:
        assert _content_to_text("hello", tmp_path) == "hello"

    def test_input_text_blocks(self, tmp_path: Path) -> None:
        content = [
            {"type": "input_text", "text": "one"},
            {"type": "text", "text": "two"},
            # invalid data URI -> materialize_attachment returns None -> no line
            {"type": "input_image", "image_url": "data:..."},
        ]
        assert _content_to_text(content, tmp_path) == "one\n\ntwo"

    def test_real_image_attachment_materialized(self, tmp_path: Path) -> None:
        # a tiny valid base64 PNG data URI should be written to disk + referenced
        png = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        out = _content_to_text([{"type": "input_image", "image_url": png}], tmp_path)
        assert out.startswith("[Attached: ")
        assert str(tmp_path) in out

    def test_empty_and_none(self, tmp_path: Path) -> None:
        assert _content_to_text(None, tmp_path) == ""
        assert _content_to_text([], tmp_path) == ""

    def test_latest_user_text(self, tmp_path: Path) -> None:
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "second"},
        ]
        assert _latest_user_text(messages, tmp_path) == "second"
        assert _latest_user_text([{"role": "assistant", "content": "x"}], tmp_path) == ""


class TestExecutorCapabilities:
    def test_capability_flags(self, tmp_path: Path) -> None:
        ex = CursorNativeExecutor(bridge_dir=tmp_path)
        # Output is shown by the embedded terminal, not streamed by the executor.
        assert ex.supports_streaming() is False
        # Web-UI messages can be injected mid-turn (steering).
        assert ex.supports_live_message_queue() is True


class TestPastePayload:
    def test_newlines_become_cr(self) -> None:
        assert _paste_payload_bytes("a\nb") == b"a\rb"
        assert _paste_payload_bytes("a\r\nb") == b"a\rb"
        assert _paste_payload_bytes("a\rb") == b"a\rb"

    def test_tab_kept_other_control_dropped(self) -> None:
        # tab kept (0x09), ESC (0x1b) and BEL (0x07) dropped.
        assert _paste_payload_bytes("a\tb\x1b\x07c") == b"a\tbc"

    def test_unicode_passthrough(self) -> None:
        assert _paste_payload_bytes("café") == "café".encode()


class TestBridge:
    def test_bridge_dir_is_deterministic_and_session_scoped(self) -> None:
        a1 = bridge_dir_for_session_id("conv_a")
        a2 = bridge_dir_for_session_id("conv_a")
        b = bridge_dir_for_session_id("conv_b")
        assert a1 == a2
        assert a1 != b
        assert "cursor-native" in str(a1)

    def test_spawn_env_carries_bridge_dir(self) -> None:
        env = build_cursor_native_spawn_env("conv_xyz")
        assert env[BRIDGE_DIR_ENV_VAR] == str(bridge_dir_for_session_id("conv_xyz"))
        # The cursor bridge has no active-session concept (unlike claude/pi), so
        # no request-session-id guard env is emitted — only the bridge dir.
        assert "HARNESS_CURSOR_NATIVE_REQUEST_SESSION_ID" not in env
        assert list(env) == [BRIDGE_DIR_ENV_VAR]

    def test_tmux_target_round_trip(self, tmp_path: Path) -> None:
        write_tmux_target(tmp_path, socket_path=Path("/tmp/x/tmux.sock"), tmux_target="main")
        info = read_tmux_info(tmp_path)
        assert info == {"socket_path": "/tmp/x/tmux.sock", "tmux_target": "main"}

    def test_read_tmux_info_missing(self, tmp_path: Path) -> None:
        assert read_tmux_info(tmp_path) is None

    def test_build_mcp_config_registers_omnigent_relay(self, tmp_path: Path) -> None:
        config = build_mcp_config(tmp_path, python_executable="python-test")
        server = config["mcpServers"]["omnigent"]
        assert server["command"] == "python-test"
        assert server["args"] == [
            "-I",
            "-m",
            "omnigent.claude_native_bridge",
            "serve-mcp",
            "--bridge-dir",
            str(tmp_path),
        ]
        assert "sys_session_send" in server["autoApprove"]
        assert "sys_os_shell" in server["autoApprove"]
        assert server["autoApprove"] == sorted(server["autoApprove"])
        assert server["env"]["TMPDIR"]

    def test_write_mcp_config_is_workspace_scoped(self, tmp_path: Path, monkeypatch) -> None:
        workspace = tmp_path / "workspace"
        bridge_dir = tmp_path / "bridge"
        monkeypatch.setattr(
            "omnigent.cursor_native_bridge.approve_mcp_server_for_workspace",
            lambda _workspace: pytest.fail("approval must happen after tool relay starts"),
        )
        path = write_mcp_config(workspace, bridge_dir, python_executable="python-test")

        assert path == workspace / ".cursor" / "mcp.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["mcpServers"]["omnigent"]["command"] == "python-test"
        assert json.loads((bridge_dir / "bridge.json").read_text(encoding="utf-8"))["token"]

    def test_write_mcp_bridge_config_is_idempotent(self, tmp_path: Path) -> None:
        write_mcp_bridge_config(tmp_path)
        first = (tmp_path / "bridge.json").read_text(encoding="utf-8")
        write_mcp_bridge_config(tmp_path)
        assert (tmp_path / "bridge.json").read_text(encoding="utf-8") == first

    def test_cursor_project_key_matches_cursor_workspace_state(self) -> None:
        assert cursor_project_key(Path("/Users/corey.zumar")) == "Users-corey.zumar"

    def test_enable_mcp_for_workspace_removes_disabled_entry(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        disabled_path = (
            tmp_path / ".cursor" / "projects" / "Users-corey.zumar" / "mcp-disabled.json"
        )
        disabled_path.parent.mkdir(parents=True)
        disabled_path.write_text('["omnigent", "other"]\n', encoding="utf-8")

        enable_mcp_for_workspace(Path("/Users/corey.zumar"))

        assert json.loads(disabled_path.read_text(encoding="utf-8")) == ["other"]

    def test_allow_mcp_tools_in_cli_config_adds_specific_allow_rules(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config_path = tmp_path / ".cursor" / "cli-config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            '{"permissions": {"allow": ["Shell(ls)", "Mcp(omnigent:sys_os_read)"]}}\n',
            encoding="utf-8",
        )

        allow_mcp_tools_in_cli_config()

        allow = json.loads(config_path.read_text(encoding="utf-8"))["permissions"]["allow"]
        assert "Shell(ls)" in allow
        assert allow.count("Mcp(omnigent:sys_os_read)") == 1
        assert "Mcp(omnigent:sys_session_send)" in allow

    def test_approve_mcp_server_for_workspace_uses_cursor_cli(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        calls: list[dict[str, object]] = []

        monkeypatch.setattr(
            "omnigent.cursor_native.resolve_cursor_executable",
            lambda: "/bin/cursor-agent-test",
        )

        def fake_run(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return subprocess.CompletedProcess(args[0], 0)

        monkeypatch.setattr(subprocess, "run", fake_run)

        approve_mcp_server_for_workspace(tmp_path)

        assert calls == [
            {
                "args": (["/bin/cursor-agent-test", "mcp", "enable", "omnigent"],),
                "kwargs": {
                    "cwd": tmp_path,
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL,
                    "timeout": 15,
                    "check": False,
                },
            }
        ]


class TestRegistration:
    def test_harness_is_registered(self) -> None:
        from omnigent.runtime.harnesses import _HARNESS_MODULES

        assert _HARNESS_MODULES["cursor-native"] == "omnigent.inner.cursor_native_harness"

    def test_harness_is_allowlisted(self) -> None:
        from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES

        assert "cursor-native" in OMNIGENT_HARNESSES

    def test_cursor_native_is_terminal_native(self) -> None:
        # cursor-native launches the cursor-agent TUI in an omnigent terminal
        # (like claude/codex/pi-native), so the runner must treat it as a native
        # terminal harness.
        from omnigent.harness_aliases import is_native_harness

        assert is_native_harness("cursor-native") is True
        assert is_native_harness("native-cursor") is True

    def test_native_coding_agent_record(self) -> None:
        from omnigent.native_coding_agents import native_coding_agent_for_harness

        agent = native_coding_agent_for_harness("cursor-native")
        assert agent is not None
        assert agent.terminal_name == "cursor"
        assert agent.display_name == "Cursor"
