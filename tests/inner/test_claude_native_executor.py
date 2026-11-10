"""Tests for the native Claude Code bridge executor."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from omnigent.harnesses.claude_native import bridge as claude_bridge
from omnigent.harnesses.claude_native.bridge import (
    REQUEST_SESSION_ID_ENV_VAR,
    ClaudePromptTimeout,
    ClaudeSignInPending,
    ClaudeTerminalDialog,
    ClaudeTerminalExited,
    TmuxSessionNotAdvertised,
)
from omnigent.inner import claude_native_executor
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor as _ClaudeNativeExecutor
from omnigent.inner.executor import (
    ExecutorConfig,
    ExecutorError,
    InjectionDeliveryAmbiguous,
    TextChunk,
    TurnComplete,
)
from omnigent.inner.native_attachments import attachment_cache_dir

# Minimal valid 1x1 white PNG used for multimodal attachment tests.
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
)
_TINY_PNG_DATA_URI = f"data:image/png;base64,{_TINY_PNG_B64}"
_TINY_PNG_BYTES = base64.b64decode(_TINY_PNG_B64)


def _native_executor(
    bridge_dir: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    submit_hook: bool = True,
    native_ready: bool = True,
) -> _ClaudeNativeExecutor:
    """Seed the owned native peer that the executor scenarios replace with callbacks."""
    from omnigent.harnesses.claude_native.framework_context import (
        consume_prompt_framework_context,
        prompt_framework_context_capability_ready,
    )
    from omnigent.harnesses.claude_native.hook import record_hook_payload
    from omnigent.inner.terminal_lifecycle import TERMINAL_INSTANCE_ID_ENV, TERMINAL_LAUNCH_ID_ENV

    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", root)
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", root.parent)
    bridge_dir.mkdir(parents=True, exist_ok=True)
    records = {
        "bridge.json": {
            "active_session_id": claude_native_executor._request_session_id_from_env()
            or "native-test"
        },
        "state.json": {"claude_session_id": "native-session"} if native_ready else {},
        "tmux.json": {
            "socket_path": str(root / "native.sock"),
            "tmux_target": "main",
            "launch_id": "c" * 32,
            "instance_id": "d" * 32,
        },
    }
    for name, payload in records.items():
        if not (bridge_dir / name).exists():
            claude_bridge._write_json_file(bridge_dir / name, payload)
    owner = claude_bridge.read_delivery_owner(bridge_dir)
    if owner.launch_id is not None:
        monkeypatch.setenv(TERMINAL_LAUNCH_ID_ENV, owner.launch_id)
    if owner.instance_id is not None:
        monkeypatch.setenv(TERMINAL_INSTANCE_ID_ENV, owner.instance_id)
    if (
        owner.session_id
        and owner.native_session_id
        and owner.launch_id
        and owner.instance_id
        and not prompt_framework_context_capability_ready(bridge_dir)
    ):
        record_hook_payload(
            bridge_dir,
            {
                "hook_event_name": "SessionStart",
                "session_id": owner.native_session_id,
                "source": "startup",
            },
            env={
                TERMINAL_LAUNCH_ID_ENV: owner.launch_id,
                TERMINAL_INSTANCE_ID_ENV: owner.instance_id,
            },
            prompt_framework_context=True,
        )
    injector = claude_native_executor.inject_user_message
    if (
        submit_hook
        and injector is not claude_bridge.inject_user_message
        and not getattr(injector, "_emits_owned_submit_hook", False)
    ):

        def inject_with_hook(directory: Path, *, content: str, **kwargs: Any) -> None:
            injector(directory, content=content, **kwargs)
            consume_prompt_framework_context(
                directory,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": claude_bridge.read_delivery_owner(directory).native_session_id,
                    "prompt": content,
                },
            )

        inject_with_hook._emits_owned_submit_hook = True
        monkeypatch.setattr(claude_native_executor, "inject_user_message", inject_with_hook)
    return _ClaudeNativeExecutor(bridge_dir)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "startup", ["matched", "foreign-launch", "replacement", "cancelled", "legacy-hook"]
)
@pytest.mark.parametrize("routed", [False, True], ids=["plain", "routed"])
async def test_cold_plain_input_waits_for_its_captured_launch_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, startup: str, routed: bool
) -> None:
    from omnigent.harnesses.claude_native.hook import record_hook_payload
    from omnigent.inner.terminal_lifecycle import TERMINAL_INSTANCE_ID_ENV, TERMINAL_LAUNCH_ID_ENV

    typed: list[dict[str, Any]] = []
    model_switches: list[str] = []
    killed = Mock()
    monkeypatch.setattr(claude_native_executor, "inject_user_message", _stub_inject(typed))
    monkeypatch.setattr(claude_native_executor, "kill_session", killed)
    monkeypatch.setattr(
        claude_native_executor, "_wait_for_claude_prompt_ready", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(claude_native_executor, "read_launch_model", lambda _bridge: None)
    monkeypatch.setattr(
        claude_native_executor,
        "read_model_env",
        lambda _bridge: {"ANTHROPIC_DEFAULT_SONNET_MODEL": "gateway-model"},
    )
    monkeypatch.setattr(
        claude_native_executor,
        "inject_slash_command",
        lambda *_args, command, **_kwargs: model_switches.append(command),
    )
    monkeypatch.setattr(claude_native_executor, "_MESSAGE_HOLD_POLL_S", 0.005)
    monkeypatch.setattr(claude_native_executor, "_MESSAGE_HOLD_S", 0.08)
    executor = _native_executor(
        tmp_path, monkeypatch=monkeypatch, root=tmp_path, native_ready=False
    )
    legacy_context = tmp_path / claude_bridge.CLAUDE_FRAMEWORK_CONTEXT_FILE
    legacy_context.write_text("accepted context for the older hook", encoding="utf-8")

    async def deliver() -> list[object]:
        return [
            event
            async for event in executor.run_turn(
                [{"role": "user", "content": "first plain input"}],
                [],
                "",
                config=ExecutorConfig(model="gateway-model") if routed else None,
            )
        ]

    task = asyncio.create_task(deliver())
    async with asyncio.timeout(1):
        while not executor._holding_message:
            await asyncio.sleep(0)
    assert typed == []
    assert model_switches == []
    if startup == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        if startup == "replacement":
            claude_bridge.write_tmux_target(
                tmp_path,
                socket_path=tmp_path / "native.sock",
                tmux_target="main",
                launch_id="e" * 32,
                instance_id="f" * 32,
            )
        else:
            record_hook_payload(
                tmp_path,
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "native-session",
                    "source": "startup",
                },
                env={
                    TERMINAL_LAUNCH_ID_ENV: ("e" if startup == "foreign-launch" else "c") * 32,
                    TERMINAL_INSTANCE_ID_ENV: ("f" if startup == "foreign-launch" else "d") * 32,
                },
                prompt_framework_context=startup != "legacy-hook",
            )
        events = await asyncio.wait_for(task, 1)
        if startup == "matched":
            assert events == [TurnComplete(response=None)]
            assert [entry["content"] for entry in typed] == ["first plain input"]
            assert model_switches == (["/model sonnet"] if routed else [])
        else:
            assert len(events) == 1 and isinstance(events[0], ExecutorError)
            assert events[0].undelivered
            assert typed == []
            assert model_switches == []
            if startup == "legacy-hook":
                assert "Let its current work finish" in events[0].message
    assert legacy_context.read_text(encoding="utf-8") == "accepted context for the older hook"
    killed.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True])
async def test_stop_retains_old_context_fence_without_discarding_successor_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, replacement: bool
) -> None:
    from omnigent.harnesses.claude_native.framework_context import (
        PromptFrameworkContextPending,
        consume_prompt_framework_context,
        write_prompt_framework_context,
    )
    from omnigent.harnesses.claude_native.hook import record_hook_payload
    from omnigent.inner.native_attachments import framework_notice_block
    from omnigent.inner.terminal_lifecycle import TERMINAL_INSTANCE_ID_ENV, TERMINAL_LAUNCH_ID_ENV

    def unconfirmed(*_args: Any, **_kwargs: Any) -> None:
        raise claude_bridge.ClaudeDeliveryUnconfirmed("delivery unconfirmed")

    monkeypatch.setattr(claude_native_executor, "inject_user_message", unconfirmed)
    executor = _native_executor(
        tmp_path, monkeypatch=monkeypatch, root=tmp_path, submit_hook=False
    )
    dimensions = {"width": 6000, "height": 4000}
    events = [
        event
        async for event in executor.run_turn(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "identical input"},
                        framework_notice_block(dimensions),
                    ],
                }
            ],
            [],
            "",
        )
    ]
    assert len(events) == 1 and isinstance(events[0], ExecutorError)
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "native-session",
        "prompt": "identical input",
    }

    if replacement:
        claude_bridge.write_tmux_target(
            tmp_path,
            socket_path=tmp_path / "native.sock",
            tmux_target="main",
            launch_id="e" * 32,
            instance_id="f" * 32,
        )
        record_hook_payload(
            tmp_path,
            {"hook_event_name": "SessionStart", "session_id": "native-session"},
            env={TERMINAL_LAUNCH_ID_ENV: "e" * 32, TERMINAL_INSTANCE_ID_ENV: "f" * 32},
            prompt_framework_context=True,
        )
        successor = write_prompt_framework_context(
            tmp_path, "identical input", ["successor context"]
        )
        assert not await executor.interrupt_session("session")
        assert successor.path.exists()
        assert consume_prompt_framework_context(tmp_path, payload) is None
        monkeypatch.setenv(TERMINAL_LAUNCH_ID_ENV, "e" * 32)
        monkeypatch.setenv(TERMINAL_INSTANCE_ID_ENV, "f" * 32)
        assert consume_prompt_framework_context(tmp_path, payload) == "successor context"
    else:
        assert not await executor.interrupt_session("session")
        with pytest.raises(PromptFrameworkContextPending):
            write_prompt_framework_context(tmp_path, "identical input", ["successor context"])
        assert consume_prompt_framework_context(tmp_path, payload) == ""
        write_prompt_framework_context(tmp_path, "identical input", ["successor context"])
        assert consume_prompt_framework_context(tmp_path, payload) == "successor context"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_point", ["run-turn", "live-steer"])
async def test_unconfirmed_paste_preserves_native_work_without_replaying(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entry_point: str
) -> None:
    attempts: list[str] = []
    killed = Mock()

    def unconfirmed(_bridge_dir: Path, *, content: str, **_kwargs: object) -> None:
        attempts.append(content)
        raise claude_bridge.ClaudeDeliveryUnconfirmed("Claude did not confirm this pasted message")

    monkeypatch.setattr(claude_native_executor, "inject_user_message", unconfirmed)
    monkeypatch.setattr(claude_native_executor, "kill_session", killed)
    executor = _native_executor(tmp_path / "bridge", monkeypatch=monkeypatch, root=tmp_path)
    if entry_point == "live-steer":
        with pytest.raises(InjectionDeliveryAmbiguous):
            await executor.enqueue_session_message("session", "keep the original input")
    else:
        events = [
            event
            async for event in executor.run_turn(
                [{"role": "user", "content": "keep the original input"}], [], ""
            )
        ]
        assert len(events) == 1
        error = events[0]
        assert isinstance(error, ExecutorError)
        assert error.code == "claude_delivery_unconfirmed"
        assert not error.undelivered
        assert error.preserve_session
    assert attempts == ["keep the original input"]
    killed.assert_not_called()


@pytest.mark.asyncio
async def test_run_turn_injects_user_message_without_streaming_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Web UI turns are typed into Claude's tmux pane only.

    The background transcript forwarder is the only path allowed to
    produce visible Omnigent chat items. This fails if the executor
    regresses to tailing JSONL and producing duplicate assistant text.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "claude.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    sent_messages: list[dict[str, Any]] = []

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Capture the injected message and write a transcript line.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text typed into the Claude tmux pane.
        :param timeout_s: tmux-target readiness timeout (ignored
            here — the fake doesn't shell out).
        :returns: None.
        """
        del timeout_s
        sent_messages.append({"bridge_dir": bridge_dir_arg, "content": content})
        transcript_path.write_text("terminal-owned output\n", encoding="utf-8")

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fake_inject_user_message,
    )

    executor = _native_executor(bridge_dir, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hello from web"}],
            tools=[],
            system_prompt="ignored",
        )
    ]

    # The executor must deliver exactly the user's text to the
    # bridge. If this assertion changes shape, the harness has
    # picked up an extra envelope (metadata, framing, etc.) that
    # wasn't in the original CLAUDE_NATIVE design.
    assert sent_messages == [
        {
            "bridge_dir": bridge_dir,
            "content": "hello from web",
        }
    ]
    assert events == [TurnComplete(response=None)]
    assert not (bridge_dir / "transcript_forwarder.json").exists()
    assert not (bridge_dir / "transcript_forwarder.pause.json").exists()


