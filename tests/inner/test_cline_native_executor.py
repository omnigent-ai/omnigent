"""Unit tests for the cline-native (terminal-injection) harness.

Covers the executor's text extraction + capability flags, the tmux bridge's pure
helpers (paste-payload encoding, bridge dir, spawn env, tmux.json round-trip),
and harness registration. The live tmux injection is exercised by the e2e gate,
not here, so these need no tmux or cline binary.
"""

from __future__ import annotations

from pathlib import Path

from omnigent.cline_native_bridge import (
    BRIDGE_DIR_ENV_VAR,
    _paste_payload_bytes,
    bridge_dir_for_session_id,
    build_cline_native_spawn_env,
    read_tmux_info,
    write_tmux_target,
)
from omnigent.inner.cline_native_executor import (
    ClineNativeExecutor,
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
        ex = ClineNativeExecutor(bridge_dir=tmp_path)
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
        assert "cline-native" in str(a1)

    def test_spawn_env_carries_bridge_dir(self) -> None:
        env = build_cline_native_spawn_env("conv_xyz")
        assert env[BRIDGE_DIR_ENV_VAR] == str(bridge_dir_for_session_id("conv_xyz"))
        # The cline bridge has no active-session concept (unlike claude/pi) and no
        # hook plumbing (unlike kimi), so only the bridge dir is emitted.
        assert list(env) == [BRIDGE_DIR_ENV_VAR]

    def test_tmux_target_round_trip(self, tmp_path: Path) -> None:
        write_tmux_target(tmp_path, socket_path=Path("/tmp/x/tmux.sock"), tmux_target="main")
        info = read_tmux_info(tmp_path)
        assert info == {"socket_path": "/tmp/x/tmux.sock", "tmux_target": "main"}

    def test_read_tmux_info_missing(self, tmp_path: Path) -> None:
        assert read_tmux_info(tmp_path) is None


class TestRegistration:
    def test_harness_is_registered(self) -> None:
        from omnigent.runtime.harnesses import _HARNESS_MODULES

        assert _HARNESS_MODULES["cline-native"] == "omnigent.inner.cline_native_harness"

    def test_harness_is_allowlisted(self) -> None:
        from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES

        assert "cline-native" in OMNIGENT_HARNESSES

    def test_cline_native_is_terminal_native(self) -> None:
        # cline-native launches the cline TUI in an omnigent terminal (like
        # cursor/kimi-native), so the runner must treat it as a native terminal
        # harness.
        from omnigent.harness_aliases import is_native_harness

        assert is_native_harness("cline-native") is True
        assert is_native_harness("native-cline") is True

    def test_native_coding_agent_record(self) -> None:
        from omnigent.native_coding_agents import native_coding_agent_for_harness

        agent = native_coding_agent_for_harness("cline-native")
        assert agent is not None
        assert agent.terminal_name == "cline"
        assert agent.display_name == "Cline"
