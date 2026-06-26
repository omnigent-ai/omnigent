"""Unit tests for PiNativeExecutor — the harness-side Pi inbox enqueuer.

These cover the pi-native happy path WITHOUT a real LLM or a live Pi TUI.
The pi-native executor never drives a model: a native Pi process is already
running in the session terminal with the Omnigent extension loaded, and each
turn merely queues the latest user message into the bridge inbox for that
extension to consume. The "LLM" is the out-of-process Pi TUI, so the happy
path is verified by mocking the bridge sink (``enqueue_user_message``) and
asserting the executor queues the right text and yields ``TurnComplete`` —
the same shape the peer native ``test_goose_native_executor.py`` uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.inner import pi_native_executor as pne
from omnigent.inner.executor import ExecutorError, TurnComplete


def test_supports_flags(tmp_path: Path) -> None:
    ex = pne.PiNativeExecutor(bridge_dir=tmp_path)
    # Output is emitted by the resident Pi extension, not streamed by the
    # executor, so streaming is off but live message queueing is on.
    assert ex.supports_streaming() is False
    assert ex.supports_live_message_queue() is True


def test_content_to_text_plain_and_parts(tmp_path: Path) -> None:
    assert pne._content_to_text("hello", tmp_path) == "hello"
    blocks = [
        {"type": "input_text", "text": "a"},
        {"type": "input_text", "text": "b"},
    ]
    assert pne._content_to_text(blocks, tmp_path) == "a\n\nb"


def test_latest_user_text_picks_last_user(tmp_path: Path) -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    assert pne._latest_user_text(messages, tmp_path) == "second"


def test_bridge_dir_from_env_requires_var(monkeypatch) -> None:
    monkeypatch.delenv(pne.PI_NATIVE_BRIDGE_DIR_ENV_VAR, raising=False)
    with pytest.raises(RuntimeError):
        pne._bridge_dir_from_env()


async def test_run_turn_enqueues_latest_user_message(tmp_path: Path, monkeypatch) -> None:
    # Happy path: the latest user message is queued for the resident Pi
    # extension and the turn completes. No real LLM/Pi process is involved —
    # the bridge sink is the only boundary that needs mocking.
    enqueued: list[tuple[Path, str]] = []

    def _fake_enqueue(bridge_dir: Path, content: str) -> str:
        enqueued.append((bridge_dir, content))
        return "msg_test"

    monkeypatch.setattr(pne, "enqueue_user_message", _fake_enqueue)
    ex = pne.PiNativeExecutor(bridge_dir=tmp_path)
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "do it"},
    ]
    events = [e async for e in ex.run_turn(messages, [], "system prompt")]

    # The latest user turn (not the earlier one) is what reaches Pi.
    assert enqueued == [(tmp_path, "do it")]
    assert len(events) == 1 and isinstance(events[0], TurnComplete)
    # pi-native does not synthesize an assistant response — the extension
    # mirrors Pi's output back over HTTP, so the executor returns no response.
    assert events[0].response is None


async def test_run_turn_errors_with_no_user_text(tmp_path: Path, monkeypatch) -> None:
    enqueued: list[str] = []
    monkeypatch.setattr(
        pne, "enqueue_user_message", lambda bridge_dir, content: enqueued.append(content)
    )
    ex = pne.PiNativeExecutor(bridge_dir=tmp_path)
    events = [e async for e in ex.run_turn([{"role": "assistant", "content": "x"}], [], "")]

    # Nothing to send to Pi → surface an error, queue nothing.
    assert enqueued == []
    assert len(events) == 1 and isinstance(events[0], ExecutorError)


async def test_enqueue_session_message_queues_steering(tmp_path: Path, monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        pne, "enqueue_user_message", lambda bridge_dir, content: seen.append(content)
    )
    ex = pne.PiNativeExecutor(bridge_dir=tmp_path)

    assert await ex.enqueue_session_message("main", "steer") is True
    assert seen == ["steer"]
    # Empty content is a no-op (nothing queued, returns False).
    assert await ex.enqueue_session_message("main", "") is False
    assert seen == ["steer"]
