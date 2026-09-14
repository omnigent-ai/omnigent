"""Tests for the native Antigravity (agy) executor bridge (web-turn injection).

These pin the write path: a web/mobile turn is delivered to the running agy by
TYPING IT INTO the agy TUI pane over tmux (``inject_user_message_via_tui``,
mocked here), which agy records as a real ``USER_INPUT`` step on the cascade the
TUI displays; agy's reply is mirrored back by the read driver — so the executor
yields a ``TurnComplete`` with no text rather than fabricating a reply. Typing
into the TUI (not headless ``SendUserCascadeMessage`` RPC) is what unifies the
agy TUI and the web mirror onto ONE cascade, giving claude/codex-native parity
(#1156/#1158). Here the inject is stubbed so the tests assert the executor's
wiring — what text it delivers and how it maps success/failure to events.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

import omnigent.inner.antigravity_native_executor as executor_mod
from omnigent.harnesses.antigravity_native.bridge import (
    AntigravityNativeBridgeState,
    write_bridge_state,
)
from omnigent.inner.antigravity_native_executor import AntigravityNativeExecutor
from omnigent.inner.executor import ExecutorError, ExecutorEvent, TurnComplete

_CONVERSATION_ID = "90468e33-38c3-4e48-ae9f-03c843196227"
_PLACEHOLDER_ID = "agy_conv_placeholder123"
_PORT = 52548
_ECHOED_MODEL = "MODEL_PLACEHOLDER_M20"
_RECOMMENDED_MODEL = "MODEL_PLACEHOLDER_M132"


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


def _steps_with_model(model: str) -> list[dict[str, object]]:
    """
    Build a trajectory-step list whose latest USER_INPUT step carries ``model``.

    Mirrors the live wire shape the executor reads to echo agy's current model:
    ``step.userInput.userConfig.plannerConfig.planModel`` (a string) on a
    ``CORTEX_STEP_TYPE_USER_INPUT`` step. Includes a trailing non-USER_INPUT
    step so the test exercises "find the latest USER_INPUT", not "take the last".

    :param model: agy model enum string to embed in the latest USER_INPUT step.
    :returns: A step list ending past the USER_INPUT step.
    """
    return [
        {
            "stepIndex": 0,
            "type": "CORTEX_STEP_TYPE_USER_INPUT",
            "userInput": {"userConfig": {"plannerConfig": {"planModel": "MODEL_PLACEHOLDER_OLD"}}},
        },
        {
            "stepIndex": 1,
            "type": "CORTEX_STEP_TYPE_USER_INPUT",
            "userInput": {"userConfig": {"plannerConfig": {"planModel": model}}},
        },
        {"stepIndex": 2, "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE", "plannerResponse": {}},
    ]


@pytest.fixture
def injected(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """
    Stub the agy TUI inject, recording each turn the executor delivers.

    The write path types the turn into the agy TUI pane via
    ``inject_user_message_via_tui``; this records every ``{bridge_dir, content}``
    call and, when ``rec["raise"]`` is set, raises it (modeling a dead/unavailable
    TUI pane).

    :param monkeypatch: pytest monkeypatch fixture.
    :returns: A mutable dict: ``calls`` (recorded injects) + ``raise`` (optional).
    """
    rec: dict[str, object] = {"calls": [], "raise": None}

    def _inject(bridge_dir: Path, *, content: str, **_kw: object) -> None:
        calls = rec["calls"]
        assert isinstance(calls, list)
        calls.append({"bridge_dir": bridge_dir, "content": content})
        exc = rec["raise"]
        if exc is not None:
            assert isinstance(exc, BaseException)
            raise exc

    monkeypatch.setattr(executor_mod, "inject_user_message_via_tui", _inject)
    return rec


def _injected(rec: dict[str, object]) -> list[dict[str, object]]:
    """Return the recorded TUI inject calls, in order."""
    calls = rec["calls"]
    assert isinstance(calls, list)
    return calls


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

    Assistant output is posted by the read driver, not streamed by the executor,
    so it must report no streaming or the workflow would await chunks that never
    come.
    """
    assert _executor(tmp_path).supports_streaming() is False


