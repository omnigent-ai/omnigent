"""Unit tests for the kimi-native (terminal-injection) harness.

Covers the executor's text extraction + capability flags, the tmux bridge's pure
helpers (paste-payload encoding, bridge dir, spawn env, tmux.json round-trip),
and harness registration. The live tmux injection is exercised by the e2e gate,
not here, so these need no tmux or kimi binary.

Unlike cursor-native, kimi-native has NO MCP plumbing (upstream kimi has no
per-spawn MCP config), so the MCP-config tests have no analogue here.
"""

from __future__ import annotations

import asyncio
import logging
import threading
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
from omnigent.llms.errors import RetryableLLMError

_FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "kimi_native"


def _fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text(encoding="utf-8")


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
    assert events[0].message == "Kimi TUI input box did not become ready"


@pytest.mark.asyncio
async def test_run_turn_cancellation_stops_threaded_injection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = threading.Event()
    cancelled_before_paste = threading.Event()
    released = threading.Event()

    def _inject(
        *_args: object, cancel_event: threading.Event | None = None, **_kwargs: object
    ) -> None:
        started.set()
        while not released.is_set():
            if cancel_event is not None and cancel_event.is_set():
                cancelled_before_paste.set()
                return
            released.wait(0.001)

    monkeypatch.setattr(kimi_native_executor, "inject_user_message", _inject)
    events: list[object] = []

    async def _consume() -> None:
        async for event in KimiNativeExecutor(tmp_path).run_turn(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            system_prompt="ignored",
        ):
            events.append(event)

    task = asyncio.create_task(_consume())
    assert await asyncio.to_thread(started.wait, 1.0)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled_before_paste.wait(1.0)
        assert events == []
    finally:
        released.set()


@pytest.mark.asyncio
async def test_steering_uses_short_readiness_budget_and_logs_drop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    seen: dict[str, object] = {}

    def _fail(*_args: object, **kwargs: object) -> None:
        seen.update(kwargs)
        raise RuntimeError("Kimi TUI input box did not become ready")

    monkeypatch.setattr(kimi_native_executor, "inject_user_message", _fail)
    assert await KimiNativeExecutor(tmp_path).enqueue_session_message("session", "steer") is False
    assert seen["timeout_s"] == 30.0
    assert seen["turn_streaming"] is True
    assert "steering message was not delivered" in caplog.text


@pytest.mark.asyncio
async def test_run_turn_marks_approval_pending_retryable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _fail(*_args: object, **_kwargs: object) -> None:
        raise kimi_native_bridge.KimiApprovalPendingError("resolve approval and retry")

    monkeypatch.setattr(kimi_native_executor, "inject_user_message", _fail)
    with pytest.raises(RetryableLLMError, match="resolve approval") as error:
        [
            event
            async for event in KimiNativeExecutor(tmp_path).run_turn(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
                system_prompt="ignored",
            )
        ]
    assert error.value.code == "connection_error"
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=lambda: KimiNativeExecutor(tmp_path))
    assert adapter._build_error_detail(error.value).code == "connection_error"


