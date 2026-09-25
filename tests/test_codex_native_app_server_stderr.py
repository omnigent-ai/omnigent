"""The native app-server must keep draining stderr, regardless of line length."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from omnigent.harnesses.codex_native.app_server import (
    _STDERR_CHUNK_LIMIT,
    CodexNativeAppServer,
)


@pytest.fixture
def server(tmp_path: Path) -> CodexNativeAppServer:
    return CodexNativeAppServer(
        codex_path=sys.executable,
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        env={},
        config_overrides=[],
        cwd=tmp_path,
        bridge_dir=tmp_path,
        recent_stderr=[],
    )


@pytest.mark.parametrize("terminated_line", [True, False])
async def test_stderr_draining_keeps_child_responsive(
    server: CodexNativeAppServer, terminated_line: bool
) -> None:
    """Oversized stderr must not block a live child's readiness response."""
    child = """
import sys

sys.stderr.buffer.write(b'x' * (1024 * 1024))
if sys.argv[1] == 'terminated':
    sys.stderr.buffer.write(b'\\n')
sys.stderr.buffer.flush()
print('ready', flush=True)
sys.stdin.readline()
if sys.argv[1] != 'terminated':
    sys.stderr.buffer.write(b'\\n')
sys.stderr.buffer.write(b'followup diagnostic\\nEOF fragment')
sys.stderr.buffer.flush()
"""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child,
        "terminated" if terminated_line else "unterminated",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    server.proc = proc
    reader = asyncio.create_task(server._stderr_loop())
    assert proc.stdout is not None and proc.stdin is not None
    try:
        # Keep the default StreamReader limit. The old readline() drain dies
        # at 64 KiB, leaving the child blocked on stderr before this response.
        assert await asyncio.wait_for(proc.stdout.readline(), 5) == b"ready\n"
        assert proc.returncode is None
        assert not reader.done()
        proc.stdin.write(b"stop\n")
        await proc.stdin.drain()
        await asyncio.wait_for(reader, 5)
        assert await asyncio.wait_for(proc.wait(), 5) == 0
        assert server.recent_stderr == [
            "x" * _STDERR_CHUNK_LIMIT + "...[truncated]",
            "followup diagnostic",
            "EOF fragment",
        ]
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        # Drain even on failure so a paused stderr transport cannot strand wait().
        await asyncio.wait_for(proc.communicate(), 5)


async def test_stderr_preserves_lines_and_unicode_with_a_bounded_tail(
    server: CodexNativeAppServer,
) -> None:
    """Chunk boundaries preserve UTF-8 and line boundaries without retaining a flood."""
    stderr = asyncio.StreamReader()
    server.proc = cast(asyncio.subprocess.Process, SimpleNamespace(stderr=stderr))
    ordinary = "".join(f"line {i}\n" for i in range(30))
    # The multibyte character crosses an 8192-byte read boundary.
    unicode_line = "u" * (8191 - len(ordinary)) + "€"
    stderr.feed_data(
        ordinary.encode() + unicode_line.encode() + b"\r\n\n" + b"z" * (2 * _STDERR_CHUNK_LIMIT)
    )
    stderr.feed_eof()

    await server._stderr_loop()

    assert server.recent_stderr == [
        *(f"line {i}" for i in range(13, 30)),
        unicode_line,
        "",
        "z" * _STDERR_CHUNK_LIMIT + "...[truncated]",
    ]


async def test_stderr_read_failure_is_logged_immediately(
    server: CodexNativeAppServer, caplog: pytest.LogCaptureFixture
) -> None:
    stderr = asyncio.StreamReader()
    stderr.set_exception(OSError("stderr pipe failed"))
    server.proc = cast(asyncio.subprocess.Process, SimpleNamespace(stderr=stderr))

    with caplog.at_level(logging.ERROR), pytest.raises(OSError, match="stderr pipe failed"):
        await server._stderr_loop()

    assert "Codex app-server stderr drain failed" in caplog.text


async def test_stderr_cancellation_is_not_logged_as_a_failure(
    server: CodexNativeAppServer, caplog: pytest.LogCaptureFixture
) -> None:
    stderr = asyncio.StreamReader()
    server.proc = cast(asyncio.subprocess.Process, SimpleNamespace(stderr=stderr))
    reader = asyncio.create_task(server._stderr_loop())
    await asyncio.sleep(0)
    reader.cancel()

    with pytest.raises(asyncio.CancelledError):
        await reader

    assert "Codex app-server stderr drain failed" not in caplog.text