def test_supports_live_message_queue(tmp_path: Path) -> None:
    """
    ``supports_live_message_queue`` is ``True``.

    The server routes mid-turn web messages to ``enqueue_session_message``; the
    executor advertises live steering so that wiring stays active under the RPC
    turn-send path.
    """
    assert _executor(tmp_path).supports_live_message_queue() is True


# ---------------------------------------------------------------------------
# run_turn — delivery (TUI injection)
# ---------------------------------------------------------------------------


def test_run_turn_delivers_via_tui_and_completes(
    tmp_path: Path, injected: dict[str, object]
) -> None:
    """
    ``run_turn`` types the user text into the agy TUI and yields a text-less TurnComplete.

    The turn is injected into the TUI pane (#1156/#1158) — agy records it as a
    real USER_INPUT on the cascade the TUI displays and the read driver mirrors
    the reply, so the executor yields ``TurnComplete`` with ``response=None``
    (fabricating text here would duplicate the mirrored reply).
    """
    _seed_state(tmp_path)
    events = asyncio.run(_run(_executor(tmp_path), "what is 2+2?"))
    calls = _injected(injected)
    assert len(calls) == 1
    assert calls[0]["content"] == "what is 2+2?"
    assert calls[0]["bridge_dir"] == tmp_path
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)
    assert events[0].response is None


def test_run_turn_flattens_content_blocks(tmp_path: Path, injected: dict[str, object]) -> None:
    """Content-block user messages are flattened to text before injection."""
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
                            # malformed data URI (no comma) -> not materialized
                            {"type": "input_image", "image_url": "data:image/png;base64"},
                            {"type": "input_text", "text": "line two"},
                        ],
                    }
                ],
                tools=[],
                system_prompt="",
            )
        ]

    events = asyncio.run(_drive())
    # The unmaterializable image contributes a visible marker, not a
    # silent drop; attachment lines precede the text lines.
    assert _injected(injected)[0]["content"] == (
        "[Attachment attachment could not be loaded]\nline one\nline two"
    )
    assert isinstance(events[0], TurnComplete)


def test_run_turn_uses_latest_user_message(tmp_path: Path, injected: dict[str, object]) -> None:
    """Only the latest user message is delivered (history is not replayed)."""
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
    assert _injected(injected)[0]["content"] == "new question"


def test_run_turn_no_user_text_errors(tmp_path: Path, injected: dict[str, object]) -> None:
    """A turn with no user text yields an ExecutorError without injecting."""
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
    assert _injected(injected) == []
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)


# a tiny valid base64 PNG data URI (1x1 pixel), materialized to disk + referenced
_PNG_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_run_turn_image_attachment_materialized(
    tmp_path: Path, injected: dict[str, object]
) -> None:
    """An image block is written to the bridge dir and referenced by path."""
    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_image", "image_url": _PNG_DATA_URI},
                            {"type": "input_text", "text": "describe this"},
                        ],
                    }
                ],
                tools=[],
                system_prompt="",
            )
        ]

    events = asyncio.run(_drive())
    content = _injected(injected)[0]["content"]
    assert isinstance(content, str)
    # attachment marker is prepended ahead of the typed text
    assert content.startswith("[Attached: ")
    assert str(tmp_path) in content
    assert content.endswith("describe this")
    assert isinstance(events[0], TurnComplete)


def test_run_turn_attachment_only_no_longer_errors(
    tmp_path: Path, injected: dict[str, object]
) -> None:
    """An attachment-only turn injects the marker instead of hard-erroring."""
    _seed_state(tmp_path)

    async def _drive() -> list[ExecutorEvent]:
        return [
            event
            async for event in _executor(tmp_path).run_turn(
                messages=[
                    {
                        "role": "user",
                        "content": [{"type": "input_image", "image_url": _PNG_DATA_URI}],
                    }
                ],
                tools=[],
                system_prompt="",
            )
        ]

    events = asyncio.run(_drive())
    content = _injected(injected)[0]["content"]
    assert isinstance(content, str)
    assert content.startswith("[Attached: ")
    assert isinstance(events[0], TurnComplete)
    assert not any(isinstance(event, ExecutorError) for event in events)


