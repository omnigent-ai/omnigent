"""Tests for the native Claude Code bridge executor."""

from __future__ import annotations

import asyncio
import base64
import threading
from pathlib import Path
from typing import Any

import pytest

from omnigent.claude_native_bridge import REQUEST_SESSION_ID_ENV_VAR
from omnigent.inner import claude_native_executor
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.executor import ExecutorError, TurnComplete

# Minimal valid 1x1 white PNG used for multimodal attachment tests.
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
)
_TINY_PNG_DATA_URI = f"data:image/png;base64,{_TINY_PNG_B64}"
_TINY_PNG_BYTES = base64.b64decode(_TINY_PNG_B64)


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

    executor = ClaudeNativeExecutor(bridge_dir)
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

    executor = ClaudeNativeExecutor(bridge_dir)
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
async def test_run_turn_rejects_stale_session_after_clear(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "old tab message"}],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "no longer active after /clear" in events[0].message


@pytest.mark.asyncio
async def test_enqueue_session_message_rejects_stale_session_after_clear(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    executor = ClaudeNativeExecutor(tmp_path)
    injected = await executor.enqueue_session_message(
        session_key="main",
        content="old steering",
    )

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

    executor = ClaudeNativeExecutor(tmp_path)
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

    executor = ClaudeNativeExecutor(tmp_path)

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

    executor = ClaudeNativeExecutor(tmp_path)
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
    uploads = tmp_path / "uploads"
    written = list(uploads.iterdir())
    # Exactly 1 file — the materialized PNG.
    assert len(written) == 1, (
        f"Expected 1 written file, got {len(written)}. If 0, the attachment was not materialized."
    )
    assert written[0].name == "screenshot.png"
    # Byte-level check: decoded content matches the original PNG.
    assert written[0].read_bytes() == _TINY_PNG_BYTES


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

    executor = ClaudeNativeExecutor(tmp_path)
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
async def test_run_turn_unresolved_file_id_skipped_gracefully(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An ``input_image`` block with only a ``file_id`` (content resolver
    did not run) is skipped. The text portion is still injected.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = ClaudeNativeExecutor(tmp_path)
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
    # The unresolved image block is skipped; only text survives.
    assert sent[0]["content"] == "analyze this"
    # No uploads directory created — nothing to materialize.
    assert not (tmp_path / "uploads").exists()


@pytest.mark.asyncio
async def test_run_turn_dedup_same_filename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Two image blocks with the same filename produce distinct files
    (the second gets a unique suffix to avoid overwriting the first).
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    image_block = {
        "type": "input_image",
        "image_url": _TINY_PNG_DATA_URI,
        "filename": "dup.png",
    }
    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [image_block, image_block],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    uploads = tmp_path / "uploads"
    written = sorted(uploads.iterdir())
    # Two distinct files, not one overwritten file.
    assert len(written) == 2, (
        f"Expected 2 files (dedup suffix), got {len(written)}. "
        "If 1, the second image overwrote the first."
    )
    # Both contain the same PNG bytes.
    for f in written:
        assert f.read_bytes() == _TINY_PNG_BYTES


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

    executor = ClaudeNativeExecutor(tmp_path)
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
    uploads = tmp_path / "uploads"
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
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = ClaudeNativeExecutor(tmp_path)
    accepted = await executor.enqueue_session_message(
        "session-key",
        [
            {
                "type": "input_image",
                "image_url": _TINY_PNG_DATA_URI,
                "filename": "steering_img.png",
            },
            {"type": "input_text", "text": "look at this"},
        ],
    )

    assert accepted is True
    assert len(sent) == 1
    injected = sent[0]["content"]
    assert "steering_img.png" in injected
    assert "look at this" in injected
    # File was written to the bridge directory.
    written = list((tmp_path / "uploads").iterdir())
    assert len(written) == 1
    assert written[0].name == "steering_img.png"


@pytest.mark.asyncio
async def test_run_turn_malformed_data_uri_skipped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An image block with a malformed data URI is skipped gracefully.
    The text portion is still injected without error.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = ClaudeNativeExecutor(tmp_path)
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

    # Turn completes — the bad image is skipped, text is injected.
    assert events == [TurnComplete(response=None)]
    assert len(sent) == 1
    assert sent[0]["content"] == "still send this"
    # No file written for the malformed URI.
    assert not (tmp_path / "uploads").exists()


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

    executor = ClaudeNativeExecutor(tmp_path)
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
    uploads = tmp_path / "uploads"
    written = list(uploads.iterdir())
    assert len(written) == 1
    assert written[0].name == ".bashrc"
    assert written[0].parent == uploads
