"""Unit tests for HermesNativeExecutor — the harness-side tmux injector."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.inner import hermes_native_executor as hne
from omnigent.inner.executor import ExecutorError, TurnComplete


def test_supports_flags(tmp_path: Path) -> None:
    ex = hne.HermesNativeExecutor(bridge_dir=tmp_path)
    assert ex.supports_streaming() is False
    assert ex.supports_live_message_queue() is True


def test_content_to_text_plain_and_parts(tmp_path: Path) -> None:
    assert hne._content_to_text("hello", tmp_path) == "hello"
    blocks = [{"type": "input_text", "text": "a"}, {"type": "text", "text": "b"}]
    assert hne._content_to_text(blocks, tmp_path) == "a\n\nb"


def test_latest_user_text_picks_last_user(tmp_path: Path) -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    assert hne._latest_user_text(messages, tmp_path) == "second"


def test_bridge_dir_from_env_requires_var(monkeypatch) -> None:
    monkeypatch.delenv(hne.BRIDGE_DIR_ENV_VAR, raising=False)
    with pytest.raises(RuntimeError):
        hne._bridge_dir_from_env()


async def test_run_turn_injects_latest_user_message(tmp_path: Path, monkeypatch) -> None:
    injected: list[tuple[Path, str]] = []

    def _fake_inject(bridge_dir: Path, *, content: str) -> None:
        injected.append((bridge_dir, content))

    monkeypatch.setattr(hne, "inject_user_message", _fake_inject)
    ex = hne.HermesNativeExecutor(bridge_dir=tmp_path)
    events = [e async for e in ex.run_turn([{"role": "user", "content": "do it"}], [], "")]
    assert injected == [(tmp_path, "do it")]
    assert len(events) == 1 and isinstance(events[0], TurnComplete)


async def test_run_turn_errors_with_no_user_text(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(hne, "inject_user_message", lambda *a, **k: None)
    ex = hne.HermesNativeExecutor(bridge_dir=tmp_path)
    events = [e async for e in ex.run_turn([{"role": "assistant", "content": "x"}], [], "")]
    assert len(events) == 1 and isinstance(events[0], ExecutorError)


async def test_enqueue_session_message_injects(tmp_path: Path, monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        hne, "inject_user_message", lambda bridge_dir, *, content: seen.append(content)
    )
    ex = hne.HermesNativeExecutor(bridge_dir=tmp_path)
    assert await ex.enqueue_session_message("main", "steer") is True
    assert seen == ["steer"]
    # Empty content is a no-op (no injection).
    assert await ex.enqueue_session_message("main", "") is False