@pytest.mark.asyncio
async def test_run_turn_does_not_advertise_active_omnigent_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The executor does not create a second AP-visible tool path.

    Claude-native chat visibility is terminal-originated. Web-chat
    submission is an input adapter, so tool activity must come back
    from Claude's transcript rather than from a transient Omnigent turn.
    """
    bridge_dir = tmp_path / "bridge"
    sent_messages: list[dict[str, Any]] = []

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Capture a web-message injection.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text typed into the Claude tmux pane.
        :param timeout_s: tmux-target readiness timeout (ignored).
        :returns: None.
        """
        del timeout_s
        sent_messages.append({"bridge_dir": bridge_dir_arg, "content": content})

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)

    executor = _native_executor(bridge_dir, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "use a tool"}],
            tools=[
                {
                    "name": "sys_os_read",
                    "description": "Read a file.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            system_prompt="ignored",
        )
    ]

    assert sent_messages == [{"bridge_dir": bridge_dir, "content": "use a tool"}]
    assert events == [TurnComplete(response=None)]
    assert not (bridge_dir / "tool_relay.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_run_turn_rejects_stale_session_after_clear(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    queued: bool,
) -> None:
    """
    Old-session turns must not type into the post-``/clear`` Claude pane.

    The request session id comes from the harness spawn env. If it no
    longer matches the bridge's active session, the executor must fail
    before calling tmux injection.
    """
    (tmp_path / "bridge.json").write_text(
        '{"active_session_id": "conv_new"}',
        encoding="utf-8",
    )
    monkeypatch.setenv(REQUEST_SESSION_ID_ENV_VAR, "conv_old")

    def fail_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Fail if stale-session protection reaches tmux injection.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text that would be typed into tmux.
        :param timeout_s: tmux-target readiness timeout.
        :returns: Never returns.
        """
        del bridge_dir_arg, content, timeout_s
        raise AssertionError("stale session injected into tmux")

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fail_inject_user_message,
    )

    executor = _native_executor(tmp_path, monkeypatch=monkeypatch, root=tmp_path)

    async def run() -> list[object]:
        return [
            event
            async for event in executor.run_turn(
                messages=[{"role": "user", "content": "old tab message"}],
                tools=[],
                system_prompt="ignored",
            )
        ]

    if queued:
        (tmp_path / "bridge.json").write_text('{"active_session_id": "conv_old"}')
        ready_for_lock = asyncio.Event()

        def model_command_arg(_model: str | None) -> None:
            ready_for_lock.set()

        monkeypatch.setattr(executor, "_model_command_arg", model_command_arg)
        async with executor._inject_lock:
            pending = asyncio.create_task(run())
            await ready_for_lock.wait()
            (tmp_path / "bridge.json").write_text('{"active_session_id": "conv_new"}')
        events = await pending
    else:
        events = await run()

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "no longer active after /clear" in events[0].message
    assert events[0].undelivered is True


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/login", "/logout"])
@pytest.mark.parametrize(
    ("in_managed_sandbox", "required_phrases", "unreachable_remedy"),
    [
        (False, ("omni setup",), "new session"),
        (
            True,
            (
                "credentials at launch",
                "new session",
                "if sign-in still fails",
                "sandbox administrator",
                "refresh its credentials",
            ),
            "omni setup",
        ),
    ],
)
async def test_run_turn_points_auth_commands_at_a_remedy_reachable_from_the_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
    in_managed_sandbox: bool,
    required_phrases: tuple[str, ...],
    unreachable_remedy: str,
) -> None:
    """
    ``/login`` must not be typed into the pane as a prompt.

    Claude Code's sign-in is an interactive TUI handoff the bridge
    cannot drive, so the bridge escapes ``/login`` into plain text and
    the CLI answers it as an ordinary message. On an expired login that
    answer is "Login expired · Please run /login" — the instruction the
    user just followed, so the turn is wasted and the session is stuck.
    Fail the turn with a remedy the user can perform: ``omni setup`` on
    their own host, or a new session (then the sandbox administrator) on a
    managed sandbox (``IS_SANDBOX=1``), which cannot run it.
    """
    if in_managed_sandbox:
        monkeypatch.setenv("IS_SANDBOX", "1")
    else:
        monkeypatch.delenv("IS_SANDBOX", raising=False)

    def fail_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Fail if an auth command reaches tmux injection.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text that would be typed into tmux.
        :param timeout_s: tmux-target readiness timeout.
        :returns: Never returns.
        """
        del bridge_dir_arg, content, timeout_s
        raise AssertionError("auth slash command injected into tmux")

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fail_inject_user_message,
    )

    executor = _native_executor(tmp_path, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": command}],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    for phrase in required_phrases:
        assert phrase in events[0].message
    assert unreachable_remedy not in events[0].message
    assert events[0].undelivered is True


@pytest.mark.asyncio
@pytest.mark.parametrize("free_bytes", [4096, 512 * 1024 * 1024])
async def test_run_turn_holds_a_message_until_the_transcript_disk_has_space(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    free_bytes: int,
) -> None:
    """
    A full disk holds the message before typing and delivers once space returns.

    Claude Code would otherwise run the message without recording it, leaving
    the web transcript missing and later reporting a delivery failure.
    """
    transcript = tmp_path / "projects" / "session.jsonl"
    (tmp_path / claude_bridge._STATE_FILE).write_text(
        json.dumps({"transcript_path": str(transcript), "claude_session_id": "native-session"}),
        encoding="utf-8",
    )
    checked: list[Path] = []
    typed: list[str] = []

    def fake_disk_usage(path: Path) -> SimpleNamespace:
        checked.append(Path(path))
        available = free_bytes if len(checked) == 1 else 512 * 1024 * 1024
        return SimpleNamespace(total=1 << 40, used=(1 << 40) - available, free=available)

    def fake_inject_user_message(
        bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0
    ) -> None:
        del bridge_dir_arg, timeout_s
        typed.append(content)

    monkeypatch.setattr(shutil, "disk_usage", fake_disk_usage)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)
    monkeypatch.setattr(claude_native_executor, "_HOST_DISK_POLL_S", 0.0, raising=False)

    executor = _native_executor(tmp_path, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hello from web"}],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert typed == ["hello from web"]
    assert events == (
        [
            TextChunk(text=claude_native_executor._HOST_DISK_WAIT_MESSAGE),
            TurnComplete(response=None),
        ]
        if free_bytes == 4096
        else [TurnComplete(response=None)]
    )
    assert checked == [transcript.parent] if free_bytes > 4096 else [transcript.parent] * 2


@pytest.mark.asyncio
async def test_run_turn_cancels_a_full_disk_hold_without_failing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Closing a full-disk turn releases the wait without an error or keystrokes."""
    transcript = tmp_path / "projects" / "session.jsonl"
    (tmp_path / claude_bridge._STATE_FILE).write_text(
        json.dumps({"transcript_path": str(transcript), "claude_session_id": "native-session"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        claude_native_executor,
        "_transcript_disk_full",
        Mock(return_value=True),
    )
    injected = Mock()
    monkeypatch.setattr(claude_native_executor, "inject_user_message", injected)
    executor = _native_executor(tmp_path, monkeypatch=monkeypatch, root=tmp_path)
    turn = executor.run_turn(
        messages=[{"role": "user", "content": "hello from web"}],
        tools=[],
        system_prompt="ignored",
    )

    assert isinstance(await turn.__anext__(), TextChunk)
    pending_next = asyncio.create_task(turn.__anext__())
    await asyncio.sleep(0)
    pending_next.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await pending_next
    await turn.aclose()

    injected.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/login", "/logout"])
async def test_enqueue_session_message_refuses_auth_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
) -> None:
    """
    A live-steered ``/login`` must not be typed into the pane either.

    ``enqueue_session_message`` is the second injection path: a message
    sent while a turn is active. Refusing it (``False``) leaves the
    runner's buffered copy undelivered, so the message arrives as the
    next turn and ``run_turn``'s short-circuit answers it with the
    ``omni setup`` guidance. The monkeypatched injector raises, so this
    test fails if anything reaches tmux.
    """

    def fail_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Fail if an auth command reaches tmux injection.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text that would be typed into tmux.
        :param timeout_s: tmux-target readiness timeout.
        :returns: Never returns.
        """
        del bridge_dir_arg, content, timeout_s
        raise AssertionError("auth slash command injected into tmux")

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fail_inject_user_message,
    )

    executor = _native_executor(tmp_path, monkeypatch=monkeypatch, root=tmp_path)
    accepted = await executor.enqueue_session_message("session-key", command)

    assert accepted is False


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_enqueue_session_message_rejects_stale_session_after_clear(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    queued: bool,
) -> None:
    """
    Stale-session steering must not reach the post-``/clear`` Claude pane.
    """
    (tmp_path / "bridge.json").write_text(
        '{"active_session_id": "conv_new"}',
        encoding="utf-8",
    )
    monkeypatch.setenv(REQUEST_SESSION_ID_ENV_VAR, "conv_old")

    def fail_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Fail if stale-session steering reaches tmux injection.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text that would be typed into tmux.
        :param timeout_s: tmux-target readiness timeout.
        :returns: Never returns.
        """
        del bridge_dir_arg, content, timeout_s
        raise AssertionError("stale session injected into tmux")

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fail_inject_user_message,
    )

    executor = _native_executor(tmp_path, monkeypatch=monkeypatch, root=tmp_path)
    if queued:
        (tmp_path / "bridge.json").write_text('{"active_session_id": "conv_old"}')
        ready_for_lock = asyncio.Event()

        def content_to_text(_content: object, _bridge_dir: Path) -> str:
            ready_for_lock.set()
            return "old steering"

        monkeypatch.setattr(claude_native_executor, "_content_to_text", content_to_text)
        async with executor._inject_lock:
            pending = asyncio.create_task(executor.enqueue_session_message("main", "old steering"))
            await ready_for_lock.wait()
            (tmp_path / "bridge.json").write_text('{"active_session_id": "conv_new"}')
        injected = await pending
    else:
        injected = await executor.enqueue_session_message("main", "old steering")

    assert injected is False


@pytest.mark.asyncio
async def test_enqueue_session_message_injects_steering_into_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    In-flight server messages are typed into Claude's tmux pane.

    This catches regressions where web UI steering is accepted by the
    harness but never reaches the native Claude Code process.
    """
    sent_messages: list[dict[str, Any]] = []

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Capture a steering injection.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text typed into the Claude tmux pane.
        :param timeout_s: tmux-target readiness timeout (ignored).
        :returns: None.
        """
        del timeout_s
        sent_messages.append({"bridge_dir": bridge_dir_arg, "content": content})

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fake_inject_user_message,
    )

    executor = _native_executor(tmp_path, monkeypatch=monkeypatch, root=tmp_path)
    accepted = await executor.enqueue_session_message("session-key", "steer me")

    assert accepted is True
    # Steering injection delivers raw text only — no envelope. The
    # session_key is intentionally NOT included since there is one
    # tmux pane per conversation; mixing in routing metadata would
    # cause Claude to see arbitrary key-value pairs as user input.
    assert sent_messages == [
        {
            "bridge_dir": tmp_path,
            "content": "steer me",
        }
    ]


@pytest.mark.asyncio
async def test_concurrent_injections_do_not_overlap_in_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two injections must not write to the tmux pane at the same time.

    Repro for the claude-native "12"/"23" message-combining symptom.
    ``inject_user_message`` is not atomic: it issues several ``tmux
    send-keys`` calls in sequence (clear line, type literal text, send
    Enter). The executor runs each injection via ``asyncio.to_thread``
    and does NOT serialize them, so a ``run_turn`` injection and a
    mid-turn ``enqueue_session_message`` injection can land in the
    thread pool concurrently and interleave their keystrokes against the
    same pane — e.g. typing "1" and "2" into one prompt as "12".

    This test drives those two real code paths concurrently. The fake
    ``inject_user_message`` records the maximum number of injections
    inside its (otherwise atomic) critical region at once. The invariant
    under test is that the executor serializes terminal writes, so that
    maximum must be 1.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)

    state = {"now": 0, "max": 0}
    state_lock = threading.Lock()
    release = threading.Event()

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """Record peak concurrency, then hold the call open until released.

        :param bridge_dir_arg: Bridge directory (ignored).
        :param content: Text that would be typed into tmux (ignored).
        :param timeout_s: tmux-target readiness timeout (ignored).
        :returns: None.
        """
        del bridge_dir_arg, content, timeout_s
        with state_lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        # Hold the keystroke sequence open so a second, concurrent
        # injection (if the executor fails to serialize) is observed
        # inside this critical region at the same time, bumping max to 2.
        release.wait(timeout=2.0)
        with state_lock:
            state["now"] -= 1

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fake_inject_user_message,
    )

    executor = _native_executor(tmp_path, monkeypatch=monkeypatch, root=tmp_path)

    async def _drive_run_turn() -> None:
        """Consume a run_turn (the initial-message injection path)."""
        async for _ in executor.run_turn(
            messages=[{"role": "user", "content": "one"}],
            tools=[],
            system_prompt="",
        ):
            pass

    # Path A: run_turn injection. Path B: mid-turn steering injection.
    # Both call inject_user_message via asyncio.to_thread concurrently.
    run_turn_task = asyncio.create_task(_drive_run_turn())
    enqueue_task = asyncio.create_task(executor.enqueue_session_message("k", "two"))

    # Sync gate: wait until at least one injection is inside the region.
    for _ in range(200):
        if state["max"] >= 1:
            break
        await asyncio.sleep(0.01)
    # Give the second injection a chance to enter concurrently. With
    # proper serialization it cannot — it would block until the first
    # releases — so max stays 1. Without it, both enter and max hits 2.
    for _ in range(50):
        if state["max"] >= 2:
            break
        await asyncio.sleep(0.01)

    release.set()
    await asyncio.gather(run_turn_task, enqueue_task)

    # max == 2 means both injections wrote to the pane simultaneously,
    # which is exactly the interleaving that combines "1" and "2" into
    # "12". A correct executor serializes terminal writes → max == 1.
    assert state["max"] == 1, (
        f"concurrent injections overlapped in the tmux pane "
        f"(peak concurrency {state['max']}); the executor must serialize "
        f"terminal writes so keystrokes from different messages cannot "
        f"interleave into a single prompt (the '12'/'23' bug)."
    )


# -- Multimodal attachment tests ------------------------------------------


def _stub_inject(
    sent: list[dict[str, Any]],
) -> Any:
    """
    Build a fake ``inject_user_message`` that captures calls.

    :param sent: Mutable list that receives one dict per invocation,
        keyed by ``bridge_dir`` and ``content``.
    :returns: Callable matching ``inject_user_message``'s signature.
    """

    def _fake(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        del timeout_s
        sent.append({"bridge_dir": bridge_dir_arg, "content": content})

    return _fake


@pytest.mark.asyncio
async def test_run_turn_materializes_image_to_bridge_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An ``input_image`` block with a resolved data URI is decoded to a
    file in the bridge directory and referenced by path in the text
    injected into Claude's terminal.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = _native_executor(tmp_path, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": _TINY_PNG_DATA_URI,
                            "filename": "screenshot.png",
                        },
                        {"type": "input_text", "text": "what is this?"},
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    # Turn completes successfully after injection.
    assert events == [TurnComplete(response=None)]
    assert len(sent) == 1
    injected = sent[0]["content"]

    # Attachment reference line appears before the user's text.
    # If the image block was silently dropped (pre-fix behavior),
    # the injected text would be just "what is this?" with no path.
    assert "[Attached:" in injected, (
        "Image block was dropped — _content_to_text did not materialize it"
    )
    assert "screenshot.png" in injected
    assert "what is this?" in injected
    # Attachment line must come before user text.
    attach_pos = injected.index("[Attached:")
    text_pos = injected.index("what is this?")
    assert attach_pos < text_pos, (
        "Attachment reference should precede user text so Claude sees the "
        "file path before the question"
    )

    # The file was written to disk with the correct content.
    uploads = attachment_cache_dir(tmp_path)
    written = list(uploads.iterdir())
    # Exactly 1 file — the materialized PNG.
    assert len(written) == 1, (
        f"Expected 1 written file, got {len(written)}. If 0, the attachment was not materialized."
    )
    assert written[0].name == "screenshot.png"
    # Byte-level check: decoded content matches the original PNG.
    assert written[0].read_bytes() == _TINY_PNG_BYTES


@pytest.mark.asyncio
async def test_resize_notice_uses_hidden_hook_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Claude terminal input excludes the framework notice."""
    from omnigent.inner.native_attachments import framework_notice_block, resize_notice

    dimensions = {"width": 6000, "height": 4000}
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(claude_native_executor, "inject_user_message", _stub_inject(sent))
    executor = _native_executor(
        tmp_path, monkeypatch=monkeypatch, root=tmp_path, submit_hook=False
    )

    async for _ in executor.run_turn(
        [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "inspect this"},
                    framework_notice_block(dimensions),
                ],
            },
            {"role": "developer", "content": "unrelated context"},
        ],
        [],
        "",
    ):
        pass

    assert sent[0]["content"] == "inspect this"
    from omnigent.harnesses.claude_native.framework_context import consume_prompt_framework_context

    assert consume_prompt_framework_context(
        tmp_path,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "native-session",
            "prompt": "inspect this",
        },
    ) == resize_notice(dimensions)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "retained"),
    [
        (RuntimeError("injection failed"), True),
        (claude_bridge.ClaudeDeliveryUnconfirmed("delivery unknown"), True),
        (ClaudePromptTimeout("timeout"), False),
    ],
)
async def test_failed_injection_clears_framework_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception, retained: bool
) -> None:
    from omnigent.harnesses.claude_native.framework_context import (
        PROMPT_FRAMEWORK_CONTEXT_DIR,
        consume_prompt_framework_context,
    )
    from omnigent.inner.native_attachments import framework_notice_block, resize_notice

    def fail_inject(*args: Any, **kwargs: Any) -> None:
        assert list((tmp_path / PROMPT_FRAMEWORK_CONTEXT_DIR).glob("*.json"))
        raise error

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(claude_native_executor, "kill_session", lambda *args, **kwargs: None)
    executor = _native_executor(
        tmp_path, monkeypatch=monkeypatch, root=tmp_path, submit_hook=False
    )
    events = [
        event
        async for event in executor.run_turn(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "inspect this"},
                        framework_notice_block({"width": 6000, "height": 4000}),
                    ],
                }
            ],
            [],
            "",
        )
    ]
    assert isinstance(events[0], ExecutorError)
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "native-session",
        "prompt": "inspect this",
    }
    assert (
        consume_prompt_framework_context(tmp_path, {**payload, "prompt": "foreign prompt"}) is None
    )
    expected = resize_notice({"width": 6000, "height": 4000}) if retained else None
    assert consume_prompt_framework_context(tmp_path, payload) == expected
    content = [
        {"type": "input_text", "text": "inspect this"},
        framework_notice_block({"width": 6000, "height": 4000}),
    ]
    if retained:
        with pytest.raises(InjectionDeliveryAmbiguous):
            await executor.enqueue_session_message("session-key", content)
    else:
        assert not await executor.enqueue_session_message("session-key", content)
    assert consume_prompt_framework_context(tmp_path, payload) == expected