# ---------------------------------------------------------------------------
# run_turn — failure mapping
# ---------------------------------------------------------------------------


def test_run_turn_missing_state_errors(tmp_path: Path, injected: dict[str, object]) -> None:
    """With no bridge state, ``run_turn`` yields an ExecutorError (no inject)."""
    events = asyncio.run(_run(_executor(tmp_path), "hi"))
    assert _injected(injected) == []
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "bridge state is missing" in events[0].message


def test_run_turn_inactive_session_errors(
    tmp_path: Path, injected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mismatched request session id blocks delivery with an ExecutorError."""
    _seed_state(tmp_path)
    executor = _executor(tmp_path)
    monkeypatch.setattr(executor, "_request_session_id", "conv_other")
    events = asyncio.run(_run(executor, "hi"))
    assert _injected(injected) == []
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "no longer active" in events[0].message


def test_run_turn_tui_inject_error_surfaces(tmp_path: Path, injected: dict[str, object]) -> None:
    """
    A ``RuntimeError`` from the TUI inject surfaces as an ExecutorError.

    The inject raises when the agy pane is gone / never advertised / the submit
    never started a turn; the executor must surface it (so the UI can prompt a
    restart) rather than report a fake success the mirror never fills.
    """
    _seed_state(tmp_path)
    injected["raise"] = RuntimeError("the agy terminal is no longer running (the TUI exited)")
    events = asyncio.run(_run(_executor(tmp_path), "hi"))
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "the agy TUI" in events[0].message


# ---------------------------------------------------------------------------
# enqueue_session_message (mid-turn steering)
# ---------------------------------------------------------------------------


def test_enqueue_session_message_delivers(tmp_path: Path, injected: dict[str, object]) -> None:
    """``enqueue_session_message`` injects the steer via the same TUI path and returns True."""
    _seed_state(tmp_path)
    result = asyncio.run(_executor(tmp_path).enqueue_session_message("main", "steer me"))
    assert result is True
    assert _injected(injected)[0]["content"] == "steer me"


def test_enqueue_session_message_empty_returns_false(
    tmp_path: Path, injected: dict[str, object]
) -> None:
    """Enqueuing empty content returns False without injecting."""
    _seed_state(tmp_path)
    result = asyncio.run(_executor(tmp_path).enqueue_session_message("main", ""))
    assert result is False
    assert _injected(injected) == []


def test_enqueue_session_message_inject_failure_returns_false(
    tmp_path: Path, injected: dict[str, object]
) -> None:
    """A failed TUI inject during enqueue returns False."""
    _seed_state(tmp_path)
    injected["raise"] = RuntimeError("boom")
    result = asyncio.run(_executor(tmp_path).enqueue_session_message("main", "steer"))
    assert result is False


# ---------------------------------------------------------------------------
# interrupt_session (real interrupt via CancelCascadeSteps)
# ---------------------------------------------------------------------------


def test_interrupt_session_cancels_and_returns_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    ``interrupt_session`` resolves the port + cascade id and cancels, returning True.

    A successful ``cancel_cascade_steps`` against the discovered agy means the
    running cascade was asked to stop, so the executor reports the interrupt
    succeeded.
    """
    _seed_state(tmp_path)
    seen: dict[str, object] = {}

    def _resolve_port(conversation_id: str) -> int | None:
        seen["resolved_for"] = conversation_id
        return _PORT

    def _cancel(port: int, cascade_id: str) -> bool:
        seen["cancel"] = {"port": port, "cascade_id": cascade_id}
        return True

    monkeypatch.setattr(executor_mod, "resolve_language_server_port", _resolve_port)
    monkeypatch.setattr(executor_mod, "cancel_cascade_steps", _cancel)
    monkeypatch.setattr(executor_mod, "wait_for_turn_idle_via_tui", lambda _bridge: True)
    result = asyncio.run(_executor(tmp_path).interrupt_session("main"))
    assert result is True
    assert seen["resolved_for"] == _CONVERSATION_ID
    assert seen["cancel"] == {"port": _PORT, "cascade_id": _CONVERSATION_ID}


def test_interrupt_session_rpc_failure_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A failed ``cancel_cascade_steps`` makes ``interrupt_session`` return False.

    ``cancel_cascade_steps`` fails open (returns False) on any RPC/transport
    error, and the executor must honestly relay that the interrupt did not land
    rather than claiming success.
    """
    _seed_state(tmp_path)
    monkeypatch.setattr(executor_mod, "resolve_language_server_port", lambda _conv: _PORT)
    monkeypatch.setattr(executor_mod, "cancel_cascade_steps", lambda _port, _cid: False)
    monkeypatch.setattr(executor_mod, "interrupt_turn_via_tui", lambda _bridge, **_kwargs: False)
    result = asyncio.run(_executor(tmp_path).interrupt_session("main"))
    assert result is False


def test_interrupt_session_no_port_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    With no resolvable agy port, ``interrupt_session`` returns False without cancelling.

    A turn cannot be interrupted on an agy that cannot be located, so the
    executor reports failure and never calls cancel.
    """
    _seed_state(tmp_path)
    called = {"cancel": False}

    def _cancel(_port: int, _cid: str) -> bool:
        called["cancel"] = True
        return True

    monkeypatch.setattr(executor_mod, "resolve_language_server_port", lambda _conv: None)
    monkeypatch.setattr(executor_mod, "cancel_cascade_steps", _cancel)
    monkeypatch.setattr(executor_mod, "interrupt_turn_via_tui", lambda _bridge, **_kwargs: False)
    result = asyncio.run(_executor(tmp_path).interrupt_session("main"))
    assert result is False
    assert called["cancel"] is False


def test_interrupt_session_placeholder_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    On a placeholder (no real conversation yet), interrupt returns False, no cancel.

    There is no live cascade to cancel before agy has minted its real id, so the
    executor must not RPC against the ``agy_conv_*`` placeholder.
    """
    _seed_state(tmp_path, conversation_id=_PLACEHOLDER_ID)
    called = {"resolve": False, "cancel": False}

    def _resolve_port(_conv: str) -> int | None:
        called["resolve"] = True
        return _PORT

    def _cancel(_port: int, _cid: str) -> bool:
        called["cancel"] = True
        return True

    monkeypatch.setattr(executor_mod, "resolve_language_server_port", _resolve_port)
    monkeypatch.setattr(executor_mod, "cancel_cascade_steps", _cancel)
    monkeypatch.setattr(executor_mod, "interrupt_turn_via_tui", lambda _bridge, **_kwargs: False)
    result = asyncio.run(_executor(tmp_path).interrupt_session("main"))
    assert result is False
    assert called["cancel"] is False


@pytest.mark.parametrize("conversation_id", [_CONVERSATION_ID, _PLACEHOLDER_ID])
def test_interrupt_uses_tui_when_rpc_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conversation_id: str
) -> None:
    _seed_state(tmp_path, conversation_id=conversation_id)
    monkeypatch.setattr(executor_mod, "resolve_language_server_port", lambda _conv: None)
    calls: list[Path] = []

    def interrupt(bridge_dir: Path, **_kwargs: object) -> bool:
        calls.append(bridge_dir)
        return True

    monkeypatch.setattr(executor_mod, "interrupt_turn_via_tui", interrupt)
    monkeypatch.setattr(executor_mod, "wait_for_turn_idle_via_tui", lambda _bridge: True)
    assert asyncio.run(_executor(tmp_path).interrupt_session("main")) is True
    assert calls == [tmp_path]


def test_interrupt_session_missing_state_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    With no bridge state, ``interrupt_session`` returns False without cancelling.

    No bridge state means no cascade id to address, so the interrupt is a no-op
    reported as failure.
    """
    called = {"cancel": False}

    def _cancel(_port: int, _cid: str) -> bool:
        called["cancel"] = True
        return True

    monkeypatch.setattr(executor_mod, "cancel_cascade_steps", _cancel)
    result = asyncio.run(_executor(tmp_path).interrupt_session("main"))
    assert result is False
    assert called["cancel"] is False


def test_interrupt_bridge_turn_already_idle_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path)
    monkeypatch.setattr(executor_mod, "turn_is_idle_via_tui", lambda _bridge: True)
    monkeypatch.setattr(executor_mod, "wait_for_turn_idle_via_tui", lambda _bridge: True)

    def unexpected(*_args: object) -> bool:
        raise AssertionError("idle turn must not send a cancel request")

    monkeypatch.setattr(executor_mod, "resolve_language_server_port", unexpected)
    monkeypatch.setattr(executor_mod, "interrupt_turn_via_tui", unexpected)
    assert asyncio.run(
        executor_mod.interrupt_bridge_turn(tmp_path, expected_session_id="conv_test")
    )
    assert not asyncio.run(
        executor_mod.interrupt_bridge_turn(tmp_path, expected_session_id="other_session")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_delivery", [False, True])
async def test_interrupt_waits_for_inflight_tui_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_delivery: bool
) -> None:
    _seed_state(tmp_path)
    executor = _executor(tmp_path)
    loop = asyncio.get_running_loop()
    injection_started = asyncio.Event()
    release_injection = threading.Event()
    actions: list[str] = []

    def _inject(_bridge: Path, *, content: str) -> None:
        loop.call_soon_threadsafe(injection_started.set)
        if not release_injection.wait(timeout=5):
            raise RuntimeError("test injection was not released")
        actions.append("injected")

    async def _interrupt(_bridge: Path, *, expected_session_id: str | None) -> bool:
        actions.append("cancelled")
        return True

    monkeypatch.setattr(executor_mod, "inject_user_message_via_tui", _inject)
    monkeypatch.setattr(executor_mod, "interrupt_bridge_turn", _interrupt)
    delivery = asyncio.create_task(executor.enqueue_session_message("main", "hello"))
    await asyncio.wait_for(injection_started.wait(), timeout=5)
    if cancel_delivery:
        delivery.cancel()
    interruption = asyncio.create_task(executor.interrupt_session("main"))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not interruption.done()
        assert actions == []
    finally:
        release_injection.set()
        await asyncio.gather(delivery, return_exceptions=True)
        await interruption
    assert actions == ["injected", "cancelled"]
    assert delivery.cancelled() is cancel_delivery


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_stage", ["escape", "boundary"])
async def test_timed_out_interrupt_owns_workers_until_next_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_stage: str
) -> None:
    from omnigent.harnesses.antigravity_native.stop_hook import STOP_EVENTS_FILE
    from omnigent.inner.antigravity_native_harness import AntigravityNativeExecutorAdapter
    from omnigent.runtime.harnesses import _executor_adapter

    _seed_state(tmp_path)
    app_dir = tmp_path / "agy-home" / ".gemini" / "antigravity-cli"
    cache = app_dir / "cache" / "last_conversations.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"workspace": _CONVERSATION_ID}))
    transcript_path = (
        app_dir
        / "brain"
        / _CONVERSATION_ID
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )
    transcript_path.parent.mkdir(parents=True)
    transcript_path.write_text("{}\n")
    executor = _executor(tmp_path)
    adapter = AntigravityNativeExecutorAdapter()
    adapter._native_executor = executor
    loop = asyncio.get_running_loop()
    worker_started = asyncio.Event()
    release_worker = threading.Event()
    actions: list[tuple[str, int]] = []
    generation = [0]
    active = [True]
    original_record = executor_mod.record_stop_event

    def _block(stage: str) -> None:
        if stage == blocked_stage:
            loop.call_soon_threadsafe(worker_started.set)
            if not release_worker.wait(timeout=5):
                raise RuntimeError("test cancellation worker was not released")

    def _escape(_bridge: Path, **_kwargs: object) -> bool:
        _block("escape")
        actions.append(("escape", generation[0]))
        active[0] = False
        return True

    def _record(bridge_dir: Path, payload: object) -> bool:
        _block("boundary")
        actions.append(("marker", generation[0]))
        return original_record(bridge_dir, payload)

    def _inject(_bridge: Path, *, content: str) -> None:
        generation[0] += 1
        active[0] = True
        actions.append((content, generation[0]))

    monkeypatch.setattr(_executor_adapter, "_INTERRUPT_SLICE_S", 0.01)
    monkeypatch.setattr(executor_mod, "turn_is_idle_via_tui", lambda _bridge: not active[0])
    monkeypatch.setattr(executor_mod, "resolve_language_server_port", lambda _cascade: None)
    monkeypatch.setattr(executor_mod, "interrupt_turn_via_tui", _escape)
    monkeypatch.setattr(executor_mod, "wait_for_turn_idle_via_tui", lambda _bridge: not active[0])
    monkeypatch.setattr(executor_mod, "record_stop_event", _record)
    monkeypatch.setattr(executor_mod, "inject_user_message_via_tui", _inject)
    cleanup = asyncio.create_task(adapter._safe_interrupt(executor, "main"))
    pending: list[asyncio.Task[Any]] = []
    try:
        await asyncio.wait_for(worker_started.wait(), timeout=2)
        assert await asyncio.wait_for(cleanup, timeout=1)
        for _ in range(2):
            waiter = asyncio.create_task(executor.interrupt_session("main"))
            await asyncio.sleep(0)
            waiter.cancel()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter

        next_stop = asyncio.create_task(adapter._handle_interrupt_event())
        pending.append(next_stop)
        await asyncio.sleep(0)
        next_turn = asyncio.create_task(executor.enqueue_session_message("main", "next turn"))
        pending.append(next_turn)
        await asyncio.sleep(0)
        assert not next_stop.done()
        assert not next_turn.done()
        assert actions == ([] if blocked_stage == "escape" else [("escape", 0)])
        release_worker.set()
        assert (await asyncio.wait_for(next_stop, timeout=2)).status_code == 204
        assert await asyncio.wait_for(next_turn, timeout=2)
        assert actions == [("escape", 0), ("marker", 0), ("next turn", 1)]
        markers = (tmp_path / STOP_EVENTS_FILE).read_text().splitlines()
        assert len(markers) == 1
        assert json.loads(markers[0])["cancelled"] is True
    finally:
        release_worker.set()
        await asyncio.gather(cleanup, *pending, return_exceptions=True)
        if executor._interrupt_task is not None:
            await asyncio.gather(executor._interrupt_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_interrupt_rejects_delivery_queued_before_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path)
    executor = _executor(tmp_path)
    loop = asyncio.get_running_loop()
    first_started = asyncio.Event()
    release_first = threading.Event()
    actions: list[str] = []

    def _inject(_bridge: Path, *, content: str) -> None:
        if content == "first":
            loop.call_soon_threadsafe(first_started.set)
            if not release_first.wait(timeout=5):
                raise RuntimeError("test injection was not released")
        actions.append(content)

    async def _interrupt(_bridge: Path, *, expected_session_id: str | None) -> bool:
        actions.append("cancelled")
        return True

    monkeypatch.setattr(executor_mod, "inject_user_message_via_tui", _inject)
    monkeypatch.setattr(executor_mod, "interrupt_bridge_turn", _interrupt)
    first = asyncio.create_task(executor.enqueue_session_message("main", "first"))
    await asyncio.wait_for(first_started.wait(), timeout=5)
    queued = asyncio.create_task(executor.enqueue_session_message("main", "queued"))
    await asyncio.sleep(0)
    interruption = asyncio.create_task(executor.interrupt_session("main"))
    await asyncio.sleep(0)
    release_first.set()
    assert await first is True
    assert await queued is False
    assert await interruption is True
    assert actions == ["first", "cancelled"]


