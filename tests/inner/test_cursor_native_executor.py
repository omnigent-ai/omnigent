"""Unit tests for the cursor-native (terminal-injection) harness.

Covers the executor's text extraction + capability flags, the tmux bridge's pure
helpers (paste-payload encoding, bridge dir, spawn env, tmux.json round-trip),
and harness registration. The live tmux injection is exercised by the e2e gate,
not here, so these need no tmux or cursor-agent.
"""

from __future__ import annotations

from pathlib import Path

from omnigent.cursor_native_bridge import (
    BRIDGE_DIR_ENV_VAR,
    REQUEST_SESSION_ID_ENV_VAR,
    _paste_payload_bytes,
    bridge_dir_for_session_id,
    build_cursor_native_spawn_env,
    read_tmux_info,
    write_tmux_target,
)
from omnigent.inner.cursor_native_executor import (
    CursorNativeExecutor,
    _content_to_text,
    _latest_user_text,
)


class TestContentExtraction:
    def test_string_content(self) -> None:
        assert _content_to_text("hello") == "hello"

    def test_input_text_blocks(self) -> None:
        content = [
            {"type": "input_text", "text": "one"},
            {"type": "text", "text": "two"},
            {"type": "input_image", "image_url": "data:..."},
        ]
        assert _content_to_text(content) == "one\n\ntwo"

    def test_empty_and_none(self) -> None:
        assert _content_to_text(None) == ""
        assert _content_to_text([]) == ""

    def test_latest_user_text(self) -> None:
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "second"},
        ]
        assert _latest_user_text(messages) == "second"
        assert _latest_user_text([{"role": "assistant", "content": "x"}]) == ""


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

    def test_spawn_env_carries_bridge_dir_and_session(self) -> None:
        env = build_cursor_native_spawn_env("conv_xyz")
        assert env[BRIDGE_DIR_ENV_VAR] == str(bridge_dir_for_session_id("conv_xyz"))
        assert env[REQUEST_SESSION_ID_ENV_VAR] == "conv_xyz"

    def test_tmux_target_round_trip(self, tmp_path: Path) -> None:
        write_tmux_target(tmp_path, socket_path=Path("/tmp/x/tmux.sock"), tmux_target="main")
        info = read_tmux_info(tmp_path)
        assert info == {"socket_path": "/tmp/x/tmux.sock", "tmux_target": "main"}

    def test_read_tmux_info_missing(self, tmp_path: Path) -> None:
        assert read_tmux_info(tmp_path) is None


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
