"""Codex-native startup-prompt detection and thread-start recovery.

A runner-owned Codex TUI runs detached in a tmux pane. When it parks its
startup on an interactive prompt a human can answer — a directory-trust
screen, a hook-review screen, or a ``TERM`` "Continue anyway?" gate — the old
behaviour reaped the terminal on the 30s thread-start timeout and failed every
chat turn with "Codex native thread never started (startup timed out)". These
tests cover the fix: detect that prompt from the pane, record an actionable
fail-fast notice instead, and keep listening so answering the prompt recovers
the session — while a genuine startup failure (no prompt, or the event stream
ending) stays fatal as before.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.harnesses.codex_native.bridge import read_bridge_startup_error
from omnigent.runner.native import orchestration


class _FakeEventClient:
    """Minimal stand-in for the app-server listener the discover task closes."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


# Sentinel marking "called without an explicit timeout" (the bounded wait).
_DEFAULT = object()


class _WaitStub:
    """Scripted ``wait_for_thread_started`` recording the timeout of each call."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.timeouts: list[Any] = []

    async def __call__(self, client: Any, *, timeout: Any = _DEFAULT) -> str:
        self.timeouts.append(timeout)
        result = self._results.pop(0)
        if result == "timeout":
            raise TimeoutError
        if result == "runtime":
            raise RuntimeError("event stream ended")
        if result == "cancel":
            raise asyncio.CancelledError
        return result


async def _run_discover(bridge_dir: Path) -> _FakeEventClient:
    event_client = _FakeEventClient()
    await orchestration._codex_discover_thread_and_forward(
        session_id="conv_test",
        bridge_dir=bridge_dir,
        codex_ws_url="ws://127.0.0.1:0",
        codex_home=bridge_dir / "codex-home",
        workspace=str(bridge_dir),
        event_client=event_client,  # type: ignore[arg-type]
        routing_summary="provider 'x' via cli-config",
        tmux_socket=Path("/tmp/fake.sock"),
        tmux_target="%1",
    )
    return event_client


@pytest.mark.parametrize(
    ("pane_text", "expected"),
    [
        ("... Hooks need review\n1. Review hooks", "a hook-review prompt"),
        (
            "You are in /home/me\nDo you trust the contents of this directory?",
            "a directory-trust prompt",
        ),
        (
            'WARNING: TERM is set to "dumb" ... Continue anyway? [y/N]',
            "a terminal-compatibility prompt",
        ),
        ("just some ordinary model output, nothing blocking", None),
    ],
)
@pytest.mark.asyncio
async def test_pane_interactive_prompt_classifies_known_screens(
    monkeypatch: pytest.MonkeyPatch, pane_text: str, expected: str | None
) -> None:
    async def _fake_capture(socket_path: str, tmux_target: str) -> bytes:
        return pane_text.encode()

    monkeypatch.setattr("omnigent.terminals.control_bridge._run_tmux_capture", _fake_capture)
    result = await orchestration._codex_pane_interactive_prompt(Path("/s"), "%1")
    assert result == expected


@pytest.mark.asyncio
async def test_pane_interactive_prompt_none_without_pane() -> None:
    assert await orchestration._codex_pane_interactive_prompt(None, None) is None
    assert await orchestration._codex_pane_interactive_prompt(Path("/s"), None) is None


@pytest.mark.asyncio
async def test_pane_interactive_prompt_swallows_capture_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(socket_path: str, tmux_target: str) -> bytes:
        raise RuntimeError("tmux gone")

    monkeypatch.setattr("omnigent.terminals.control_bridge._run_tmux_capture", _boom)
    assert await orchestration._codex_pane_interactive_prompt(Path("/s"), "%1") is None


@pytest.mark.asyncio
async def test_thread_start_timeout_on_prompt_records_recoverable_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A timeout while a prompt is up keeps waiting and records an actionable notice."""
    # Bounded wait times out; the second, unbounded wait is interrupted by a
    # teardown cancel (stands in for the session being torn down while a human
    # had not yet answered the prompt).
    wait = _WaitStub(["timeout", "cancel"])
    monkeypatch.setattr(codex_native_forwarder, "wait_for_thread_started", wait)

    async def _prompt(socket: Any, target: Any) -> str:
        return "a hook-review prompt"

    monkeypatch.setattr(orchestration, "_codex_pane_interactive_prompt", _prompt)

    with pytest.raises(asyncio.CancelledError):
        await _run_discover(tmp_path)

    # It retried without a deadline rather than reaping the terminal.
    assert wait.timeouts == [_DEFAULT, None]
    recorded = read_bridge_startup_error(tmp_path)
    assert recorded is not None
    assert "hook-review prompt" in recorded
    assert "Open the Terminal" in recorded
    # The fatal timeout marker the executor keys "never started" on is absent.
    assert "never started" not in recorded
    assert "startup timed out" not in recorded


@pytest.mark.asyncio
async def test_thread_start_timeout_clears_notice_once_thread_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Answering the prompt (thread starts on the retry) clears the notice."""
    wait = _WaitStub(["timeout", "thread-abc"])
    monkeypatch.setattr(codex_native_forwarder, "wait_for_thread_started", wait)

    async def _prompt(socket: Any, target: Any) -> str:
        return "a directory-trust prompt"

    monkeypatch.setattr(orchestration, "_codex_pane_interactive_prompt", _prompt)

    # Once the thread starts the notice is cleared, then the task moves on to
    # record external_session_id — which needs RUNNER_SERVER_URL. Leaving it
    # unset raises right after the clear, so the test asserts the clear without
    # standing up a live server.
    monkeypatch.delenv("RUNNER_SERVER_URL", raising=False)

    with pytest.raises(RuntimeError, match="RUNNER_SERVER_URL"):
        await _run_discover(tmp_path)

    assert wait.timeouts == [_DEFAULT, None]
    assert read_bridge_startup_error(tmp_path) is None


@pytest.mark.asyncio
async def test_thread_start_timeout_without_prompt_stays_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A timeout with no interactive prompt keeps the fatal 'never started' error."""
    wait = _WaitStub(["timeout"])
    monkeypatch.setattr(codex_native_forwarder, "wait_for_thread_started", wait)

    async def _no_prompt(socket: Any, target: Any) -> None:
        return None

    monkeypatch.setattr(orchestration, "_codex_pane_interactive_prompt", _no_prompt)

    event_client = await _run_discover(tmp_path)

    assert wait.timeouts == [_DEFAULT]  # did not retry
    recorded = read_bridge_startup_error(tmp_path)
    assert recorded is not None
    assert "never started" in recorded
    assert event_client.closed  # terminal/listener torn down as before


@pytest.mark.asyncio
async def test_event_stream_end_stays_fatal_even_with_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A crashed TUI (stream ends) is fatal regardless of any stale pane text."""
    wait = _WaitStub(["runtime"])
    monkeypatch.setattr(codex_native_forwarder, "wait_for_thread_started", wait)

    async def _prompt(socket: Any, target: Any) -> str:
        return "a hook-review prompt"

    monkeypatch.setattr(orchestration, "_codex_pane_interactive_prompt", _prompt)

    await _run_discover(tmp_path)

    assert wait.timeouts == [_DEFAULT]  # RuntimeError is not the timeout retry path
    recorded = read_bridge_startup_error(tmp_path)
    assert recorded is not None
    assert "never started" in recorded