@pytest.mark.asyncio
async def test_retained_native_adapter_confirms_interrupt_after_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.inner.antigravity_native_harness as harness_mod

    class _InterruptExecutor:
        def __init__(self, result: bool) -> None:
            self.result = result
            self.calls: list[str] = []

        async def interrupt_session(self, session_key: str) -> bool:
            self.calls.append(session_key)
            return self.result

    retained = _InterruptExecutor(True)
    monkeypatch.setattr(harness_mod, "_build_antigravity_native_executor", lambda: retained)
    adapter = harness_mod.AntigravityNativeExecutorAdapter()
    adapter._ensure_executor()
    adapter._executor = None

    response = await adapter._handle_interrupt_event()

    assert response.status_code == 204
    assert retained.calls == [adapter._session_key]


@pytest.mark.asyncio
async def test_retained_native_adapter_rejects_unconfirmed_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.inner.antigravity_native_harness as harness_mod
    from omnigent.runtime.harnesses._scaffold import TurnContext

    class _InterruptExecutor:
        async def interrupt_session(self, session_key: str) -> bool:
            return False

    monkeypatch.setattr(
        harness_mod, "_build_antigravity_native_executor", lambda: _InterruptExecutor()
    )
    adapter = harness_mod.AntigravityNativeExecutorAdapter()
    adapter._ensure_executor()
    ctx = TurnContext(
        response_id="response",
        event_queue=asyncio.Queue(),
        cancelled=asyncio.Event(),
    )
    adapter._in_flight[ctx.response_id] = ctx

    response = await adapter._handle_interrupt_event()

    assert response.status_code == 503
    assert ctx.cancelled.is_set()


