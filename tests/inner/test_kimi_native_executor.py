"""Unit tests for the kimi-native (terminal-injection) harness.

Covers the executor's text extraction + capability flags, the tmux bridge's pure
helpers (paste-payload encoding, bridge dir, spawn env, tmux.json round-trip),
and harness registration. The live tmux injection is exercised by the e2e gate,
not here, so these need no tmux or kimi binary.

Unlike cursor-native, kimi-native has NO MCP plumbing (upstream kimi has no
per-spawn MCP config), so the MCP-config tests have no analogue here.
"""

from __future__ import annotations

from inspect import signature
from pathlib import Path

import pytest

from omnigent import kimi_native_bridge
from omnigent.inner import kimi_native_executor
from omnigent.inner.executor import ExecutorError
from omnigent.inner.kimi_native_executor import (
    KimiNativeExecutor,
    _content_to_text,
    _latest_user_text,
)
from omnigent.kimi_native_bridge import (
    APPROVE_KEY,
    BRIDGE_DIR_ENV_VAR,
    DENY_KEY,
    _paste_payload_bytes,
    bridge_dir_for_session_id,
    build_kimi_native_spawn_env,
    inject_approval_keystroke,
    inject_user_message,
    read_tmux_info,
    write_tmux_target,
)


class TestContentExtraction:
    def test_string_content(self, tmp_path: Path) -> None:
        assert _content_to_text("hello", tmp_path) == "hello"

    def test_input_text_blocks(self, tmp_path: Path) -> None:
        content = [
            {"type": "input_text", "text": "one"},
            {"type": "text", "text": "two"},
            # invalid data URI -> not materialized -> visible marker line
            {"type": "input_image", "image_url": "data:..."},
        ]
        assert _content_to_text(content, tmp_path) == (
            "[Attachment attachment could not be loaded]\n\none\n\ntwo"
        )

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
        ex = KimiNativeExecutor(bridge_dir=tmp_path)
        # Output is shown by the embedded terminal, not streamed by the executor.
        assert ex.supports_streaming() is False
        # Web-UI messages can be injected mid-turn (steering).
        assert ex.supports_live_message_queue() is True