@pytest.mark.asyncio
async def test_run_turn_image_only_no_text_still_injects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A message with only an image (no text) materializes the file and
    injects the path reference. The executor must not yield an error.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = _native_executor(tmp_path, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": _TINY_PNG_DATA_URI,
                            "filename": "photo.png",
                        },
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    # Must complete, not error — an image-only message is valid input.
    # If _content_to_text returned "" (dropping the image), the
    # executor would yield ExecutorError instead of TurnComplete.
    assert events == [TurnComplete(response=None)]
    assert len(sent) == 1
    assert "photo.png" in sent[0]["content"]


@pytest.mark.asyncio
async def test_run_turn_unresolved_file_id_emits_visible_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An ``input_image`` block with only a ``file_id`` (content resolver
    did not run) injects a visible could-not-load marker with the text.

    Silently skipping the block made the model hallucinate the image;
    the marker tells both the model and the user the attachment is gone.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = _native_executor(tmp_path, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "file_id": "file_abc123"},
                        {"type": "input_text", "text": "analyze this"},
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    assert len(sent) == 1
    # The unresolved image block becomes a visible marker, not a silent drop.
    assert sent[0]["content"] == ("[Attachment file_abc123 could not be loaded]\n\nanalyze this")
    # No uploads directory created — nothing to materialize.
    assert not (attachment_cache_dir(tmp_path)).exists()