@pytest.mark.asyncio
async def test_native_adapter_confirms_interrupt_before_first_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.inner.antigravity_native_harness as harness_mod

    class _InterruptExecutor:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def interrupt_session(self, session_key: str) -> bool:
            self.calls.append(session_key)
            return False

    retained = _InterruptExecutor()
    monkeypatch.setattr(harness_mod, "_build_antigravity_native_executor", lambda: retained)
    adapter = harness_mod.AntigravityNativeExecutorAdapter()

    response = await adapter._handle_interrupt_event()

    assert response.status_code == 503
    assert retained.calls == [adapter._session_key]


# ---------------------------------------------------------------------------
# model resolution helpers
# ---------------------------------------------------------------------------


def test_latest_requested_model_picks_latest_user_input() -> None:
    """
    ``_latest_requested_model`` returns the most recent USER_INPUT step's model.

    Echoing agy's CURRENT model means scanning for the LAST USER_INPUT step
    (a later turn may have switched models), not the first or the last step.
    """
    from omnigent.inner.antigravity_native_executor import _latest_requested_model

    assert _latest_requested_model(_steps_with_model(_ECHOED_MODEL)) == _ECHOED_MODEL


def test_latest_requested_model_none_when_absent() -> None:
    """
    ``_latest_requested_model`` returns ``None`` when no USER_INPUT model is present.

    An empty step list (first turn) or steps without a ``planModel`` must signal
    "nothing to echo" so the caller falls back to the recommended model.
    """
    from omnigent.inner.antigravity_native_executor import _latest_requested_model

    assert _latest_requested_model([]) is None
    assert (
        _latest_requested_model([{"stepIndex": 0, "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE"}])
        is None
    )