@pytest.mark.asyncio
async def test_run_turn_bounds_persistent_approval_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _fail(*_args: object, **_kwargs: object) -> None:
        raise kimi_native_bridge.KimiApprovalPendingError("resolve approval and retry")

    monkeypatch.setattr(kimi_native_executor, "inject_user_message", _fail)
    executor = KimiNativeExecutor(tmp_path)
    for _ in range(kimi_native_executor._MAX_APPROVAL_PENDING_RETRIES):
        with pytest.raises(RetryableLLMError):
            [
                event
                async for event in executor.run_turn(
                    messages=[{"role": "user", "content": "hello"}],
                    tools=[],
                    system_prompt="ignored",
                )
            ]
    with pytest.raises(kimi_native_executor.PermanentLLMError, match="resolve approval"):
        [
            event
            async for event in executor.run_turn(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
                system_prompt="ignored",
            )
        ]


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
    """`inject_approval_keystroke` types a digit only while a real menu is visible."""

    def _stub_tmux(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        pane: str,
        alive: bool = True,
        after_key_pane: str | None = None,
        after_key_panes: tuple[str, ...] = (),
        capture_log: list[str] | None = None,
    ) -> list[tuple[str, ...]]:
        sent: list[tuple[str, ...]] = []
        captures = [pane]
        if after_key_panes:
            captures.extend(after_key_panes)
        elif after_key_pane is not None:
            captures.append(after_key_pane)
        monkeypatch.setattr(
            kimi_native_bridge,
            "_wait_for_tmux_info",
            lambda bridge_dir, *, timeout_s: {"socket_path": "/s", "tmux_target": "main"},
        )
        monkeypatch.setattr(kimi_native_bridge, "_session_alive", lambda s, t: alive)

        def _capture(_socket_path: str, _tmux_target: str) -> str:
            pane = captures.pop(0) if len(captures) > 1 else captures[0]
            if capture_log is not None:
                capture_log.append(pane)
            return pane

        monkeypatch.setattr(kimi_native_bridge, "_capture_pane", _capture)
        monkeypatch.setattr(
            kimi_native_bridge,
            "_run_tmux",
            lambda socket_path, *args: sent.append(args),
        )
        return sent

    def test_injects_digit_when_menu_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._stub_tmux(
            monkeypatch,
            pane=_fixture("approval_menu.txt"),
            after_key_pane=_fixture("first_boot_empty.txt"),
        )
        assert inject_approval_keystroke(tmp_path, key=APPROVE_KEY) is True
        assert sent == [("send-keys", "-t", "main", APPROVE_KEY)]

    def test_deny_key_selects_reject(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._stub_tmux(
            monkeypatch,
            pane=_fixture("approval_menu.txt"),
            after_key_pane=_fixture("first_boot_empty.txt"),
        )
        assert inject_approval_keystroke(tmp_path, key=DENY_KEY) is True
        assert sent[0] == ("send-keys", "-t", "main", DENY_KEY)

    @pytest.mark.parametrize("marker", ["Approve for this session", "Reject with feedback"])
    def test_matches_alternate_menu_markers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str
    ) -> None:
        pane = "\n".join(
            [
                "────────────────",
                "▶ Run this command?",
                f"▶ {2 if marker == 'Approve for this session' else 4}. {marker}",
                "  1. Approve once",
                "↑/↓ select · 1/2/3/4 choose · ↵ confirm",
                "────────────────",
            ]
        )
        sent = self._stub_tmux(
            monkeypatch, pane=pane, after_key_pane=_fixture("first_boot_empty.txt")
        )
        assert inject_approval_keystroke(tmp_path, key=APPROVE_KEY) is True
        assert sent[-1] == ("send-keys", "-t", "main", APPROVE_KEY)

    def test_confirms_when_menu_disappears_after_digit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._stub_tmux(
            monkeypatch,
            pane=_fixture("approval_menu.txt"),
            after_key_pane="✨ approved transcript",
        )
        assert inject_approval_keystroke(tmp_path, key=APPROVE_KEY) is True
        assert sent == [("send-keys", "-t", "main", APPROVE_KEY)]

    def test_empty_capture_is_retried_before_menu_disappears(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captures: list[str] = []
        sent = self._stub_tmux(
            monkeypatch,
            pane=_fixture("approval_menu.txt"),
            after_key_panes=("", _fixture("approval_menu.txt"), _fixture("first_boot_empty.txt")),
            capture_log=captures,
        )
        # Generous deadline: the real 0.5s window minus two poll sleeps leaves
        # too little margin under CI load, and this test is about retry order,
        # not the deadline.
        monkeypatch.setattr(kimi_native_bridge, "_APPROVAL_SETTLE_TIMEOUT_S", 5.0)
        monkeypatch.setattr(kimi_native_bridge, "_POLL_INTERVAL_S", 0.01)
        assert inject_approval_keystroke(tmp_path, key=APPROVE_KEY) is True
        assert sent == [("send-keys", "-t", "main", APPROVE_KEY)]
        assert captures[-1] == _fixture("first_boot_empty.txt")

    def test_persistent_empty_capture_is_ambiguous(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._stub_tmux(
            monkeypatch,
            pane=_fixture("approval_menu.txt"),
            after_key_pane="",
        )
        monkeypatch.setattr(kimi_native_bridge, "_APPROVAL_SETTLE_TIMEOUT_S", 0.1)
        monkeypatch.setattr(kimi_native_bridge, "_POLL_INTERVAL_S", 0.2)
        with pytest.raises(
            kimi_native_bridge.KimiApprovalPromptAmbiguousError,
            match="state remained indeterminate",
        ):
            inject_approval_keystroke(tmp_path, key=APPROVE_KEY)
        assert sent == [("send-keys", "-t", "main", APPROVE_KEY)]

    def test_empty_capture_after_session_exit_is_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._stub_tmux(
            monkeypatch,
            pane=_fixture("approval_menu.txt"),
            after_key_pane="",
        )
        alive = iter((True, False))
        monkeypatch.setattr(kimi_native_bridge, "_session_alive", lambda _s, _t: next(alive))
        with pytest.raises(kimi_native_bridge.KimiApprovalSessionNotFoundError):
            inject_approval_keystroke(tmp_path, key=APPROVE_KEY)
        assert sent == [("send-keys", "-t", "main", APPROVE_KEY)]

    def test_same_menu_after_digit_is_ambiguous(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._stub_tmux(
            monkeypatch,
            pane=_fixture("approval_menu.txt"),
            after_key_pane=_fixture("approval_menu.txt"),
        )
        monkeypatch.setattr(kimi_native_bridge, "_APPROVAL_SETTLE_TIMEOUT_S", 0.001)
        monkeypatch.setattr(kimi_native_bridge, "_POLL_INTERVAL_S", 0.0)
        with pytest.raises(kimi_native_bridge.KimiApprovalPromptAmbiguousError):
            inject_approval_keystroke(tmp_path, key=APPROVE_KEY)
        assert sent == [("send-keys", "-t", "main", APPROVE_KEY)]

    def test_wrapped_same_menu_after_digit_is_ambiguous(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wrapped_menu = _fixture("approval_menu.txt").replace(
            "printf approval-fixture", "printf\n   approval-fixture"
        )
        wrapped_menu = wrapped_menu.replace("▶ 1. Approve once", "  1. Approve once")
        sent = self._stub_tmux(
            monkeypatch,
            pane=_fixture("approval_menu.txt"),
            after_key_pane=wrapped_menu,
        )
        monkeypatch.setattr(kimi_native_bridge, "_APPROVAL_SETTLE_TIMEOUT_S", 0.001)
        monkeypatch.setattr(kimi_native_bridge, "_POLL_INTERVAL_S", 0.0)
        with pytest.raises(kimi_native_bridge.KimiApprovalPromptAmbiguousError):
            inject_approval_keystroke(tmp_path, key=APPROVE_KEY)
        assert sent == [("send-keys", "-t", "main", APPROVE_KEY)]

    def test_different_menu_after_digit_is_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other_menu = _fixture("approval_menu.txt").replace(
            "printf approval-fixture", "printf different-fixture"
        )
        sent = self._stub_tmux(
            monkeypatch,
            pane=_fixture("approval_menu.txt"),
            after_key_pane=other_menu,
        )
        assert inject_approval_keystroke(tmp_path, key=APPROVE_KEY) is True
        assert sent == [("send-keys", "-t", "main", APPROVE_KEY)]

    @pytest.mark.parametrize(
        "marker", ["Approve once", "Approve for this session", "Reject", "Reject with feedback"]
    )
    def test_raises_when_only_one_menu_marker_is_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        marker: str,
    ) -> None:
        sent = self._stub_tmux(monkeypatch, pane=f"▶ 1. {marker}")
        with pytest.raises(kimi_native_bridge.KimiApprovalPromptNotFoundError):
            inject_approval_keystroke(tmp_path, key=APPROVE_KEY)
        assert sent == []
        assert "permission menu markers missing" in caplog.text

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

    def test_ignores_numbered_transcript_without_menu_chrome(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._stub_tmux(
            monkeypatch,
            pane="1. Reject malformed rows early\n2. Reject with feedback later",
        )
        with pytest.raises(kimi_native_bridge.KimiApprovalPromptNotFoundError):
            inject_approval_keystroke(tmp_path, key=APPROVE_KEY)
        assert sent == []

    def test_refuses_menu_text_when_editor_is_visible(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _fixture("approval_menu.txt") + "\n" + _fixture("first_boot_empty.txt")
        sent = self._stub_tmux(monkeypatch, pane=pane)
        with pytest.raises(kimi_native_bridge.KimiApprovalPromptNotFoundError):
            inject_approval_keystroke(tmp_path, key=APPROVE_KEY)
        assert sent == []

    def test_raises_when_tui_exited(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._stub_tmux(monkeypatch, pane="▶ 1. Approve once", alive=False)
        with pytest.raises(kimi_native_bridge.KimiApprovalSessionNotFoundError):
            inject_approval_keystroke(tmp_path, key=APPROVE_KEY)
        assert sent == []


class TestSettlePaneReadiness:
    """``_settle_pane`` must recognize the real kimi TUI chrome so it returns on
    the first capture — a wrong marker silently burns the full readiness timeout
    on every web→TUI injection (the original web→TUI latency bug)."""

    def test_requires_input_box_and_live_kimi_footer(self) -> None:
        assert kimi_native_bridge._kimi_tui_ready(_fixture("first_boot_empty.txt"))

    def test_rejects_input_box_when_approval_menu_is_active(self) -> None:
        assert not kimi_native_bridge._kimi_tui_ready(_fixture("approval_menu.txt"))

    def test_rejects_editor_below_real_approval_menu(self) -> None:
        pane = _fixture("approval_menu.txt") + "\n" + _fixture("first_boot_empty.txt")
        assert kimi_native_bridge._kimi_tui_ready(pane)
        assert not kimi_native_bridge._permission_prompt_visible(pane)

    def test_rejects_welcome_banner_without_editor(self) -> None:
        assert not kimi_native_bridge._kimi_tui_ready(_fixture("banner_without_editor.txt"))

    def test_transcript_glyph_is_not_an_input_marker(self) -> None:
        pane = "✨ submitted transcript\ncontext: 0% (0/1M)"
        assert not kimi_native_bridge._kimi_tui_ready(pane)

    def test_real_draft_and_submitted_fixtures_anchor_to_editor_row(self) -> None:
        draft = _fixture("draft_pasted.txt")
        submitted = _fixture("draft_submitted.txt")
        assert kimi_native_bridge._draft_in_input_box(draft, "literal $ and ✨ marker")
        assert not kimi_native_bridge._draft_in_input_box(submitted, "literal $ and ✨ marker")

    def test_editor_frame_context_ignores_menu_text_and_anchors_first_marker(self) -> None:
        pane = "\n".join(
            [
                " ╭────────────────────╮",
                " │ > first draft line │",
                " │   ▶ 1. Approve once │",
                " │   2. Reject with feedback │",
                " ╰────────────────────╯",
                " context: 0%",
            ]
        )
        state = kimi_native_bridge._parse_pane(pane)
        assert state.ready
        assert not state.menu_visible
        assert state.input_content == "first draft line"

    def test_editor_frame_without_top_border_remains_ready(self) -> None:
        pane = "\n".join(
            [
                " │ > first draft line │",
                " │   second line │",
                " ╰────────────────────╯",
                " context: 0%",
            ]
        )
        assert kimi_native_bridge._kimi_tui_ready(pane)

    def test_settle_waits_for_both_markers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captures = {"n": 0}
        panes = iter(["│ > │", _fixture("first_boot_empty.txt")])

        def _capture(_s: str, _t: str) -> str:
            captures["n"] += 1
            return next(panes)

        monkeypatch.setattr(kimi_native_bridge, "_capture_pane", _capture)
        monkeypatch.setattr(kimi_native_bridge.time, "sleep", lambda _s: None)
        kimi_native_bridge._settle_pane("/s", "main", timeout_s=30.0)
        assert captures["n"] == 2

    def test_settle_accepts_real_first_boot_trust_modal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panes = iter([_fixture("trust_modal.txt"), _fixture("first_boot_empty.txt")])
        sent: list[tuple[str, ...]] = []
        monkeypatch.setattr(kimi_native_bridge, "_capture_pane", lambda _s, _t: next(panes))
        monkeypatch.setattr(kimi_native_bridge.time, "sleep", lambda _s: None)
        monkeypatch.setattr(
            kimi_native_bridge,
            "_run_tmux",
            lambda _socket, *args: sent.append(args),
        )
        kimi_native_bridge._settle_pane("/s", "main", timeout_s=30.0)
        assert sent == [("send-keys", "-t", "main", "Enter")]

    def test_settle_fails_fast_when_approval_is_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captures = {"n": 0}

        def _capture(_s: str, _t: str) -> str:
            captures["n"] += 1
            return _fixture("approval_menu.txt")

        monkeypatch.setattr(kimi_native_bridge, "_capture_pane", _capture)
        with pytest.raises(kimi_native_bridge.KimiApprovalPendingError, match="approval"):
            kimi_native_bridge._settle_pane("/s", "main", timeout_s=120.0)
        assert captures["n"] == 1

    def test_settle_does_not_accept_quoted_trust_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panes = iter(
            [
                "Transcript: Trust this folder? was mentioned.\n" + "context: 0% (0/1M)",
                _fixture("first_boot_empty.txt"),
            ]
        )
        sent: list[tuple[str, ...]] = []
        monkeypatch.setattr(kimi_native_bridge, "_capture_pane", lambda _s, _t: next(panes))
        monkeypatch.setattr(
            kimi_native_bridge,
            "_run_tmux",
            lambda _socket, *args: sent.append(args),
        )
        kimi_native_bridge._settle_pane("/s", "main", timeout_s=30.0)
        assert sent == []

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

    @pytest.mark.parametrize(
        "content", ["a", "abc", "\n x", "ok", "yes", "go", "✨", "ok\nlong second line"]
    )
    def test_submit_needle_requires_four_characters(self, content: str) -> None:
        assert kimi_native_bridge._submit_needle(content) == ""

    def test_paste_char_count_matches_kimi_utf16_payload_chars(self) -> None:
        content = "\n".join("😀" * 334 for _ in range(3))
        assert kimi_native_bridge._paste_char_count(content) == 2007


class TestUserMessageInjection:
    def _stub_tui(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        submit_after_enters: int | None,
        content: str = "fix the flaky test",
        initial_content: str = "",
        empty_captures_after_submit: int = 0,
        post_paste_captures: tuple[str, ...] = (),
        post_submit_captures: tuple[str, ...] = (),
        raise_on_enter: bool = False,
        pre_clear_captures: tuple[str, ...] = (),
        clear_verifies: bool = True,
        capture_log: list[tuple[int, str]] | None = None,
        cancel_after_clear: threading.Event | None = None,
        sticky_post_paste: bool = False,
    ) -> list[tuple[str, ...]]:
        def _editor_pane(text: str) -> str:
            rows = text.splitlines() or [""]
            editor_rows = [f" │ > {rows[0]} │"] + [f" │   {row} │" for row in rows[1:]]
            return "\n".join(
                [
                    " ╭────────────────────╮",
                    *editor_rows,
                    " ╰────────────────────╯",
                    " context: 0%",
                ]
            )

        bridge_dir = tmp_path / "bridge"
        bridge_dir.mkdir()
        write_tmux_target(bridge_dir, socket_path=Path("/tmp/x/tmux.sock"), tmux_target="main")
        sent: list[tuple[str, ...]] = []
        tui = {"pane": _editor_pane(initial_content)}
        enters = {"count": 0}
        empty_captures = {"count": 0}
        pasted = {"value": False}
        post_paste = list(post_paste_captures)
        transient_captures = list(post_submit_captures)
        pre_clear = list(pre_clear_captures)

        monkeypatch.setattr(
            kimi_native_bridge,
            "_wait_for_tmux_info",
            lambda _bridge_dir, *, timeout_s: {
                "socket_path": "/tmp/x/tmux.sock",
                "tmux_target": "main",
            },
        )
        monkeypatch.setattr(kimi_native_bridge, "_session_alive", lambda _s, _t: True)
        monkeypatch.setattr(
            kimi_native_bridge,
            "_settle_pane",
            lambda _s, _t, *, timeout_s, cancel_event=None: None,
        )

        def _capture_pane(_socket_path: str, _tmux_target: str) -> str:
            if not pasted["value"] and pre_clear:
                pane = pre_clear.pop(0)
            elif pasted["value"] and enters["count"] == 0 and post_paste:
                pane = post_paste[0] if sticky_post_paste else post_paste.pop(0)
            elif enters["count"] and transient_captures:
                pane = transient_captures.pop(0)
            elif empty_captures["count"]:
                empty_captures["count"] -= 1
                pane = ""
            else:
                pane = tui["pane"]
            if capture_log is not None:
                capture_log.append((len(sent), pane))
            return pane

        monkeypatch.setattr(kimi_native_bridge, "_capture_pane", _capture_pane)
        monkeypatch.setattr(kimi_native_bridge, "_PASTE_SETTLE_S", 0.0)
        monkeypatch.setattr(kimi_native_bridge, "_POLL_INTERVAL_S", 0.001)
        monkeypatch.setattr(kimi_native_bridge, "_PASTE_COMMIT_TIMEOUT_S", 0.1)
        monkeypatch.setattr(kimi_native_bridge, "_SUBMIT_VERIFY_TIMEOUT_S", 0.1)
        monkeypatch.setattr(kimi_native_bridge, "_SUBMIT_RETRY_INTERVAL_S", 0.001)

        def _run_tmux(_socket_path: str, *args: str) -> None:
            sent.append(args)
            if "paste-buffer" in args:
                pasted["value"] = True
                tui["pane"] = _editor_pane(content)
            elif args[-1] == "C-c":
                tui["pane"] = _editor_pane("" if clear_verifies else "leftover draft")
                if cancel_after_clear is not None:
                    cancel_after_clear.set()
            elif args[-1] == "Enter":
                if raise_on_enter:
                    raise AssertionError("stale Enter reached the test menu")
                enters["count"] += 1
                if submit_after_enters is not None and enters["count"] >= submit_after_enters:
                    tui["pane"] = _editor_pane("")
                    empty_captures["count"] = empty_captures_after_submit

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

    def test_submits_draft_containing_prompt_markers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        content = "literal $ prompt and ✨ marker"
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content=content,
        )
        inject_user_message(tmp_path / "bridge", content=content)
        assert [args[-1] for args in sent if args[-1] == "Enter"] == ["Enter"]

    def test_submits_multiline_draft_when_first_line_is_short(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        content = "ok\nlong second line"
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content=content,
        )
        inject_user_message(tmp_path / "bridge", content=content)
        assert [args[-1] for args in sent if args[-1] == "Enter"] == ["Enter"]

    def test_menu_after_paste_is_still_pending(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content="run the command",
            post_paste_captures=(_fixture("menu_after_paste.txt"),),
        )
        with pytest.raises(kimi_native_bridge.KimiApprovalPendingError, match="approval"):
            inject_user_message(tmp_path / "bridge", content="run the command")
        assert not any(args[-1] == "Enter" for args in sent)

    def test_menu_after_enter_counts_as_submitted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        menu_after_submit = "✨ run the command\n" + _fixture("menu_after_paste.txt")
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content="run the command",
            post_submit_captures=(menu_after_submit,),
        )
        inject_user_message(tmp_path / "bridge", content="run the command")
        assert [args[-1] for args in sent if args[-1] == "Enter"] == ["Enter"]

    def test_menu_after_enter_with_draft_still_visible_is_pending(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        menu_with_draft = _fixture("draft_pasted.txt") + "\n" + _fixture("approval_menu.txt")
        self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content="run the command",
            post_submit_captures=(menu_with_draft,),
        )
        with pytest.raises(
            kimi_native_bridge.KimiApprovalPendingError, match="before this message"
        ):
            inject_user_message(tmp_path / "bridge", content="run the command")

    def test_final_pre_enter_capture_blocks_new_menu(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content="literal $ and ✨ marker",
            post_paste_captures=(
                _fixture("draft_pasted.txt"),
                _fixture("draft_pasted.txt"),
                _fixture("menu_after_paste.txt"),
            ),
            raise_on_enter=True,
        )
        with pytest.raises(kimi_native_bridge.KimiApprovalPendingError, match="approval"):
            inject_user_message(tmp_path / "bridge", content="literal $ and ✨ marker")
        assert not any(args[-1] == "Enter" for args in sent)

    @pytest.mark.parametrize("content", ["ok", "yes", "go", "✨"])
    def test_submits_short_drafts_without_a_needle(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str
    ) -> None:
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content=content,
        )
        inject_user_message(tmp_path / "bridge", content=content)
        assert [args[-1] for args in sent if args[-1] == "Enter"] == ["Enter"]

    @pytest.mark.parametrize(
        ("content", "stale_draft"),
        [
            pytest.param("ok", "old streaming draft", id="short-message"),
            pytest.param(
                "please fix the flaky test now",
                "please fix the flaky test yesterday",
                id="shared-prefix",
            ),
        ],
    )
    def test_streaming_paste_must_change_preexisting_draft_before_submit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        content: str,
        stale_draft: str,
    ) -> None:
        stale_pane = "\n".join(
            [
                " ╭────────────────────╮",
                f" │ > {stale_draft} │",
                " ╰────────────────────╯",
                " context: 0%",
            ]
        )
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content=content,
            initial_content=stale_draft,
            post_paste_captures=(stale_pane,),
            sticky_post_paste=True,
        )

        with pytest.raises(RuntimeError, match="not delivered"):
            inject_user_message(tmp_path / "bridge", content=content, turn_streaming=True)

        assert not any(args[-1] == "Enter" for args in sent)

    def test_submits_long_multi_row_draft(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        content = "\n".join(f"line {index} with enough text" for index in range(1, 8))
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content=content,
        )
        inject_user_message(tmp_path / "bridge", content=content)
        assert [args[-1] for args in sent if args[-1] == "Enter"] == ["Enter"]

    def test_submits_real_multiline_paste_placeholder(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        content = "\n".join(f"round7 line {index} with enough text" for index in range(1, 11))
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content=content,
            post_paste_captures=(_fixture("paste_placeholder_11_lines.txt"),) * 100,
        )
        inject_user_message(tmp_path / "bridge", content=content)
        assert [args[-1] for args in sent if args[-1] == "Enter"] == ["Enter"]

    def test_submits_real_chars_paste_placeholder(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        content = "\n".join("A" * 419 for _ in range(3))
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content=content,
            post_paste_captures=(_fixture("paste_placeholder_1260_chars.txt"),) * 100,
        )
        inject_user_message(tmp_path / "bridge", content=content)
        assert [args[-1] for args in sent if args[-1] == "Enter"] == ["Enter"]

    def test_submits_draft_when_wrap_splits_needle(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        content = "A" * 12 + "B" * 12 + "C" * 476
        wrapped_pane = "\n".join(
            [
                " ╭────────────────────╮",
                f" │ > {content[:12]} │",
                f" │   {content[12:24]} │",
                f" │   {content[24:]} │",
                " ╰────────────────────╯",
                " context: 0%",
            ]
        )
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content=content,
            post_paste_captures=(wrapped_pane,) * 100,
        )
        inject_user_message(tmp_path / "bridge", content=content)
        assert [args[-1] for args in sent if args[-1] == "Enter"] == ["Enter"]

    def test_stale_paste_placeholder_does_not_verify_new_paste(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        content = "\n".join(f"line {index}" for index in range(1, 11))
        stale_placeholder = _fixture("paste_placeholder_11_lines.txt").replace(
            "[paste #1 +11 lines]", "[paste #999 +11 lines]"
        )
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content=content,
            initial_content="[paste #999 +11 lines]",
            post_paste_captures=(stale_placeholder,),
            sticky_post_paste=True,
        )
        with pytest.raises(RuntimeError, match="not delivered"):
            inject_user_message(tmp_path / "bridge", content=content, turn_streaming=True)
        assert not any(args[-1] == "Enter" for args in sent)

    def test_literal_paste_placeholder_text_is_draft_content(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        content = "[paste #999 +2 lines]"
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content=content,
            initial_content=content,
        )
        with pytest.raises(RuntimeError, match="not delivered"):
            inject_user_message(tmp_path / "bridge", content=content, turn_streaming=True)
        assert not any(args[-1] == "Enter" for args in sent)

    def test_clears_draft_with_single_ctrl_c(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            initial_content="leftover draft",
        )
        inject_user_message(tmp_path / "bridge", content="fix the flaky test")
        assert ("send-keys", "-t", "main", "C-c") in sent
        assert not any(args[-1] in {"C-a", "C-k"} for args in sent)

    def test_waits_for_delayed_clear_redraw(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pane = "\n".join(
            [
                " ╭────────────────────╮",
                " │ > leftover draft │",
                " ╰────────────────────╯",
                " context: 0%",
            ]
        )
        empty = "\n".join(
            [
                " ╭────────────────────╮",
                " │ > │",
                " ╰────────────────────╯",
                " context: 0%",
                " Press Ctrl+C again to exit",
            ]
        )
        capture_log: list[tuple[int, str]] = []
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content="new message",
            initial_content="leftover draft",
            pre_clear_captures=(pane, pane, pane, empty),
            capture_log=capture_log,
        )
        inject_user_message(tmp_path / "bridge", content="new message")
        assert ("send-keys", "-t", "main", "C-c") in sent
        assert any("paste-buffer" in args for args in sent)
        assert any(sent_count >= 1 and captured == pane for sent_count, captured in capture_log)
        assert any(sent_count == 1 and captured == empty for sent_count, captured in capture_log)

    def test_exit_hint_after_submit_does_not_fail_delivery(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        submitted = "\n".join(
            [
                " ╭────────────────────╮",
                " │ > │",
                " ╰────────────────────╯",
                " context: 0%",
                " Press Ctrl+C again to exit",
            ]
        )
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content="new message",
            post_submit_captures=(submitted,),
        )
        inject_user_message(tmp_path / "bridge", content="new message")
        assert [args[-1] for args in sent if args[-1] == "Enter"] == ["Enter"]

    def test_cancellation_during_clear_restores_existing_draft(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cancel_event = threading.Event()
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            initial_content="leftover draft",
            cancel_after_clear=cancel_event,
        )
        with pytest.raises(RuntimeError, match="cancelled"):
            inject_user_message(
                tmp_path / "bridge",
                content="new message",
                cancel_event=cancel_event,
            )
        assert [args[0] for args in sent if args[0] == "load-buffer"] == ["load-buffer"]
        assert not any(args[-1] == "Enter" for args in sent)

    def test_restore_skips_permission_menu_and_logs_lost_draft(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sent: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            kimi_native_bridge,
            "_capture_pane",
            lambda _socket, _target: _fixture("approval_menu.txt"),
        )
        monkeypatch.setattr(
            kimi_native_bridge,
            "_run_tmux",
            lambda _socket, *args: sent.append(args),
        )
        with caplog.at_level(logging.WARNING, logger="omnigent.kimi_native_bridge"):
            kimi_native_bridge._restore_editor_content(
                "/s", "main", tmp_path, "draft that must not be lost silently"
            )
        assert sent == []
        assert "lost draft length=36" in caplog.text

    def test_restore_rechecks_before_paste_buffer(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        captures = iter((_fixture("first_boot_empty.txt"), _fixture("approval_menu.txt")))
        sent: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            kimi_native_bridge, "_capture_pane", lambda _socket, _target: next(captures)
        )
        monkeypatch.setattr(
            kimi_native_bridge,
            "_run_tmux",
            lambda _socket, *args: sent.append(args),
        )
        with caplog.at_level(logging.WARNING, logger="omnigent.kimi_native_bridge"):
            kimi_native_bridge._restore_editor_content(
                "/s", "main", tmp_path, "draft that must not be lost silently"
            )
        assert [args[0] for args in sent] == ["load-buffer"]
        assert "lost draft length=36" in caplog.text

    def test_does_not_clear_empty_editor(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content="new message",
        )
        inject_user_message(tmp_path / "bridge", content="new message")
        assert not any(args[-1] == "C-c" for args in sent)

    def test_streaming_injection_queues_without_clearing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            content="steer now",
            initial_content="leftover draft",
        )
        inject_user_message(tmp_path / "bridge", content="steer now", turn_streaming=True)
        assert not any(args[-1] == "C-c" for args in sent)

    def test_clear_must_be_verified_before_paste(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            initial_content="leftover draft",
            clear_verifies=False,
        )
        with pytest.raises(RuntimeError, match="did not clear"):
            inject_user_message(tmp_path / "bridge", content="new message")
        assert not any("paste-buffer" in args for args in sent)

    def test_menu_race_before_clear_is_pending(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            initial_content="leftover draft",
            pre_clear_captures=(_fixture("approval_menu.txt"),),
        )
        with pytest.raises(kimi_native_bridge.KimiApprovalPendingError, match="approval"):
            inject_user_message(tmp_path / "bridge", content="new message")
        assert not any(args[-1] == "C-c" for args in sent)

    def test_exit_armed_draft_is_not_cleared(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pane = "\n".join(
            [
                " ╭────────────────────╮",
                " │ > leftover draft │",
                " ╰────────────────────╯",
                " context: 0%",
                " Press Ctrl+C again to exit",
            ]
        )
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            pre_clear_captures=(pane,),
        )
        with pytest.raises(RuntimeError, match="exit-armed"):
            inject_user_message(tmp_path / "bridge", content="new message")
        assert not any(args[-1] == "C-c" for args in sent)

    def test_raises_when_submit_capture_stays_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            empty_captures_after_submit=1000,
        )
        monkeypatch.setattr(kimi_native_bridge, "_SUBMIT_VERIFY_TIMEOUT_S", 0.01)
        with pytest.raises(RuntimeError, match="not delivered"):
            inject_user_message(tmp_path / "bridge", content="fix the flaky test")

    def test_nonempty_capture_without_input_row_is_unverified(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sent = self._stub_tui(
            monkeypatch,
            tmp_path,
            submit_after_enters=1,
            post_submit_captures=("spinner running",) * 100,
        )
        monkeypatch.setattr(kimi_native_bridge, "_SUBMIT_VERIFY_TIMEOUT_S", 0.01)
        with pytest.raises(RuntimeError, match="not delivered"):
            inject_user_message(tmp_path / "bridge", content="fix the flaky test")
        assert [args[-1] for args in sent if args[-1] == "Enter"] == ["Enter"]

    def test_interrupt_marks_active_injection_cancelled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        event = threading.Event()
        kimi_native_bridge._register_injection(tmp_path, event)
        sent: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            kimi_native_bridge,
            "_wait_for_tmux_info",
            lambda _bridge_dir, *, timeout_s: {"socket_path": "/s", "tmux_target": "main"},
        )
        monkeypatch.setattr(kimi_native_bridge, "_run_tmux", lambda _s, *args: sent.append(args))
        try:
            kimi_native_bridge.inject_interrupt(tmp_path)
        finally:
            kimi_native_bridge._unregister_injection(tmp_path, event)
        assert event.is_set()
        assert sent[-1][-1] == "Escape"
        assert (tmp_path / kimi_native_bridge._INJECTION_CANCEL_FILE).exists()

    def test_cancelled_injection_does_not_paste(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sent = self._stub_tui(monkeypatch, tmp_path, submit_after_enters=1)
        cancel_event = threading.Event()
        cancel_event.set()
        with pytest.raises(RuntimeError, match="cancelled"):
            inject_user_message(
                tmp_path / "bridge",
                content="fix the flaky test",
                cancel_event=cancel_event,
            )
        assert not any("paste-buffer" in args for args in sent)


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