@pytest.mark.asyncio
async def test_run_turn_materializes_zip_outside_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A ZIP reaches Claude by absolute cache path without changing the checkout."""
    import json

    workspace = tmp_path / "repo"
    workspace.mkdir()
    (tmp_path / "bridge.json").write_text(
        json.dumps({"workspace": str(workspace), "active_session_id": "native-test"})
    )

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(claude_native_executor, "inject_user_message", _stub_inject(sent))

    zip_bytes = b"PK\x03\x04 fake zip"
    executor = _native_executor(tmp_path, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_file",
                            "file_data": (
                                "data:application/zip;base64,"
                                f"{base64.b64encode(zip_bytes).decode()}"
                            ),
                            "filename": "bundle.zip",
                        },
                        {"type": "input_text", "text": "what is in here?"},
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    materialized = attachment_cache_dir(tmp_path) / "bundle.zip"
    assert materialized.read_bytes() == zip_bytes
    assert list(workspace.iterdir()) == []
    assert f"[Attached: {materialized}]" in sent[0]["content"]


@pytest.mark.asyncio
async def test_run_turn_materializes_zip_without_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Attachment delivery works before any workspace is recorded at launch."""
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(claude_native_executor, "inject_user_message", _stub_inject(sent))

    executor = _native_executor(tmp_path, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_file",
                            "file_data": "data:application/zip;base64,UEsDBA==",
                            "filename": "bundle.zip",
                        },
                        {"type": "input_text", "text": "unpack this"},
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    assert (
        sent[0]["content"]
        == f"[Attached: {attachment_cache_dir(tmp_path) / 'bundle.zip'}]\n\nunpack this"
    )


