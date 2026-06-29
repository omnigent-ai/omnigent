"""Tests for the Kiro native executor scaffold."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.inner.executor import ExecutorError, TurnComplete
from omnigent.inner.kiro_native_executor import KiroNativeExecutor


def test_kiro_native_executor_scaffold_capabilities() -> None:
    """The executor is terminal-first and supports live queue injection."""
    executor = KiroNativeExecutor(bridge_dir=Path("/tmp/kiro-bridge"))

    assert executor.supports_streaming() is False
    assert executor.supports_live_message_queue() is True


@pytest.mark.asyncio
async def test_kiro_native_executor_injects_latest_user_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A web turn injects exactly the latest user text into the Kiro terminal."""
    injected: list[tuple[Path, str]] = []

    def _fake_inject(bridge_dir: Path, *, content: str) -> None:
        injected.append((bridge_dir, content))

    monkeypatch.setattr("omnigent.inner.kiro_native_executor.inject_user_message", _fake_inject)
    executor = KiroNativeExecutor(bridge_dir=tmp_path)

    events = [
        event
        async for event in executor.run_turn(
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "second"},
            ],
            [],
            "",
        )
    ]

    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)
    assert events[0].response is None
    assert injected == [(tmp_path, "second")]


@pytest.mark.asyncio
async def test_kiro_native_executor_surfaces_injection_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Bridge injection errors return an ExecutorError instead of hanging."""

    def _fail_inject(bridge_dir: Path, *, content: str) -> None:
        del bridge_dir, content
        raise RuntimeError("kiro terminal is no longer running")

    monkeypatch.setattr("omnigent.inner.kiro_native_executor.inject_user_message", _fail_inject)
    executor = KiroNativeExecutor(bridge_dir=tmp_path)

    events = [
        event async for event in executor.run_turn([{"role": "user", "content": "hi"}], [], "")
    ]

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "kiro terminal is no longer running" in events[0].message


def test_kiro_native_executor_requires_bridge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness process must receive the Kiro bridge dir env."""
    from omnigent.kiro_native_bridge import KIRO_NATIVE_BRIDGE_DIR_ENV_VAR

    monkeypatch.delenv(KIRO_NATIVE_BRIDGE_DIR_ENV_VAR, raising=False)

    with pytest.raises(RuntimeError, match=KIRO_NATIVE_BRIDGE_DIR_ENV_VAR):
        KiroNativeExecutor()
