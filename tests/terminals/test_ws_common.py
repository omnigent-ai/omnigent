"""Tests for shared terminal WebSocket framing and tmux liveness helpers."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import dataclass

import pytest

import omnigent.terminals.ws_common as ws_common
from omnigent.terminals.ws_common import (
    _check_pane_dead_definitive,
    _coalesce_limit_after_input,
    _current_coalesce_limit,
    _forward_terminal_to_ws,
    _tmux_session_alive,
)


def test_importing_claude_native_does_not_import_fastapi() -> None:
    """Shared close codes stay importable without the server dependency graph."""
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import omnigent.claude_native; assert 'fastapi' not in sys.modules",
        ],
        check=True,
    )


class _RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_forward_terminal_to_ws_coalesces_and_caps_output() -> None:
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    chunks = [bytes([index]) * 1024 for index in range(6)]
    for chunk in chunks:
        queue.put_nowait(chunk)
    queue.put_nowait(None)

    websocket = _RecordingWebSocket()
    await _forward_terminal_to_ws(  # type: ignore[arg-type]
        websocket,
        queue,
        max_coalesce_bytes=2048,
    )

    assert [len(frame) for frame in websocket.sent] == [2048, 2048, 2048]
    assert b"".join(websocket.sent) == b"".join(chunks)


@pytest.mark.asyncio
async def test_forward_terminal_to_ws_sends_lone_chunk_immediately() -> None:
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    websocket = _RecordingWebSocket()
    task = asyncio.create_task(_forward_terminal_to_ws(websocket, queue))  # type: ignore[arg-type]
    try:
        queue.put_nowait(b"x")

        async def _wait_for_frame() -> None:
            while not websocket.sent:
                await asyncio.sleep(0)

        await asyncio.wait_for(_wait_for_frame(), timeout=1.0)
        assert websocket.sent == [b"x"]
    finally:
        queue.put_nowait(None)
        await task


@pytest.mark.parametrize("cap", [0, -1, lambda: 0])
def test_current_coalesce_limit_rejects_non_positive_values(cap: object) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        _current_coalesce_limit(cap)  # type: ignore[arg-type]


def test_coalesce_limit_after_input_uses_interactive_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ws_common, "_monotonic", lambda: 100.5)
    assert _coalesce_limit_after_input(None) == ws_common._WS_COALESCE_MAX_BYTES
    assert _coalesce_limit_after_input(100.2) == ws_common._INTERACTIVE_WS_COALESCE_MAX_BYTES
    assert _coalesce_limit_after_input(90.0) == ws_common._WS_COALESCE_MAX_BYTES


@dataclass
class _FakeProcess:
    stdout: bytes
    returncode: int = 0
    killed: bool = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, b""

    def kill(self) -> None:
        self.killed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdout", "expected_alive", "expected_dead"),
    [(b"0\n", True, False), (b"1\n", False, True)],
)
async def test_tmux_liveness_uses_pane_dead_flag(
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
    expected_alive: bool,
    expected_dead: bool,
) -> None:
    process = _FakeProcess(stdout)

    async def _create(*args: object, **kwargs: object) -> _FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)
    assert await _tmux_session_alive("socket", "main") is expected_alive
    assert await _check_pane_dead_definitive("socket", "main") is expected_dead


@pytest.mark.asyncio
async def test_tmux_liveness_distinguishes_probe_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise(*args: object, **kwargs: object) -> None:
        raise OSError("tmux unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise)
    assert await _tmux_session_alive("socket", "main") is False
    assert await _check_pane_dead_definitive("socket", "main") is None


def test_ws_close_codes_are_distinct_application_codes() -> None:
    codes = {
        ws_common.WS_CLOSE_TERMINAL_NOT_FOUND,
        ws_common.WS_CLOSE_TERMINAL_DETACHED,
        ws_common.WS_CLOSE_WRONG_REPLICA,
        ws_common.WS_CLOSE_INTERNAL_ERROR,
    }
    assert len(codes) == 4
    assert all(4000 <= code <= 4999 for code in codes)