@pytest.mark.asyncio
async def test_run_turn_dedup_same_filename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Two different images under the same filename produce distinct files
    (the second gets a unique suffix instead of overwriting the first).
    Identical bytes would instead reuse the existing file.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    other_bytes = b"not-the-same-png"
    image_block = {
        "type": "input_image",
        "image_url": _TINY_PNG_DATA_URI,
        "filename": "dup.png",
    }
    other_block = {
        "type": "input_image",
        "image_url": "data:image/png;base64," + base64.b64encode(other_bytes).decode(),
        "filename": "dup.png",
    }
    executor = _native_executor(tmp_path, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [image_block, other_block],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    uploads = attachment_cache_dir(tmp_path)
    written = sorted(uploads.iterdir())
    # Two distinct files, not one overwritten file.
    assert len(written) == 2, (
        f"Expected 2 files (dedup suffix), got {len(written)}. "
        "If 1, the second image overwrote the first."
    )
    # Both payloads survive, each in its own file.
    assert {f.read_bytes() for f in written} == {_TINY_PNG_BYTES, other_bytes}


@pytest.mark.asyncio
async def test_run_turn_image_without_filename_gets_generated_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An image block without a ``filename`` field gets a generated name
    with the correct extension derived from the MIME type.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = _native_executor(tmp_path, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": _TINY_PNG_DATA_URI},
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    uploads = attachment_cache_dir(tmp_path)
    written = list(uploads.iterdir())
    assert len(written) == 1
    # Generated name should have .png extension from the data URI MIME.
    assert written[0].suffix == ".png", (
        f"Expected .png extension, got {written[0].suffix}. "
        "MIME-to-extension mapping may be missing for image/png."
    )


@pytest.mark.asyncio
async def test_enqueue_session_message_materializes_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Steering messages with multimodal content blocks also materialize
    attachments (same path as ``run_turn``).
    """
    from omnigent.harnesses.claude_native.framework_context import consume_prompt_framework_context
    from omnigent.inner.native_attachments import framework_notice_block, resize_notice

    dimensions = {"width": 6000, "height": 4000}
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = _native_executor(
        tmp_path, monkeypatch=monkeypatch, root=tmp_path, submit_hook=False
    )
    accepted = await executor.enqueue_session_message(
        "session-key",
        [
            {
                "type": "input_image",
                "image_url": _TINY_PNG_DATA_URI,
                "filename": "steering_img.png",
            },
            framework_notice_block(dimensions),
            {"type": "input_text", "text": "look at this"},
        ],
    )

    assert accepted is True
    assert len(sent) == 1
    injected = sent[0]["content"]
    assert "steering_img.png" in injected
    assert "look at this" in injected
    assert "downscaled" not in injected
    # File was written to the bridge directory.
    written = list((attachment_cache_dir(tmp_path)).iterdir())
    assert len(written) == 1
    assert written[0].name == "steering_img.png"
    assert await executor.enqueue_session_message("session-key", "follow-up")
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "native-session",
        "prompt": injected,
    }
    assert consume_prompt_framework_context(tmp_path, {**payload, "prompt": "follow-up"}) == ""
    assert consume_prompt_framework_context(tmp_path, payload) == resize_notice(dimensions)


@pytest.mark.asyncio
async def test_run_turn_malformed_data_uri_emits_visible_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An image block with a malformed data URI injects a visible
    could-not-load marker with the text; the turn does not error.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = _native_executor(tmp_path, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,NOT_VALID_BASE64!@#",
                        },
                        {"type": "input_text", "text": "still send this"},
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    # Turn completes — the bad image becomes a marker, text is injected.
    assert events == [TurnComplete(response=None)]
    assert len(sent) == 1
    assert sent[0]["content"] == ("[Attachment attachment could not be loaded]\n\nstill send this")
    # No file written for the malformed URI.
    assert not (attachment_cache_dir(tmp_path)).exists()


@pytest.mark.asyncio
async def test_run_turn_path_traversal_filename_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A filename with path traversal components is stripped to the base name.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = _native_executor(tmp_path, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": _TINY_PNG_DATA_URI,
                            "filename": "../../.bashrc",
                        },
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    uploads = attachment_cache_dir(tmp_path)
    written = list(uploads.iterdir())
    assert len(written) == 1
    assert written[0].name == ".bashrc"
    assert written[0].parent == uploads


@pytest.mark.asyncio
@pytest.mark.parametrize("clear_during_switch", [False, True])
async def test_run_turn_applies_routed_model_before_message_under_one_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clear_during_switch: bool,
) -> None:
    """
    A routed model is switched via ``/model`` BEFORE the message, in order.

    Intelligent routing delivers the picked model on the turn (adapter maps
    ``request.model_override`` -> ``config.model``). The executor must type
    ``/model <routed>`` and then inject the message as one sequence under
    ``_inject_lock`` — never as a separate racing writer. This test records
    the call order across both injectors and asserts ``/model`` lands first,
    then the message, exactly once each. A regression that dropped the switch
    (or ran it concurrently) would fail the ordering assertion.

    The typed argument is the session's alias for the routed catalog id:
    ``/model`` rejects a bare gateway id and silently keeps the old model.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
    bridge_dir = tmp_path / "bridge"
    if clear_during_switch:
        bridge_dir.mkdir()
        (bridge_dir / "bridge.json").write_text('{"active_session_id": "conv_old"}')
        monkeypatch.setenv(REQUEST_SESSION_ID_ENV_VAR, "conv_old")
    calls: list[tuple[str, str]] = []

    def fake_inject_slash_command(
        bridge_dir_arg: Path,
        *,
        command: str,
        timeout_s: float = 30.0,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        """Record the ``/model`` switch keystroke and its auto_confirm flag."""
        del bridge_dir_arg, timeout_s
        calls.append(("slash", command, auto_confirm))
        if clear_during_switch:
            (bridge_dir / "bridge.json").write_text('{"active_session_id": "conv_new"}')

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """Record the message inject."""
        del bridge_dir_arg, timeout_s
        calls.append(("message", content))

    # No ucode profile at launch -> unknown baseline -> the routed model is
    # treated as a change and switched.
    monkeypatch.setattr(claude_native_executor, "read_launch_model", lambda _bridge: None)
    monkeypatch.setattr(
        claude_native_executor,
        "read_model_env",
        lambda _bridge: {"ANTHROPIC_DEFAULT_SONNET_MODEL": "databricks-claude-sonnet-5"},
    )
    monkeypatch.setattr(claude_native_executor, "inject_slash_command", fake_inject_slash_command)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)

    executor = _native_executor(bridge_dir, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "review this function"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model="databricks-claude-sonnet-5"),
        )
    ]

    expected_calls = [
        # auto_confirm=True mirrors the manual picker path so the switch is
        # accepted if the CLI ever pops a confirmation dialog.
        ("slash", "/model sonnet", True),
        ("message", "review this function"),
    ]
    assert calls == (expected_calls[:1] if clear_during_switch else expected_calls)
    if clear_during_switch:
        assert len(events) == 1 and isinstance(events[0], ExecutorError)
        assert events[0].undelivered is True
        assert "no longer active after /clear" in events[0].message
    else:
        assert events == [TurnComplete(response=None)]


