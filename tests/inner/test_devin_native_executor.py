"""Tests for the devin-native executor (web-UI turn -> Devin TUI)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.inner.devin_native_executor import DevinNativeExecutor
from omnigent.inner.executor import ExecutorConfig, ExecutorError, TurnComplete


@pytest.fixture()
def injections(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record what the executor would deliver into the tmux pane."""
    calls: list[tuple[str, str]] = []

    def _inject_user_message(_bridge_dir: Path, *, content: str, **_kw: object) -> None:
        calls.append(("message", content))

    def _inject_model_command(_bridge_dir: Path, *, model: str, **_kw: object) -> None:
        calls.append(("model", model))

    monkeypatch.setattr(
        "omnigent.inner.devin_native_executor.inject_user_message", _inject_user_message
    )
    monkeypatch.setattr(
        "omnigent.inner.devin_native_executor.inject_model_command", _inject_model_command
    )
    return calls


def _executor(tmp_path: Path) -> DevinNativeExecutor:
    return DevinNativeExecutor(bridge_dir=tmp_path)


async def _run(
    executor: DevinNativeExecutor,
    text: str,
    config: ExecutorConfig | None = None,
) -> list[object]:
    messages = [{"role": "user", "content": text}]
    return [event async for event in executor.run_turn(messages, [], "", config)]


class TestRunTurn:
    @pytest.mark.asyncio
    async def test_injects_the_latest_user_text(
        self, tmp_path: Path, injections: list[tuple[str, str]]
    ) -> None:
        events = await _run(_executor(tmp_path), "hello devin")
        assert injections == [("message", "hello devin")]
        assert isinstance(events[-1], TurnComplete)

    @pytest.mark.asyncio
    async def test_errors_without_user_text(
        self, tmp_path: Path, injections: list[tuple[str, str]]
    ) -> None:
        events = [event async for event in _executor(tmp_path).run_turn([], [], "", None)]
        assert isinstance(events[0], ExecutorError)
        assert injections == []

    @pytest.mark.asyncio
    async def test_routed_model_switches_before_the_message(
        self, tmp_path: Path, injections: list[tuple[str, str]]
    ) -> None:
        # Order matters: a /model typed after the prompt would apply to the NEXT
        # turn, so the switch has to land first.
        await _run(_executor(tmp_path), "go", ExecutorConfig(model="claude-opus-5-xhigh"))
        assert injections == [("model", "claude-opus-5-xhigh"), ("message", "go")]

    @pytest.mark.asyncio
    async def test_unchanged_model_does_not_retype_the_slash_command(
        self, tmp_path: Path, injections: list[tuple[str, str]]
    ) -> None:
        executor = _executor(tmp_path)
        config = ExecutorConfig(model="swe-2-high")
        await _run(executor, "one", config)
        await _run(executor, "two", config)
        assert injections == [
            ("model", "swe-2-high"),
            ("message", "one"),
            ("message", "two"),
        ]

    @pytest.mark.asyncio
    async def test_injection_failure_surfaces_as_executor_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_bridge_dir: Path, *, content: str, **_kw: object) -> None:
            raise RuntimeError("the Devin terminal is no longer running")

        monkeypatch.setattr("omnigent.inner.devin_native_executor.inject_user_message", _boom)
        events = await _run(_executor(tmp_path), "hi")
        assert isinstance(events[0], ExecutorError)
        assert "no longer running" in events[0].message


class TestLiveQueue:
    @pytest.mark.asyncio
    async def test_steering_message_is_injected(
        self, tmp_path: Path, injections: list[tuple[str, str]]
    ) -> None:
        # Devin's composer stays writable mid-turn, so a queued message steers
        # the running turn instead of waiting for it.
        assert await _executor(tmp_path).enqueue_session_message("main", "wait, stop") is True
        assert injections == [("message", "wait, stop")]

    @pytest.mark.asyncio
    async def test_empty_steering_message_is_dropped(
        self, tmp_path: Path, injections: list[tuple[str, str]]
    ) -> None:
        assert await _executor(tmp_path).enqueue_session_message("main", "") is False
        assert injections == []

    def test_declares_live_queue_and_no_streaming(self, tmp_path: Path) -> None:
        executor = _executor(tmp_path)
        assert executor.supports_live_message_queue() is True
        # Output is rendered by the embedded terminal; the forwarder mirrors whole
        # items rather than token deltas.
        assert executor.supports_streaming() is False


class TestContentFlattening:
    @pytest.mark.asyncio
    async def test_text_blocks_are_joined(
        self, tmp_path: Path, injections: list[tuple[str, str]]
    ) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "first"},
                    {"type": "input_text", "text": "second"},
                ],
            }
        ]
        events = [event async for event in _executor(tmp_path).run_turn(messages, [], "", None)]
        assert isinstance(events[-1], TurnComplete)
        assert injections == [("message", "first\n\nsecond")]

    @pytest.mark.asyncio
    async def test_bridge_dir_is_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HARNESS_DEVIN_NATIVE_BRIDGE_DIR", raising=False)
        with pytest.raises(RuntimeError, match="HARNESS_DEVIN_NATIVE_BRIDGE_DIR"):
            DevinNativeExecutor()