def test_latest_requested_model_falls_back_to_requested_model() -> None:
    """
    ``_latest_requested_model`` reads the legacy ``requestedModel.model`` shape.

    The live wire carries ``plannerConfig.planModel`` (a string), but a
    TUI-origin step may still use the older ``requestedModel.model`` dict shape.
    The executor must honor that fallback so such a turn's model still echoes.
    """
    from omnigent.inner.antigravity_native_executor import _latest_requested_model

    legacy_steps: list[dict[str, object]] = [
        {
            "stepIndex": 0,
            "type": "CORTEX_STEP_TYPE_USER_INPUT",
            "userInput": {
                "userConfig": {
                    "plannerConfig": {"requestedModel": {"model": "MODEL_PLACEHOLDER_M20"}}
                }
            },
        },
    ]
    assert _latest_requested_model(legacy_steps) == "MODEL_PLACEHOLDER_M20"


def test_recommended_model_picks_recommended_entry() -> None:
    """
    ``_recommended_model`` returns the ``recommended`` catalog entry's enum.

    The fallback model must be the one agy marks ``recommended`` so a first turn
    uses agy's own default rather than an arbitrary catalog entry.
    """
    from omnigent.inner.antigravity_native_executor import _recommended_model

    catalog: dict[str, object] = {
        "models": {
            "a": {"model": "MODEL_A", "recommended": False},
            "b": {"model": "MODEL_B", "recommended": True},
        }
    }
    assert _recommended_model(catalog) == "MODEL_B"