@pytest.mark.asyncio
async def test_run_turn_uses_the_custom_model_slot_id_verbatim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A model pinned to the custom picker slot is applied exactly."""
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
    slash_calls: list[str] = []

    def fake_inject_slash_command(
        bridge_dir_arg: Path,
        *,
        command: str,
        timeout_s: float = 30.0,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del bridge_dir_arg, timeout_s, auto_confirm, confirm_hint
        slash_calls.append(command)

    monkeypatch.setattr(claude_native_executor, "read_launch_model", lambda _bridge: None)
    monkeypatch.setattr(
        claude_native_executor,
        "read_model_env",
        lambda _bridge: {
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "databricks-claude-sonnet-4-6",
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "databricks-claude-sonnet-5",
        },
    )
    monkeypatch.setattr(claude_native_executor, "inject_slash_command", fake_inject_slash_command)
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        lambda bridge_dir_arg, *, content, timeout_s=30.0: None,
    )

    executor = _native_executor(tmp_path / "bridge", monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model="databricks-claude-sonnet-5"),
        )
    ]

    assert slash_calls == ["/model databricks-claude-sonnet-5"]
    assert events == [TurnComplete(response=None)]


@pytest.mark.asyncio
async def test_run_turn_types_a_managed_picker_row_verbatim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A routed model of no Claude family is spelled by the pane's picker.

    A workspace-managed picker lists every served model by its own id, and
    ``/model`` takes those verbatim — but no alias pin covers them, so the
    switch was skipped and the turn ran on the launch model.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
    slash_calls: list[str] = []

    def fake_inject_slash_command(
        bridge_dir_arg: Path,
        *,
        command: str,
        timeout_s: float = 30.0,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del bridge_dir_arg, timeout_s, auto_confirm, confirm_hint
        slash_calls.append(command)

    monkeypatch.setattr(claude_native_executor, "read_launch_model", lambda _bridge: None)
    monkeypatch.setattr(
        claude_native_executor,
        "read_model_env",
        lambda _bridge: {
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "system.ai.claude-opus-4-8[1m]",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "system.ai.claude-sonnet-4-6[1m]",
        },
    )
    monkeypatch.setattr(
        claude_native_executor,
        "read_model_picker_values",
        lambda _bridge: ["system.ai.claude-opus-4-8[1m]", "system.ai.glm-5-3"],
    )
    monkeypatch.setattr(claude_native_executor, "inject_slash_command", fake_inject_slash_command)
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        lambda bridge_dir_arg, *, content, timeout_s=30.0: None,
    )

    executor = _native_executor(tmp_path / "bridge", monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model="system.ai.glm-5-3"),
        )
    ]

    assert slash_calls == ["/model system.ai.glm-5-3"]
    assert events == [TurnComplete(response=None)]


@pytest.mark.asyncio
async def test_run_turn_skips_switch_for_untranslatable_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A routed id this session can't spell fails open — message still sent.

    Typing a value ``/model`` doesn't accept leaves the pane on its old
    model while reporting success, so the switch is skipped instead.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
    slash_calls: list[str] = []
    msg_calls: list[str] = []

    def fake_inject_slash_command(
        bridge_dir_arg: Path,
        *,
        command: str,
        timeout_s: float = 30.0,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del bridge_dir_arg, timeout_s, auto_confirm, confirm_hint
        slash_calls.append(command)

    def fake_inject_user_message(
        bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0
    ) -> None:
        del bridge_dir_arg, timeout_s
        msg_calls.append(content)

    monkeypatch.setattr(claude_native_executor, "read_launch_model", lambda _bridge: None)
    # Only opus is pinned, so a sonnet id has no spelling this pane accepts:
    # the bare "sonnet" alias would resolve to a vendor id the gateway rejects.
    monkeypatch.setattr(
        claude_native_executor,
        "read_model_env",
        lambda _bridge: {"ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-4-8"},
    )
    monkeypatch.setattr(claude_native_executor, "inject_slash_command", fake_inject_slash_command)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)

    executor = _native_executor(tmp_path / "bridge", monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model="databricks-claude-sonnet-5"),
        )
    ]

    assert slash_calls == []
    assert msg_calls == ["hello"]
    assert events == [TurnComplete(response=None)]


@pytest.mark.asyncio
async def test_run_turn_skips_switch_when_the_family_pin_drifted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A mismatched family pin must not be spoken as its alias.

    The workspace serves two opus generations and ``opus`` is pinned to the
    newer one, so ``/model opus`` would move the pane off the routed model
    while the transcript claimed it ran.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
    slash_calls: list[str] = []
    msg_calls: list[str] = []

    monkeypatch.setattr(claude_native_executor, "read_launch_model", lambda _bridge: None)
    monkeypatch.setattr(
        claude_native_executor,
        "read_model_env",
        lambda _bridge: {"ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-5"},
    )
    monkeypatch.setattr(
        claude_native_executor,
        "inject_slash_command",
        lambda bridge_dir_arg, *, command, timeout_s=30.0, auto_confirm=False, confirm_hint=None: (
            slash_calls.append(command)
        ),
    )
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        lambda bridge_dir_arg, *, content, timeout_s=30.0: msg_calls.append(content),
    )

    executor = _native_executor(tmp_path / "bridge", monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model="databricks-claude-opus-4-8"),
        )
    ]

    assert slash_calls == []
    assert msg_calls == ["hello"]
    assert events == [TurnComplete(response=None)]


@pytest.mark.asyncio
async def test_run_turn_without_model_override_injects_message_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    No routed model -> no ``/model`` typed, message injected as before.

    A non-routing turn (config.model None) must not touch the model at all —
    typing ``/model`` on every turn would be wrong and noisy.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
    bridge_dir = tmp_path / "bridge"
    slash_calls: list[str] = []
    msg_calls: list[str] = []

    def fake_inject_slash_command(
        bridge_dir_arg: Path,
        *,
        command: str,
        timeout_s: float = 30.0,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del bridge_dir_arg, timeout_s, auto_confirm, confirm_hint
        slash_calls.append(command)

    def fake_inject_user_message(
        bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0
    ) -> None:
        del bridge_dir_arg, timeout_s
        msg_calls.append(content)

    monkeypatch.setattr(claude_native_executor, "inject_slash_command", fake_inject_slash_command)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)

    executor = _native_executor(bridge_dir, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model=None),
        )
    ]

    assert slash_calls == [], f"No /model expected without a routed model; got {slash_calls}."
    assert msg_calls == ["hi"]
    assert events == [TurnComplete(response=None)]


@pytest.mark.asyncio
async def test_run_turn_skips_model_switch_when_already_on_that_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A routed model equal to the pane's launch model types no ``/model``.

    ``read_launch_model`` reports what Claude booted with; if routing picks
    that same model there is nothing to switch, so the executor must inject
    the message only. Prevents a pointless ``/model`` (and the turn churn it
    would add) when the pane is already correct.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
    bridge_dir = tmp_path / "bridge"
    slash_calls: list[str] = []
    msg_calls: list[str] = []

    def fake_inject_slash_command(
        bridge_dir_arg: Path,
        *,
        command: str,
        timeout_s: float = 30.0,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del bridge_dir_arg, timeout_s, auto_confirm, confirm_hint
        slash_calls.append(command)

    def fake_inject_user_message(
        bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0
    ) -> None:
        del bridge_dir_arg, timeout_s
        msg_calls.append(content)

    monkeypatch.setattr(
        claude_native_executor,
        "read_launch_model",
        lambda _bridge: "databricks-claude-opus-4-8",
    )
    monkeypatch.setattr(claude_native_executor, "inject_slash_command", fake_inject_slash_command)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)

    executor = _native_executor(bridge_dir, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model="databricks-claude-opus-4-8"),
        )
    ]

    assert slash_calls == [], f"No /model expected when already on that model; got {slash_calls}."
    assert msg_calls == ["hello"]
    assert events == [TurnComplete(response=None)]


@pytest.mark.asyncio
async def test_a_routed_first_message_switches_the_model_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The replay of a routed first message must not re-issue ``/model``.

    First-message routing blocks the prompt, types the switch itself, then
    replays the prompt with the same ``model_override``. Seeding the baseline
    from ``launch_model`` (written once at bridge prepare) made the replay
    compare against the PRE-switch model and type a second, redundant
    ``/model`` — visible in the transcript ahead of the very first turn.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
    bridge_dir = tmp_path / "bridge"
    slash_calls: list[str] = []
    msg_calls: list[str] = []

    def fake_inject_slash_command(bridge_dir_arg: Path, *, command: str, **kwargs: object) -> None:
        del bridge_dir_arg, kwargs
        slash_calls.append(command)

    def fake_inject_user_message(
        bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0
    ) -> None:
        del bridge_dir_arg, timeout_s
        msg_calls.append(content)

    # The launch model is stale — the turn router already moved the pane, and
    # only the statusLine capture knows it.
    monkeypatch.setattr(
        claude_native_executor,
        "read_launch_model",
        lambda _bridge: "databricks-claude-sonnet-5",
    )
    monkeypatch.setattr(
        claude_native_executor,
        "read_claude_status_model",
        lambda _bridge: "claude-opus-4-8",
    )
    monkeypatch.setattr(claude_native_executor, "inject_slash_command", fake_inject_slash_command)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)

    executor = _native_executor(bridge_dir, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model="databricks-claude-opus-4-8"),
        )
    ]

    assert slash_calls == [], (
        f"The router already switched the pane; the replay must type nothing. Got {slash_calls}."
    )
    assert msg_calls == ["hello"]
    assert events == [TurnComplete(response=None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True])
async def test_run_turn_reaps_tmux_before_reporting_prompt_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    replacement: bool,
) -> None:
    """A readiness timeout reaps its own pane and preserves a replacement."""
    bridge_dir = tmp_path / "bridge"
    killed: list[Path] = []

    def fail_inject(bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0) -> None:
        del content, timeout_s
        if replacement:
            claude_bridge.write_tmux_target(
                bridge_dir_arg,
                socket_path=tmp_path / "native.sock",
                tmux_target="main",
                launch_id="e" * 32,
                instance_id="f" * 32,
            )
        raise ClaudePromptTimeout("terminal did not become ready")

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(
        claude_native_executor,
        "kill_session",
        lambda bridge_dir_arg, *, timeout_s, expected_close=False: killed.append(bridge_dir_arg),
    )

    executor = _native_executor(bridge_dir, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
        )
    ]

    assert killed == ([] if replacement else [bridge_dir])
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)


@pytest.mark.asyncio
async def test_run_turn_marks_its_timeout_reap_as_an_expected_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The runner must read the reap as Omnigent's close, not a terminal crash.

    The pane is alive when the readiness wait times out, and the reap ends it
    moments before the runner would otherwise report the exit as a failure.
    The turn's own error is the one failure the person should see.
    """
    kill_flags: list[bool] = []

    def fail_inject(bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0) -> None:
        del bridge_dir_arg, content, timeout_s
        raise ClaudePromptTimeout("terminal did not become ready")

    def record_kill(
        bridge_dir_arg: Path, *, timeout_s: float, expected_close: bool = False
    ) -> None:
        del bridge_dir_arg, timeout_s
        kill_flags.append(expected_close)

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(claude_native_executor, "kill_session", record_kill)

    executor = _native_executor(tmp_path / "bridge", monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
        )
    ]

    assert kill_flags == [True]
    assert isinstance(events[0], ExecutorError)


@pytest.mark.asyncio
async def test_run_turn_does_not_mark_a_pane_that_exited_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A pane that already exited was not closed by Omnigent, so its exit still counts."""
    kill_flags: list[bool] = []

    def fail_inject(bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0) -> None:
        del bridge_dir_arg, content, timeout_s
        raise ClaudeTerminalExited("terminal exited", exit_status="1")

    def record_kill(
        bridge_dir_arg: Path, *, timeout_s: float, expected_close: bool = False
    ) -> None:
        del bridge_dir_arg, timeout_s
        kill_flags.append(expected_close)

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(claude_native_executor, "kill_session", record_kill)

    executor = _native_executor(tmp_path / "bridge", monkeypatch=monkeypatch, root=tmp_path)
    [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
        )
    ]

    assert kill_flags == [False]


@pytest.mark.asyncio
async def test_run_turn_keeps_the_pane_when_a_terminal_dialog_blocks_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A dialog holding the terminal fails the turn without reaping the pane.

    The person answers the dialog in the embedded terminal and resends, so
    killing the pane would destroy the very thing they need. Only readiness
    timeouts reap. The gate raised before anything was typed, so the error
    is classified undelivered and the sender's queued copy is settled.
    """
    bridge_dir = tmp_path / "bridge"
    killed: list[Path] = []

    def fail_inject(bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0) -> None:
        del bridge_dir_arg, content, timeout_s
        raise ClaudeTerminalDialog(
            "Claude Code is waiting for an answer in its terminal "
            "(New MCP server found in this project: e2e-noop), so the message was not delivered."
        )

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(
        claude_native_executor,
        "kill_session",
        lambda bridge_dir_arg, *, timeout_s, expected_close=False: killed.append(bridge_dir_arg),
    )

    executor = _native_executor(bridge_dir, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
        )
    ]

    assert killed == []
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "waiting for an answer" in events[0].message
    assert events[0].undelivered is True


