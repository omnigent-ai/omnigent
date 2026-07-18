"""Unit tests for CocoNativeExecutor — the harness-side tmux injector."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.inner import coco_native_executor as cne
from omnigent.inner.executor import ExecutorError, TurnComplete


def test_supports_flags(tmp_path: Path) -> None:
    ex = cne.CocoNativeExecutor(bridge_dir=tmp_path)
    assert ex.supports_streaming() is False
    assert ex.supports_live_message_queue() is True


def test_content_to_text_plain_and_parts(tmp_path: Path) -> None:
    assert cne._content_to_text("hello", tmp_path) == "hello"
    blocks = [{"type": "input_text", "text": "a"}, {"type": "text", "text": "b"}]
    assert cne._content_to_text(blocks, tmp_path) == "a\n\nb"


def test_latest_user_text_picks_last_user(tmp_path: Path) -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    assert cne._latest_user_text(messages, tmp_path) == "second"


def test_bridge_dir_from_env_requires_var(monkeypatch) -> None:
    monkeypatch.delenv(cne.BRIDGE_DIR_ENV_VAR, raising=False)
    with pytest.raises(RuntimeError):
        cne._bridge_dir_from_env()


async def test_run_turn_injects_latest_user_message(tmp_path: Path, monkeypatch) -> None:
    injected: list[tuple[Path, str]] = []

    def _fake_inject(bridge_dir: Path, *, content: str) -> None:
        injected.append((bridge_dir, content))

    monkeypatch.setattr(cne, "inject_user_message", _fake_inject)
    ex = cne.CocoNativeExecutor(bridge_dir=tmp_path)
    events = [e async for e in ex.run_turn([{"role": "user", "content": "do it"}], [], "")]
    assert injected == [(tmp_path, "do it")]
    assert len(events) == 1 and isinstance(events[0], TurnComplete)
    assert events[0].response is None


async def test_run_turn_errors_with_no_user_text(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cne, "inject_user_message", lambda *a, **k: None)
    ex = cne.CocoNativeExecutor(bridge_dir=tmp_path)
    events = [e async for e in ex.run_turn([{"role": "assistant", "content": "x"}], [], "")]
    assert len(events) == 1 and isinstance(events[0], ExecutorError)


async def test_run_turn_errors_when_injection_fails(tmp_path: Path, monkeypatch) -> None:
    def _boom(bridge_dir: Path, *, content: str) -> None:
        raise RuntimeError("tmux pane gone")

    monkeypatch.setattr(cne, "inject_user_message", _boom)
    ex = cne.CocoNativeExecutor(bridge_dir=tmp_path)
    events = [e async for e in ex.run_turn([{"role": "user", "content": "go"}], [], "")]
    assert len(events) == 1 and isinstance(events[0], ExecutorError)
    assert "tmux pane gone" in events[0].message


async def test_enqueue_session_message_injects(tmp_path: Path, monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        cne, "inject_user_message", lambda bridge_dir, *, content: seen.append(content)
    )
    ex = cne.CocoNativeExecutor(bridge_dir=tmp_path)
    assert await ex.enqueue_session_message("main", "steer") is True
    assert seen == ["steer"]
    # Empty content is a no-op (no injection).
    assert await ex.enqueue_session_message("main", "") is False


async def test_enqueue_session_message_false_on_injection_error(
    tmp_path: Path, monkeypatch
) -> None:
    def _boom(bridge_dir: Path, *, content: str) -> None:
        raise RuntimeError("tmux pane gone")

    monkeypatch.setattr(cne, "inject_user_message", _boom)
    ex = cne.CocoNativeExecutor(bridge_dir=tmp_path)
    assert await ex.enqueue_session_message("main", "steer") is False
