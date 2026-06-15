"""Tests for CursorExecutor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.inner.cursor_executor import CursorExecutor
from omnigent.inner.executor import TextChunk, TurnComplete


class _FakeStreamReader:
    def __init__(self, lines: list[bytes]):
        self._buffer = bytearray(b"".join(lines))

    async def read(self, n: int = -1) -> bytes:
        if not self._buffer:
            return b""
        if n < 0 or n > len(self._buffer):
            n = len(self._buffer)
        chunk = bytes(self._buffer[:n])
        del self._buffer[:n]
        return chunk


class _FakeProcess:
    def __init__(self, stdout_lines: list[str], stderr: str = "", returncode: int = 0):
        self.stdin = None
        self.stdout = _FakeStreamReader([(line + "\n").encode() for line in stdout_lines])
        self.stderr = _FakeStreamReader([stderr.encode()])
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.asyncio
async def test_cursor_executor_stream_json_turn(tmp_path: Path) -> None:
    process = _FakeProcess(
        [
            '{"type":"system","model":"Composer 2.5 Fast"}',
            '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Hi"}]}}',
            (
                '{"type":"result","subtype":"success","is_error":false,'
                '"result":"Hi","usage":{"inputTokens":7,"outputTokens":2,'
                '"cacheReadTokens":3,"cacheWriteTokens":1}}'
            ),
        ]
    )
    calls: list[tuple[object, ...]] = []

    async def fake_create(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(args)
        return process

    with (
        patch("omnigent.inner.cursor_executor._find_cursor_cli", return_value="/usr/bin/cursor-agent"),
        patch("omnigent.inner.cursor_executor._create_subprocess_exec", fake_create),
    ):
        executor = CursorExecutor(cwd=str(tmp_path), model="gpt-5")
        events = [
            event
            async for event in executor.run_turn(
                [{"role": "user", "content": "say hi"}],
                [],
                "",
            )
        ]

    assert any(isinstance(event, TextChunk) and event.text == "Hi" for event in events)
    complete = next(event for event in events if isinstance(event, TurnComplete))
    assert complete.response == "Hi"
    assert complete.usage == {
        "input_tokens": 7,
        "output_tokens": 2,
        "total_tokens": 13,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 1,
        "context_tokens": 11,
        "model": "Composer 2.5 Fast",
    }
    assert calls
    argv = calls[0]
    assert argv[:5] == (
        "/usr/bin/cursor-agent",
        "--print",
        "--output-format",
        "stream-json",
        "--stream-partial-output",
    )
    assert "--trust" in argv
    assert "--model" in argv
    assert "gpt-5" in argv


@pytest.mark.asyncio
async def test_cursor_executor_handles_timestamped_partial_assistant_chunks(
    tmp_path: Path,
) -> None:
    process = _FakeProcess(
        [
            '{"type":"system","model":"Composer 2.5 Fast"}',
            (
                '{"type":"assistant","message":{"role":"assistant","content":'
                '[{"type":"text","text":"1\\n"}]},"timestamp_ms":1}'
            ),
            (
                '{"type":"assistant","message":{"role":"assistant","content":'
                '[{"type":"text","text":"2\\n3"}]},"timestamp_ms":2}'
            ),
            (
                '{"type":"assistant","message":{"role":"assistant","content":'
                '[{"type":"text","text":"1\\n2\\n3"}]}}'
            ),
            '{"type":"result","subtype":"success","is_error":false,"result":"1\\n2\\n3"}',
        ]
    )

    async def fake_create(*args, **kwargs):  # type: ignore[no-untyped-def]
        return process

    with (
        patch("omnigent.inner.cursor_executor._find_cursor_cli", return_value="/usr/bin/cursor-agent"),
        patch("omnigent.inner.cursor_executor._create_subprocess_exec", fake_create),
    ):
        executor = CursorExecutor(cwd=str(tmp_path))
        events = [
            event
            async for event in executor.run_turn(
                [{"role": "user", "content": "count"}],
                [],
                "",
            )
        ]

    assert [event.text for event in events if isinstance(event, TextChunk)] == ["1\n", "2\n3"]
    complete = next(event for event in events if isinstance(event, TurnComplete))
    assert complete.response == "1\n2\n3"


@pytest.mark.asyncio
async def test_cursor_executor_surfaces_nonzero_stderr(tmp_path: Path) -> None:
    process = _FakeProcess([], stderr="not logged in", returncode=1)

    async def fake_create(*args, **kwargs):  # type: ignore[no-untyped-def]
        return process

    with (
        patch("omnigent.inner.cursor_executor._find_cursor_cli", return_value="/usr/bin/cursor-agent"),
        patch("omnigent.inner.cursor_executor._create_subprocess_exec", fake_create),
    ):
        executor = CursorExecutor(cwd=str(tmp_path))
        events = [
            event
            async for event in executor.run_turn(
                [{"role": "user", "content": "say hi"}],
                [],
                "",
            )
        ]

    assert events[0].message == "Cursor agent failed: not logged in"