@pytest.mark.asyncio
async def test_run_turn_reports_a_refused_routed_model_switch_as_undelivered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A ``/model`` switch refused at an occupied input box fails the turn as undelivered.

    ``inject_slash_command`` raises :class:`ClaudeTerminalDialog` instead of
    typing into a surface Escape cannot clear (wrapper output before the
    input box mounts, a hintless confirmation). The routed switch runs before
    the message, so nothing has reached Claude Code: the message must not be
    injected behind it, the pane stays alive for the person to clear the
    surface, and the error is classified undelivered so the sender's queued
    copy is settled rather than lost on reload.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
    bridge_dir = tmp_path / "bridge"
    messages: list[str] = []
    killed: list[Path] = []

    def refuse_slash_command(bridge_dir_arg: Path, *, command: str, **kwargs: object) -> None:
        """Refuse the switch the way the bridge does at a surface Escape cannot clear."""
        del bridge_dir_arg, kwargs
        raise ClaudeTerminalDialog(
            f"Claude Code's input box is occupied by an overlay, so the command was "
            f"not sent ({command})."
        )

    def record_message(bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0) -> None:
        """Record a message inject that must never happen."""
        del bridge_dir_arg, timeout_s
        messages.append(content)

    monkeypatch.setattr(claude_native_executor, "read_launch_model", lambda _bridge: None)
    monkeypatch.setattr(
        claude_native_executor,
        "read_model_env",
        lambda _bridge: {"ANTHROPIC_DEFAULT_SONNET_MODEL": "databricks-claude-sonnet-5"},
    )
    monkeypatch.setattr(claude_native_executor, "inject_slash_command", refuse_slash_command)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", record_message)
    monkeypatch.setattr(
        claude_native_executor,
        "kill_session",
        lambda bridge_dir_arg, *, timeout_s, expected_close=False: killed.append(bridge_dir_arg),
    )

    executor = _native_executor(bridge_dir, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "review this function"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model="databricks-claude-sonnet-5"),
        )
    ]

    assert messages == [], f"No message may follow a refused switch; got {messages}."
    assert killed == []
    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ExecutorError)
    assert "occupied by an overlay" in error.message
    assert error.undelivered is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("submitted", "refusal"),
    [
        (False, "pending_prompt"),
        (True, "pending_prompt"),
        (True, "draft_persisted"),
        (True, "capture_lost"),
        (False, "missing_target"),
    ],
    ids=["before_submit", "after_submit", "draft_not_accepted", "capture_lost", "missing_target"],
)
async def test_run_turn_preserves_refused_input_but_keeps_post_submit_failures_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    submitted: bool,
    refusal: str,
) -> None:
    """
    Refusals preserve input; a prompt after Enter may belong to its accepted turn.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    pane = "❯ "
    enters = 0
    prompt_pending = False
    killed: list[Path] = []

    def run_tmux(socket_path: str, *args: str) -> None:
        nonlocal pane, enters, prompt_pending
        del socket_path
        if args[-1] == "Space":
            pane = "──────\n❯ \n──────\n"
        elif args[0] == "send-keys" and "-l" in args:
            pane = "❯ "
        elif args[0] == "paste-buffer":
            pane = "❯ review this function"
            prompt_pending = not submitted
        elif args[-1] == "Enter":
            # The draft stays visible, so submit verification looks to retry.
            enters += 1
            prompt_pending = refusal == "pending_prompt"
            if refusal == "capture_lost":
                pane = ""

    if refusal == "missing_target":
        wait_for_target = claude_bridge._wait_for_tmux_info
        monkeypatch.setattr(
            claude_bridge,
            "_wait_for_tmux_info",
            lambda directory, **_kwargs: wait_for_target(directory, timeout_s=0.0),
        )
    else:
        monkeypatch.setattr(
            claude_bridge,
            "_wait_for_tmux_info",
            lambda *_a, **_k: {"socket_path": "/unused/socket", "tmux_target": "main"},
        )
    monkeypatch.setattr(claude_bridge, "_restore_occupied_input", lambda *_a, **_k: None)
    monkeypatch.setattr(claude_bridge, "_wait_for_claude_prompt_ready", lambda *_a, **_k: None)
    monkeypatch.setattr(claude_bridge, "_run_tmux", run_tmux)
    monkeypatch.setattr(claude_bridge, "_capture_pane", lambda *_a: pane)
    monkeypatch.setattr(claude_bridge, "_has_approval_wait", lambda _bridge: prompt_pending)
    monkeypatch.setattr(claude_bridge, "_PASTE_SETTLE_S", 0.0)
    monkeypatch.setattr(claude_bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(claude_bridge, "_SUBMIT_RETRY_INTERVAL_S", 0.0)
    monkeypatch.setattr(claude_bridge, "_SUBMIT_VERIFY_TIMEOUT_S", 0.01)
    monkeypatch.setattr(
        claude_native_executor,
        "kill_session",
        lambda bridge_dir_arg, *, timeout_s, expected_close=False: killed.append(bridge_dir_arg),
    )

    executor = _native_executor(bridge_dir, monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "review this function"}],
            tools=[],
            system_prompt="",
        )
    ]

    if refusal == "draft_persisted":
        assert enters >= 1
        assert pane == "❯ review this function"
    else:
        assert enters == int(submitted)
    assert killed == []
    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ExecutorError)
    expected_detail = {
        "pending_prompt": "waiting for an explicit answer",
        "draft_persisted": "draft is still in the input box",
        "capture_lost": "delivery could not be confirmed",
        "missing_target": "tmux target is not advertised",
    }[refusal]
    assert expected_detail in error.message
    assert error.undelivered is (refusal in {"draft_persisted", "missing_target"} or not submitted)


@pytest.mark.asyncio
@pytest.mark.parametrize("before_session_start", [False, True])
async def test_run_turn_keeps_the_pane_when_a_sign_in_prompt_blocks_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    before_session_start: bool,
) -> None:
    """
    A launcher sign-in prompt fails the turn with the link and leaves the pane.

    The person signs in from the card's link and the launcher continues into
    Claude Code on its own; reaping the pane would destroy that prompt. The
    error carries the semantic code, headline and next step.
    """
    bridge_dir = tmp_path / "bridge"
    killed: list[Path] = []
    injections: list[str] = []

    def sign_in_pending() -> ClaudeSignInPending:
        return ClaudeSignInPending(
            "Claude Code is waiting for a sign-in in this session's terminal, "
            "so the message was not delivered.",
            title="Claude Code can't start until you sign in to Databricks",
            remediation="Open https://signin.example.com/device and enter code HQ7M-2KPD.",
        )

    def fail_inject(bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0) -> None:
        del bridge_dir_arg, timeout_s
        injections.append(content)
        raise sign_in_pending()

    def observe_startup(*_args: Any, **kwargs: Any) -> None:
        assert kwargs["read_only"] is True
        raise sign_in_pending()

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(
        claude_native_executor,
        "kill_session",
        lambda bridge_dir_arg, *, timeout_s, expected_close=False: killed.append(bridge_dir_arg),
    )

    executor = _native_executor(
        bridge_dir, monkeypatch=monkeypatch, root=tmp_path, native_ready=not before_session_start
    )
    if before_session_start:
        monkeypatch.setattr(
            claude_native_executor, "_wait_for_claude_prompt_ready", observe_startup
        )
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
        )
    ]

    assert killed == []
    assert injections == ([] if before_session_start else ["hi"])
    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ExecutorError)
    assert error.code == "databricks_sign_in_pending"
    assert error.title == "Claude Code can't start until you sign in to Databricks"
    assert error.remediation is not None
    assert "HQ7M-2KPD" in error.remediation
    # The gate failed before the prompt was typed: the message never reached Claude Code.
    assert error.undelivered is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_status", "expect_error_level"),
    [("0", False), ("1", True), ("137", True), (None, True)],
)
async def test_run_turn_logs_a_closed_terminal_below_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    exit_status: str | None,
    expect_error_level: bool,
) -> None:
    """
    Closing Claude Code is not an Omnigent defect, so it is not an ERROR.

    Claude Code exits 0 on ``/quit`` or a closed window. Every turn sent
    afterwards must still fail — the pane is gone — but logging that as an
    ERROR reports the person's own teardown as a mid-session failure. A
    pane that died on its own (any other wait-status, or none recorded)
    keeps the ERROR and its traceback.
    """
    bridge_dir = tmp_path / "bridge"

    def fail_inject(bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0) -> None:
        del bridge_dir_arg, content, timeout_s
        raise ClaudeTerminalExited(
            "The Claude Code terminal has exited, so the message was not delivered.",
            exit_status=exit_status,
        )

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(
        claude_native_executor,
        "kill_session",
        lambda bridge_dir_arg, *, timeout_s, expected_close=False: None,
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.inner.claude_native_executor"):
        events = [
            event
            async for event in _native_executor(
                bridge_dir, monkeypatch=monkeypatch, root=tmp_path
            ).run_turn(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                system_prompt="",
            )
        ]

    # The turn still fails: the pane is gone either way.
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert events[0].undelivered is True

    records = [r for r in caplog.records if "terminal exited" in r.getMessage()]
    assert len(records) == 1
    assert (records[0].levelno == logging.ERROR) is expect_error_level
    # A traceback only earns its place when something actually broke.
    assert bool(records[0].exc_info) is expect_error_level


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["message", "steering", "model"])
@pytest.mark.parametrize("watchdog", [False, True])
@pytest.mark.parametrize("replacement", [None, "conversation", "same-address-terminal"])
async def test_cancelled_delivery_drains_worker_before_unlocking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    delivery: str,
    watchdog: bool,
    replacement: str | None,
) -> None:
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)
    bridge_dir = claude_bridge.prepare_bridge_dir("conversation-original", workspace=tmp_path)
    claude_bridge.write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/sock"),
        tmux_target="main",
        launch_id="c" * 32,
        instance_id="d" * 32,
    )
    claude_bridge.record_hook_event(
        bridge_dir, {"hook_event_name": "SessionStart", "session_id": "native-session"}
    )
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    commands: list[tuple[str, ...]] = []
    reaped: list[Path] = []

    def capture(socket_path: str, tmux_target: str) -> str:
        started.set()
        assert release.wait(5), "test did not release the in-flight capture"
        return "────────────────\n❯ \n────────────────\n"

    def inject(*args: Any, **kwargs: Any) -> None:
        try:
            claude_bridge._wait_for_claude_prompt_ready("/tmp/sock", "main", timeout_s=30)
            claude_bridge._run_tmux("/tmp/sock", "send-keys", "late message", "Enter")
        finally:
            finished.set()

    def reap(bridge_dir: Path, *, timeout_s: float, expected_close: bool = False) -> None:
        assert finished.is_set()
        claude_bridge._check_injection_cancelled()
        reaped.append(bridge_dir)

    def run(cmd: list[str], **kwargs: Any) -> Any:
        commands.append(tuple(cmd))
        raise AssertionError("cancelled delivery sent keystrokes")

    monkeypatch.setattr(claude_bridge, "_capture_pane", capture)
    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", inject)
    monkeypatch.setattr(claude_native_executor, "inject_slash_command", inject)
    monkeypatch.setattr(claude_native_executor, "kill_session", reap)
    executor = _native_executor(bridge_dir, monkeypatch=monkeypatch, root=tmp_path)
    monkeypatch.setattr(
        executor, "_model_command_arg", lambda model: "sonnet" if delivery == "model" else None
    )

    async def deliver() -> None:
        async with asyncio.timeout(0.1 if watchdog else 5):
            if delivery == "steering":
                await executor.enqueue_session_message("session", "hello")
            else:
                async for _event in executor.run_turn(
                    messages=[{"role": "user", "content": "hello"}],
                    tools=[],
                    system_prompt="",
                ):
                    raise AssertionError("cancelled turn emitted a completion")

    task = asyncio.create_task(deliver())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        if not watchdog:
            task.cancel()
        async with asyncio.timeout(2):
            while not task.cancelling():
                await asyncio.sleep(0.005)
        await asyncio.sleep(0)
        assert not task.done()
        assert executor._inject_lock.locked()
        if not watchdog:
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        if replacement == "conversation":
            claude_bridge.write_active_session_id(bridge_dir, "conversation-new")
        elif replacement == "same-address-terminal":
            claude_bridge.write_tmux_target(
                bridge_dir, socket_path=Path("/tmp/sock"), tmux_target="main"
            )
        release.set()
        await asyncio.wait({task}, timeout=2)
        assert task.done(), "cancelled delivery did not finish after its capture was released"
        with pytest.raises(TimeoutError if watchdog else asyncio.CancelledError):
            task.result()
        assert finished.is_set()
        assert not executor._inject_lock.locked()
        assert reaped == ([bridge_dir] if replacement is None else [])
        assert commands == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait({task})
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            task.result()


@pytest.mark.asyncio
async def test_run_turn_does_not_reap_unrelated_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only the readiness failure that terminalizes the turn triggers cleanup."""
    killed: list[Path] = []

    def fail_inject(bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0) -> None:
        del bridge_dir_arg, content, timeout_s
        raise RuntimeError("tmux send-keys failed")

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(
        claude_native_executor,
        "kill_session",
        lambda *args, **kwargs: killed.append(args[0]),
    )

    executor = _native_executor(tmp_path / "bridge", monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
        )
    ]

    assert killed == []
    assert isinstance(events[0], ExecutorError)
    assert events[0].message == "tmux send-keys failed"
    assert events[0].undelivered is False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ClaudePromptTimeout, ClaudeTerminalExited])