@pytest.mark.asyncio
async def test_run_turn_surfaces_injection_failure_as_actionable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Kimi TUI input box did not become ready")

    monkeypatch.setattr(kimi_native_executor, "inject_user_message", _fail)
    events = [
        event
        async for event in KimiNativeExecutor(tmp_path).run_turn(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "retry the turn after the Kimi terminal is ready" in events[0].message


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
        assert "kimi-native" in str(a1)

    def test_spawn_env_carries_bridge_dir(self) -> None:
        env = build_kimi_native_spawn_env("conv_xyz")
        assert env[BRIDGE_DIR_ENV_VAR] == str(bridge_dir_for_session_id("conv_xyz"))
        # Only the bridge dir is emitted (no MCP / active-session guard env).
        assert list(env) == [BRIDGE_DIR_ENV_VAR]

    def test_tmux_target_round_trip(self, tmp_path: Path) -> None:
        write_tmux_target(tmp_path, socket_path=Path("/tmp/x/tmux.sock"), tmux_target="main")
        info = read_tmux_info(tmp_path)
        assert info == {"socket_path": "/tmp/x/tmux.sock", "tmux_target": "main"}

    def test_read_tmux_info_missing(self, tmp_path: Path) -> None:
        assert read_tmux_info(tmp_path) is None


class TestApprovalKeystroke:
    """`inject_approval_keystroke` types the option digit + Enter, guarded by
    the permission-menu marker so a stray verdict can't leak a keystroke."""

    def _stub_tmux(
        self, monkeypatch: pytest.MonkeyPatch, *, pane: str, alive: bool = True
    ) -> list[tuple[str, ...]]:
        sent: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            kimi_native_bridge,
            "_wait_for_tmux_info",
            lambda bridge_dir, *, timeout_s: {"socket_path": "/s", "tmux_target": "main"},
        )
        monkeypatch.setattr(kimi_native_bridge, "_session_alive", lambda s, t: alive)
        monkeypatch.setattr(kimi_native_bridge, "_capture_pane", lambda s, t: pane)
        monkeypatch.setattr(
            kimi_native_bridge,
            "_run_tmux",
            lambda socket_path, *args: sent.append(args),
        )
        return sent

    def test_injects_digit_and_enter_when_menu_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._stub_tmux(monkeypatch, pane="▶ 1. Approve once\n  3. Reject")
        assert inject_approval_keystroke(tmp_path, key=APPROVE_KEY) is True
        assert sent == [
            ("send-keys", "-t", "main", APPROVE_KEY),
            ("send-keys", "-t", "main", "Enter"),
        ]

    def test_deny_key_selects_reject(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._stub_tmux(monkeypatch, pane="▶ 1. Approve once\n  3. Reject")
        assert inject_approval_keystroke(tmp_path, key=DENY_KEY) is True
        assert sent[0] == ("send-keys", "-t", "main", DENY_KEY)

    @pytest.mark.parametrize("marker", ["Approve for session", "Reject with feedback"])
    def test_matches_alternate_menu_markers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str
    ) -> None:
        sent = self._stub_tmux(monkeypatch, pane=f"▶ 2. {marker}")
        assert inject_approval_keystroke(tmp_path, key=APPROVE_KEY) is True
        assert sent[-1] == ("send-keys", "-t", "main", "Enter")

    def test_raises_when_menu_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sent = self._stub_tmux(monkeypatch, pane="● Hello! How can I help?")
        with pytest.raises(kimi_native_bridge.KimiApprovalPromptNotFoundError):
            inject_approval_keystroke(tmp_path, key=APPROVE_KEY)
        assert sent == []
        assert "permission menu markers missing" in caplog.text

    def test_raises_when_tui_exited(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._stub_tmux(monkeypatch, pane="▶ 1. Approve once", alive=False)
        with pytest.raises(kimi_native_bridge.KimiApprovalPromptNotFoundError):
            inject_approval_keystroke(tmp_path, key=APPROVE_KEY)
        assert sent == []


class TestSettlePaneReadiness:
    """``_settle_pane`` must recognize the real kimi TUI chrome so it returns on
    the first capture — a wrong marker silently burns the full readiness timeout
    on every web→TUI injection (the original web→TUI latency bug)."""

    def test_requires_input_box_and_live_kimi_footer(self) -> None:
        # Footer chrome captured verbatim from a live K2.7 session.
        footer = (
            " K2.7 Code thinking  ~/omnigent  pr521-kimi-native [+61 -8]"
            '   ask Kimi to schedule tasks, e.g. "remind me at 5pm"\n'
            "   context: 6.5% (17.0k/262.1k)"
        )
        pane = "alice✨ " + "\n" + footer
        assert kimi_native_bridge._kimi_tui_ready(pane)
        # The cursor-native strings carried over unverified never appeared.
        assert "Plan, search, build" not in footer
        assert "Add a follow-up" not in footer

    def test_settle_waits_for_both_markers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captures = {"n": 0}
        panes = iter(["alice✨ ", "alice✨ \ncontext: 6.5% (17.0k/262.1k)"])

        def _capture(_s: str, _t: str) -> str:
            captures["n"] += 1
            return next(panes)

        monkeypatch.setattr(kimi_native_bridge, "_capture_pane", _capture)
        monkeypatch.setattr(kimi_native_bridge.time, "sleep", lambda _s: None)
        kimi_native_bridge._settle_pane("/s", "main", timeout_s=30.0)
        assert captures["n"] == 2

    def test_settle_raises_when_tui_never_mounts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(kimi_native_bridge, "_capture_pane", lambda _s, _t: "booting")
        with pytest.raises(kimi_native_bridge.KimiTuiNotReadyError, match="not delivered"):
            kimi_native_bridge._settle_pane("/s", "main", timeout_s=0.0)

    def test_readiness_budget_is_separate_and_long_enough(self) -> None:
        assert kimi_native_bridge._KIMI_READY_TIMEOUT_S >= 120.0
        assert kimi_native_bridge._KIMI_READY_TIMEOUT_S != kimi_native_bridge._TMUX_READY_TIMEOUT_S
        assert (
            signature(kimi_native_bridge.inject_interrupt).parameters["timeout_s"].default
            == kimi_native_bridge._TMUX_READY_TIMEOUT_S
        )
        assert (
            signature(kimi_native_bridge.inject_approval_keystroke).parameters["timeout_s"].default
            == kimi_native_bridge._TMUX_READY_TIMEOUT_S
        )
        assert (
            signature(kimi_native_bridge.kill_session).parameters["timeout_s"].default
            == kimi_native_bridge._TMUX_READY_TIMEOUT_S
        )


class TestUserMessageInjection:
    def _stub_tui(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        submit_after_enters: int | None,
    ) -> list[tuple[str, ...]]:
        bridge_dir = tmp_path / "bridge"
        bridge_dir.mkdir()
        write_tmux_target(bridge_dir, socket_path=Path("/tmp/x/tmux.sock"), tmux_target="main")
        sent: list[tuple[str, ...]] = []
        tui = {"pane": "alice✨ "}
        enters = {"count": 0}

        monkeypatch.setattr(
            kimi_native_bridge,
            "_wait_for_tmux_info",
            lambda _bridge_dir, *, timeout_s: {
                "socket_path": "/tmp/x/tmux.sock",
                "tmux_target": "main",
            },
        )
        monkeypatch.setattr(kimi_native_bridge, "_session_alive", lambda _s, _t: True)
        monkeypatch.setattr(kimi_native_bridge, "_settle_pane", lambda _s, _t, *, timeout_s: None)
        monkeypatch.setattr(kimi_native_bridge, "_capture_pane", lambda _s, _t: tui["pane"])
        monkeypatch.setattr(kimi_native_bridge, "_PASTE_SETTLE_S", 0.0)
        monkeypatch.setattr(kimi_native_bridge, "_POLL_INTERVAL_S", 0.001)
        monkeypatch.setattr(kimi_native_bridge, "_PASTE_COMMIT_TIMEOUT_S", 0.1)
        monkeypatch.setattr(kimi_native_bridge, "_SUBMIT_VERIFY_TIMEOUT_S", 0.1)
        monkeypatch.setattr(kimi_native_bridge, "_SUBMIT_RETRY_INTERVAL_S", 0.001)

        def _run_tmux(_socket_path: str, *args: str) -> None:
            sent.append(args)
            if "paste-buffer" in args:
                tui["pane"] = "alice✨ fix the flaky test"
            elif args[-1] == "Enter":
                enters["count"] += 1
                if submit_after_enters is not None and enters["count"] >= submit_after_enters:
                    tui["pane"] = "alice✨ "

        monkeypatch.setattr(kimi_native_bridge, "_run_tmux", _run_tmux)
        return sent

    def test_retries_enter_until_draft_leaves_input_box(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sent = self._stub_tui(monkeypatch, tmp_path, submit_after_enters=2)
        inject_user_message(tmp_path / "bridge", content="fix the flaky test")
        assert [args[-1] for args in sent if args[-1] == "Enter"] == ["Enter", "Enter"]

    def test_raises_when_draft_never_submits(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._stub_tui(monkeypatch, tmp_path, submit_after_enters=None)
        with pytest.raises(RuntimeError, match="not delivered"):
            inject_user_message(tmp_path / "bridge", content="fix the flaky test")


class TestRegistration:
    def test_harness_is_registered(self) -> None:
        from omnigent.runtime.harnesses import _HARNESS_MODULES

        assert _HARNESS_MODULES["kimi-native"] == "omnigent.inner.kimi_native_harness"

    def test_harness_is_allowlisted(self) -> None:
        from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES

        assert "kimi-native" in OMNIGENT_HARNESSES

    def test_kimi_native_is_terminal_native(self) -> None:
        # kimi-native launches the kimi TUI in an omnigent terminal (like
        # claude/codex/cursor-native), so the runner must treat it as a native
        # terminal harness.
        from omnigent.harness_aliases import is_native_harness

        assert is_native_harness("kimi-native") is True
        assert is_native_harness("native-kimi") is True

    def test_native_coding_agent_record(self) -> None:
        from omnigent.native_coding_agents import native_coding_agent_for_harness

        agent = native_coding_agent_for_harness("kimi-native")
        assert agent is not None
        assert agent.terminal_name == "kimi"
        assert agent.display_name == "Kimi"

    def test_distinct_from_headless_kimi_harness(self) -> None:
        # The bare ``kimi`` harness is the headless SDK path; ``kimi-native`` is
        # the TUI path. They must resolve to different harness modules.
        from omnigent.runtime.harnesses import _HARNESS_MODULES

        assert _HARNESS_MODULES["kimi"] != _HARNESS_MODULES["kimi-native"]