def test_recommended_model_none_when_absent() -> None:
    """
    ``_recommended_model`` returns ``None`` when no entry is recommended.

    A catalog with no ``recommended`` model (or a malformed one) must signal
    "no model" so the caller surfaces a clear error instead of guessing.
    """
    from omnigent.inner.antigravity_native_executor import _recommended_model

    assert _recommended_model({"models": {}}) is None
    assert _recommended_model({"models": {"a": {"model": "MODEL_A"}}}) is None
    assert _recommended_model({}) is None


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
    tmp_path: Path, injected: dict[str, object], effort: str
) -> None:
    """
    A valid Antigravity effort level (low/medium/high) does not block delivery.

    agy's Gemini backend supports these three levels. A valid effort in the
    config must not surface as an error — the executor validates it and proceeds
    to inject the turn into the TUI.

    :param tmp_path: Bridge directory (injected by pytest).
    :param injected: Stub recording TUI injects.
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
    tmp_path: Path, injected: dict[str, object], bad_effort: str
) -> None:
    """
    An effort level unsupported by Antigravity/Gemini yields an ExecutorError.

    ``xhigh`` and ``max`` are OpenAI/Anthropic-only; ``none`` and ``minimal``
    are OpenAI-only. Passing them to an Antigravity turn should surface a
    clear non-retryable error so the caller does not silently ignore the
    mismatch.

    :param tmp_path: Bridge directory.
    :param injected: Stub recording TUI injects.
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
    assert _injected(injected) == [], "delivery must not happen on bad effort"
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert bad_effort in events[0].message


# ---------------------------------------------------------------------------
# _content_to_text flattening
# ---------------------------------------------------------------------------


def test_content_to_text_handles_string_blocks_none_and_other(tmp_path: Path) -> None:
    """
    Flattening covers every content shape the executor may receive.

    A plain string passes through; ``input_text``/``text`` blocks join by newline
    while an unmaterializable image/file block contributes a visible
    could-not-load marker; ``None`` yields ``""``; any other shape falls back to
    a JSON encoding rather than crashing.
    """
    from omnigent.inner.antigravity_native_executor import _content_to_text

    assert _content_to_text("  hello  ", tmp_path) == "hello"
    assert (
        _content_to_text(
            [
                {"type": "input_text", "text": "a"},
                # malformed data URI (no comma) -> not materialized
                {"type": "input_image", "image_url": "data:image/png;base64"},
                {"type": "text", "text": "b"},
            ],
            tmp_path,
        )
        == "[Attachment attachment could not be loaded]\na\nb"
    )
    assert _content_to_text(None, tmp_path) == ""
    # Defensive fallback for an unexpected shape: encoded, not crashed.
    assert _content_to_text(123, tmp_path) == "123"