async def test_run_turn_reports_reap_failure_with_prompt_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: type[ClaudePromptTimeout],
) -> None:
    """A failed hard-stop remains visible beside the delivery error."""

    def fail_inject(bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0) -> None:
        del bridge_dir_arg, content, timeout_s
        raise failure("terminal did not become ready")

    def fail_kill(bridge_dir_arg: Path, *, timeout_s: float, expected_close: bool = False) -> None:
        del bridge_dir_arg, timeout_s, expected_close
        raise RuntimeError("tmux kill failed")

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(claude_native_executor, "kill_session", fail_kill)

    executor = _native_executor(tmp_path / "bridge", monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
        )
    ]

    assert isinstance(events[0], ExecutorError)
    assert "terminal did not become ready" in events[0].message
    assert "Cleanup also failed: tmux kill failed" in events[0].message
    assert events[0].undelivered is True


@pytest.mark.asyncio
async def test_run_turn_ignores_missing_tmux_during_timeout_reap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A pane that exited during readiness polling needs no cleanup error."""

    def fail_inject(bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0) -> None:
        del bridge_dir_arg, content, timeout_s
        raise ClaudePromptTimeout("terminal did not become ready")

    def absent_kill(
        bridge_dir_arg: Path, *, timeout_s: float, expected_close: bool = False
    ) -> None:
        del bridge_dir_arg, timeout_s, expected_close
        raise TmuxSessionNotAdvertised("not advertised")

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(claude_native_executor, "kill_session", absent_kill)

    executor = _native_executor(tmp_path / "bridge", monkeypatch=monkeypatch, root=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
        )
    ]

    assert isinstance(events[0], ExecutorError)
    assert events[0].message == "terminal did not become ready"


@pytest.mark.asyncio
async def test_repeated_local_command_without_submit_hook_advances_each_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.harnesses.claude_native.framework_context import PROMPT_FRAMEWORK_CONTEXT_DIR
    from tests._helpers.polling import poll_until_async

    command = "/model example"
    transcript = tmp_path / "native.jsonl"
    transcript.write_text('{"type":"system","content":"prefix"}\n')
    injected: list[str] = []
    killed = Mock()

    def append_output(number: int) -> None:
        with transcript.open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "local_command",
                        "sessionId": "native-session",
                        "uuid": f"stdout-{number}",
                        "commandRun": {"command": "model", "args": "example"},
                        "content": "<local-command-stdout>Done.</local-command-stdout>",
                    }
                )
                + "\n"
            )

    def inject_local(directory: Path, *, content: str) -> None:
        assert directory == tmp_path
        injected.append(content)
        number = len(injected)
        with transcript.open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "local_command",
                        "sessionId": "native-session",
                        "uuid": f"echo-{number}",
                        "content": "<command-name>/model</command-name>"
                        "<command-args>example</command-args>",
                    }
                )
                + "\n"
            )
        if number > 1:
            append_output(number)

    def checkpoint_offset() -> int:
        records = list((tmp_path / PROMPT_FRAMEWORK_CONTEXT_DIR).glob("*.json"))
        assert len(records) == 1
        return json.loads(records[0].read_text())["submit_boundary"]["byte_offset"]

    monkeypatch.setattr(claude_native_executor, "inject_user_message", inject_local)
    monkeypatch.setattr(claude_native_executor, "kill_session", killed)
    monkeypatch.setattr(claude_native_executor, "_MESSAGE_HOLD_POLL_S", 0.005)
    monkeypatch.setattr(claude_native_executor, "_MESSAGE_HOLD_S", 2.0)
    executor = _native_executor(
        tmp_path, monkeypatch=monkeypatch, root=tmp_path, submit_hook=False
    )
    claude_bridge.record_hook_event(
        tmp_path,
        {
            "hook_event_name": "SessionStart",
            "session_id": "native-session",
            "transcript_path": str(transcript),
        },
    )
    assert await executor.enqueue_session_message("session-key", command)
    first_offset = checkpoint_offset()
    second = asyncio.create_task(executor.enqueue_session_message("session-key", command))
    try:
        await poll_until_async(lambda: executor._holding_message, timeout_s=1, interval_s=0.001)
        assert injected == [command]
        append_output(1)
        assert await asyncio.wait_for(second, timeout=3)
    finally:
        if not second.done():
            second.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await second
    second_offset = checkpoint_offset()
    assert second_offset > first_offset
    assert await asyncio.wait_for(
        executor.enqueue_session_message("session-key", command), timeout=3
    )
    assert checkpoint_offset() > second_offset
    assert injected == [command, command, command]
    killed.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_notice", [False, True], ids=["plain", "required-context"])
async def test_ui_only_help_does_not_wait_for_a_prompt_hook_or_drop_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, with_notice: bool
) -> None:
    from omnigent.harnesses.claude_native.framework_context import PROMPT_FRAMEWORK_CONTEXT_DIR
    from omnigent.inner.native_attachments import framework_notice_block

    sent: list[dict[str, Any]] = []
    models: list[str] = []
    killed = Mock()
    monkeypatch.setattr(claude_native_executor, "inject_user_message", _stub_inject(sent))
    monkeypatch.setattr(claude_native_executor, "kill_session", killed)
    monkeypatch.setattr(claude_native_executor, "read_launch_model", lambda _bridge: None)
    monkeypatch.setattr(
        claude_native_executor,
        "read_model_env",
        lambda _bridge: {"ANTHROPIC_DEFAULT_SONNET_MODEL": "gateway-model"},
    )
    monkeypatch.setattr(
        claude_native_executor,
        "inject_slash_command",
        lambda *_args, command, **_kwargs: models.append(command),
    )
    monkeypatch.setattr(claude_native_executor, "_MESSAGE_HOLD_S", 0.1)
    monkeypatch.setattr(claude_native_executor, "_MESSAGE_HOLD_POLL_S", 0.005)
    executor = _native_executor(
        tmp_path, monkeypatch=monkeypatch, root=tmp_path, submit_hook=False
    )
    content: Any = "/help"
    if with_notice:
        content = [
            {"type": "input_text", "text": "/help"},
            framework_notice_block({"width": 6000, "height": 4000}),
        ]
        assert not await executor.enqueue_session_message("session-key", content)
    else:
        for _ in range(3):
            assert await executor.enqueue_session_message("session-key", content)
    events = [
        event
        async for event in executor.run_turn(
            [{"role": "user", "content": content}],
            [],
            "",
            config=ExecutorConfig(model="gateway-model") if with_notice else None,
        )
    ]
    if with_notice:
        assert len(events) == 1 and isinstance(events[0], ExecutorError)
        assert events[0].undelivered
        assert "Open Help in the terminal" in events[0].message
        assert sent == []
    else:
        assert events == [TurnComplete(response=None)]
        assert [entry["content"] for entry in sent] == ["/help"] * 4
    assert models == []
    assert claude_bridge.read_transcript_path(tmp_path) is None
    assert claude_bridge.read_hook_events_since(tmp_path, 0)[1] == ["SessionStart"]
    assert not (tmp_path / PROMPT_FRAMEWORK_CONTEXT_DIR).exists()
    killed.assert_not_called()
