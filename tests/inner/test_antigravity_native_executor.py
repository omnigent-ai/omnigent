"""Tests for the native Antigravity (agy) executor bridge (web-turn injection).

These pin the write path: a web/mobile turn is typed into the running agy TUI via
tmux send-keys (``inject_user_message_via_tui``, mocked here), and agy's reply is
mirrored back by the transcript forwarder — so the executor yields a
``TurnComplete`` with no text rather than fabricating a reply. Delivery is the
TUI for EVERY turn (not connect-RPC, which agy logs as a ``SYSTEM_MESSAGE`` the
forwarder would not mirror), exactly like the claude native bridge. The tmux
delivery itself is exercised in ``test_antigravity_native_bridge``; here it is
stubbed so the tests assert the executor's wiring (what content it delivers, how
it confirms the first turn, and how it maps success/failure to events).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import omnigent.inner.antigravity_native_executor as executor_mod
from omnigent.antigravity_native_bridge import (
    AntigravityNativeBridgeState,
    write_bridge_state,
)
from omnigent.inner.antigravity_native_executor import AntigravityNativeExecutor
from omnigent.inner.executor import ExecutorError, ExecutorEvent, TurnComplete

_CONVERSATION_ID = "90468e33-38c3-4e48-ae9f-03c843196227"
_PLACEHOLDER_ID = "agy_conv_placeholder123"


def _executor(tmp_path: Path) -> AntigravityNativeExecutor:
    """
    Build an executor with an explicit bridge dir (no env needed).

    :param tmp_path: Pytest temporary directory used as the bridge dir.
    :returns: A configured :class:`AntigravityNativeExecutor`.
    """
    return AntigravityNativeExecutor(bridge_dir=tmp_path)


def _seed_state(tmp_path: Path, *, conversation_id: str = _CONVERSATION_ID) -> None:
    """
    Write bridge state the executor will read before delivering.

    :param tmp_path: Bridge directory.
    :param conversation_id: agy conversation id to record (a real id, or an
        ``agy_conv_*`` placeholder to model a fresh, not-yet-discovered session).
    :returns: None.
    """
    write_bridge_state(
        tmp_path,
        AntigravityNativeBridgeState(session_id="conv_test", conversation_id=conversation_id),
    )


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """
    Stub the tmux TUI delivery, recording what the executor types.

    :param monkeypatch: pytest monkeypatch fixture.
    :returns: A dict whose ``calls`` lists each delivery ``{bridge_dir, content}``.
        Set ``raise`` to a ``RuntimeError`` to model a delivery failure, or
        ``after`` to a callback ``(bridge_dir) -> None`` run post-delivery (e.g.
        to simulate the forwarder persisting agy's discovered conversation id).
    """
    rec: dict[str, object] = {"calls": [], "raise": None, "after": None}

    def _inject(bridge_dir: Path, *, content: str, timeout_s: float = 30.0) -> None:
        del timeout_s
        calls = rec["calls"]
        assert isinstance(calls, list)
        calls.append({"bridge_dir": Path(bridge_dir), "content": content})
        if rec["raise"] is not None:
            raise rec["raise"]
        after = rec["after"]
        if after is not None:
            after(Path(bridge_dir))

    monkeypatch.setattr(executor_mod, "inject_user_message_via_tui", _inject)
    return rec


def _only_content(rec: dict[str, object]) -> list[str]:
    """Return the content of each recorded delivery, in order."""
    calls = rec["calls"]
    assert isinstance(calls, list)
    return [c["content"] for c in calls]


async def _run(executor: AntigravityNativeExecutor, text: str) -> list[ExecutorEvent]:
    """
    Drive ``run_turn`` with a single user message and collect its events.

    :param executor: Executor under test.
    :param text: User message text.
    :returns: The yielded executor events.
    """
    return [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": text}],
            tools=[],
            system_prompt="",
        )
    ]


# ---------------------------------------------------------------------------
# capability flags
# ---------------------------------------------------------------------------


def test_does_not_support_streaming(tmp_path: Path) -> None:
    """
    ``supports_streaming`` is ``False``.

    Assistant output is posted by the transcript forwarder, not streamed by the
    executor, so it must report no streaming or the workflow would await chunks
    that never come.
    """
    assert _executor(tmp_path).supports_streaming() is False


def test_supports_live_message_queue(tmp_path: Path) -> None:
    """
    ``supports_live_message_queue`` is ``True``.

    agy queues a mid-turn send-keys paste as the next message, so the executor
    advertises live steering — the server routes mid-turn web messages to
    ``enqueue_session_message``.
    """
    assert _executor(tmp_path).supports_live_message_queue() is True


# ---------------------------------------------------------------------------
# run_turn — delivery
# ---------------------------------------------------------------------------


def test_run_turn_delivers_via_tui_and_completes(
    tmp_path: Path, delivered: dict[str, object]
) -> None:
    """
    ``run_turn`` types the user text into the agy TUI and yields a text-less TurnComplete.

    A known (non-placeholder) conversation id needs no first-turn confirmation:
    the executor delivers the latest user text and yields ``TurnComplete`` with
    ``response=None`` — the forwarder mirrors agy's actual reply, so fabricating
    text here would duplicate it.
    """
    _seed_state(tmp_path)
    events = asyncio.run(_run(_executor(tmp_path), "what is 2+2?"))
    assert _only_content(delivered) == ["what is 2+2?"]
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)
    assert events[0].response is None


def test_run_turn_flattens_content_blocks(tmp_path: Path, delivered: dict[str, object]) -> None:
    """
    Content-block user messages are flattened to text before delivery.

    A web turn arrives as ``input_text`` blocks; the executor must join their
    text (and drop image/file blocks tmux input cannot carry) so agy receives the
    typed prompt.
    """
    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "line one"},
                            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                            {"type": "input_text", "text": "line two"},
                        ],
                    }
                ],
                tools=[],
                system_prompt="",
            )
        ]

    events = asyncio.run(_drive())
    assert _only_content(delivered) == ["line one\nline two"]
    assert isinstance(events[0], TurnComplete)


def test_run_turn_uses_latest_user_message(tmp_path: Path, delivered: dict[str, object]) -> None:
    """
    Only the latest user message is delivered (history is not replayed).

    agy already holds the conversation history; re-sending older turns would
    duplicate them. The executor must pick the most recent user message.
    """
    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[
                    {"role": "user", "content": "old question"},
                    {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "new question"},
                ],
                tools=[],
                system_prompt="",
            )
        ]

    asyncio.run(_drive())
    assert _only_content(delivered) == ["new question"]


def test_run_turn_no_user_text_errors(tmp_path: Path, delivered: dict[str, object]) -> None:
    """
    A turn with no user text yields an ExecutorError without delivering.

    Guards against typing an empty message into agy; there is nothing to send, so
    the executor reports an error instead.
    """
    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[{"role": "assistant", "content": "only assistant"}],
                tools=[],
                system_prompt="",
            )
        ]

    events = asyncio.run(_drive())
    assert _only_content(delivered) == []
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)


# ---------------------------------------------------------------------------
# run_turn — first-turn confirmation
# ---------------------------------------------------------------------------


def test_run_turn_first_turn_waits_for_discovered_id(
    tmp_path: Path, delivered: dict[str, object]
) -> None:
    """
    On a placeholder (fresh) session, the first turn confirms agy registered it.

    agy mints its real conversation id only after processing the first turn, so
    after delivery the executor waits for the forwarder to overwrite the
    ``agy_conv_*`` placeholder with the real id (modeled here by the delivery
    hook). Its appearance confirms the turn registered, and the executor
    completes.
    """
    _seed_state(tmp_path, conversation_id=_PLACEHOLDER_ID)
    # Simulate the forwarder discovering + persisting agy's real id post-delivery.
    delivered["after"] = lambda bd: _seed_state(bd, conversation_id=_CONVERSATION_ID)
    events = asyncio.run(_run(_executor(tmp_path), "first hello"))
    assert _only_content(delivered) == ["first hello"]
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)
    assert events[0].response is None


def test_run_turn_first_turn_no_conversation_registered_errors(
    tmp_path: Path, delivered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    If agy registers no conversation after delivery, the first turn reports failure.

    The delivery hook leaves the placeholder in place (agy created no
    conversation), so the post-delivery wait times out and the executor surfaces a
    clear error rather than a false success. The wait is shortened so the test
    does not block.
    """
    _seed_state(tmp_path, conversation_id=_PLACEHOLDER_ID)
    monkeypatch.setattr(executor_mod, "_STATE_WAIT_ATTEMPTS", 1)
    monkeypatch.setattr(executor_mod, "_STATE_WAIT_INTERVAL_S", 0.0)
    events = asyncio.run(_run(_executor(tmp_path), "hi"))
    assert _only_content(delivered) == ["hi"]
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "did not register a conversation" in events[0].message


# ---------------------------------------------------------------------------
# run_turn — failure mapping
# ---------------------------------------------------------------------------


def test_run_turn_missing_state_errors(tmp_path: Path, delivered: dict[str, object]) -> None:
    """
    With no bridge state, ``run_turn`` yields an ExecutorError (no delivery).

    The runner seeds bridge state before launching the terminal, so a missing
    state file means broken wiring — not a first turn — and the executor reads it
    once and errors immediately (no polling, no delivery).
    """
    events = asyncio.run(_run(_executor(tmp_path), "hi"))
    assert _only_content(delivered) == []
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "bridge state is missing" in events[0].message


def test_run_turn_inactive_session_errors(
    tmp_path: Path, delivered: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A mismatched request session id blocks delivery with an ExecutorError.

    The harness only steers the conversation it was spawned for; if the bridge
    state names a different session, delivery must be refused.
    """
    _seed_state(tmp_path)
    executor = _executor(tmp_path)
    monkeypatch.setattr(executor, "_request_session_id", "conv_other")
    events = asyncio.run(_run(executor, "hi"))
    assert _only_content(delivered) == []
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "no longer active" in events[0].message


def test_run_turn_delivery_failure_errors(tmp_path: Path, delivered: dict[str, object]) -> None:
    """
    A RuntimeError from the tmux delivery maps to an ExecutorError, not a TurnComplete.

    If the agy terminal is gone or a tmux command fails, the turn must be reported
    as failed so the UI does not show a successful, response-less turn.
    """
    _seed_state(tmp_path)
    delivered["raise"] = RuntimeError("the agy terminal is no longer running")
    events = asyncio.run(_run(_executor(tmp_path), "hi"))
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "Could not deliver the message" in events[0].message


def test_wait_for_state_skips_placeholder_until_real_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_wait_for_state`` polls past the ``agy_conv_*`` placeholder and returns the real id.

    After the first turn is delivered, the launcher placeholder is still in bridge
    state until the forwarder discovers agy's real UUID; the executor must keep
    polling until the real id lands rather than returning the placeholder.
    """
    placeholder = AntigravityNativeBridgeState(
        session_id="conv_test", conversation_id="agy_conv_deadbeef"
    )
    real = AntigravityNativeBridgeState(session_id="conv_test", conversation_id=_CONVERSATION_ID)
    seq = [placeholder, placeholder, real]
    calls = {"n": 0}

    def _read(_bridge_dir: Path) -> AntigravityNativeBridgeState | None:
        state = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return state

    monkeypatch.setattr(executor_mod, "read_bridge_state", _read)
    monkeypatch.setattr(executor_mod, "_STATE_WAIT_INTERVAL_S", 0.0)
    result = asyncio.run(_executor(tmp_path)._wait_for_state())
    assert result is not None
    assert result.conversation_id == _CONVERSATION_ID  # the real id, not the placeholder
    assert calls["n"] == 3  # polled past both placeholders before returning the real id


# ---------------------------------------------------------------------------
# enqueue_session_message (mid-turn steering)
# ---------------------------------------------------------------------------


def test_enqueue_session_message_delivers(tmp_path: Path, delivered: dict[str, object]) -> None:
    """
    ``enqueue_session_message`` types the steer via the same TUI path and returns True.

    Mid-turn web steering reuses the send-keys delivery (agy queues it as the next
    turn), so a successful enqueue must deliver the content and report success.
    """
    _seed_state(tmp_path)
    result = asyncio.run(_executor(tmp_path).enqueue_session_message("main", "steer me"))
    assert result is True
    assert _only_content(delivered) == ["steer me"]


def test_enqueue_session_message_empty_returns_false(
    tmp_path: Path, delivered: dict[str, object]
) -> None:
    """
    Enqueuing empty content returns False without delivering.

    There is nothing to steer with, so the executor reports it did nothing.
    """
    _seed_state(tmp_path)
    result = asyncio.run(_executor(tmp_path).enqueue_session_message("main", ""))
    assert result is False
    assert _only_content(delivered) == []


def test_enqueue_session_message_delivery_failure_returns_false(
    tmp_path: Path, delivered: dict[str, object]
) -> None:
    """
    A failed delivery during enqueue returns False.

    Mid-turn steering is best-effort; a failed injection must be reported as not
    delivered.
    """
    _seed_state(tmp_path)
    delivered["raise"] = RuntimeError("tmux command failed")
    result = asyncio.run(_executor(tmp_path).enqueue_session_message("main", "steer"))
    assert result is False


# ---------------------------------------------------------------------------
# interrupt_session
# ---------------------------------------------------------------------------


def test_interrupt_session_returns_false(tmp_path: Path) -> None:
    """
    ``interrupt_session`` returns ``False`` (no verified agy cancel RPC).

    agy's connect-RPC surface has no confirmed interrupt method, so the executor
    honestly reports it cannot interrupt rather than faking success.
    """
    result = asyncio.run(_executor(tmp_path).interrupt_session("main"))
    assert result is False


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def test_init_requires_bridge_dir_env_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Constructing without a bridge dir or env var raises ``RuntimeError``.

    The harness always spawns with ``HARNESS_ANTIGRAVITY_NATIVE_BRIDGE_DIR``
    set; a missing value means the runner wiring is broken, which must fail loud
    rather than read a bogus path.
    """
    monkeypatch.delenv("HARNESS_ANTIGRAVITY_NATIVE_BRIDGE_DIR", raising=False)
    with pytest.raises(RuntimeError, match="HARNESS_ANTIGRAVITY_NATIVE_BRIDGE_DIR"):
        AntigravityNativeExecutor()


# ---------------------------------------------------------------------------
# reasoning_effort validation (F-M5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_run_turn_valid_effort_is_accepted(
    tmp_path: Path, delivered: dict[str, object], effort: str
) -> None:
    """
    A valid Antigravity effort level (low/medium/high) does not block delivery.

    agy's Gemini backend supports these three levels. A valid effort in the
    config must not surface as an error — the executor validates it and proceeds
    to deliver.

    :param tmp_path: Bridge directory (injected by pytest).
    :param delivered: Stub recording TUI deliveries.
    :param effort: One valid effort level to test.
    :returns: None.
    """
    from omnigent.inner.executor import ExecutorConfig

    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                system_prompt="",
                config=ExecutorConfig(extra={"reasoning_effort": effort}),
            )
        ]

    events = asyncio.run(_drive())
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)


@pytest.mark.parametrize("bad_effort", ["xhigh", "max", "none", "minimal"])
def test_run_turn_unsupported_effort_surfaces_error(
    tmp_path: Path, delivered: dict[str, object], bad_effort: str
) -> None:
    """
    An effort level unsupported by Antigravity/Gemini yields an ExecutorError.

    ``xhigh`` and ``max`` are OpenAI/Anthropic-only; ``none`` and ``minimal``
    are OpenAI-only. Passing them to an Antigravity turn should surface a
    clear non-retryable error so the caller does not silently ignore the
    mismatch.

    :param tmp_path: Bridge directory.
    :param delivered: Stub recording TUI deliveries.
    :param bad_effort: An effort level that is invalid for Antigravity.
    :returns: None.
    """
    from omnigent.inner.executor import ExecutorConfig

    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                system_prompt="",
                config=ExecutorConfig(extra={"reasoning_effort": bad_effort}),
            )
        ]

    events = asyncio.run(_drive())
    assert _only_content(delivered) == [], "delivery must not happen on bad effort"
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert bad_effort in events[0].message


# ---------------------------------------------------------------------------
# _content_to_text flattening
# ---------------------------------------------------------------------------


def test_content_to_text_handles_string_blocks_none_and_other() -> None:
    """
    Flattening covers every content shape the executor may receive.

    A plain string passes through; ``input_text``/``text`` blocks join by newline
    while image/file blocks are dropped (tmux input is text-only); ``None`` yields
    ``""``; any other shape falls back to a JSON encoding rather than crashing.
    """
    from omnigent.inner.antigravity_native_executor import _content_to_text

    assert _content_to_text("  hello  ") == "hello"
    assert (
        _content_to_text(
            [
                {"type": "input_text", "text": "a"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                {"type": "text", "text": "b"},
            ]
        )
        == "a\nb"
    )
    assert _content_to_text(None) == ""
    # Defensive fallback for an unexpected shape: encoded, not crashed.
    assert _content_to_text(123) == "123"
