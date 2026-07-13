"""Tests for shared process logging helpers."""

from __future__ import annotations

import contextlib
import logging
import os

import pytest

from omnigent._platform import IS_POSIX
from omnigent.process_logging import (
    LOG_TO_STDERR_ENV_VAR,
    LOG_TTY_FD_ENV_VAR,
    child_logging_popen_kwargs,
    terminal_stream_handler,
)


@pytest.mark.skipif(not IS_POSIX, reason="pass_fds is POSIX-only")
def test_child_logging_popen_kwargs_duplicates_explicit_log_fd() -> None:
    """An explicit mirror fd is duplicated before child stderr is redirected."""
    read_fd, write_fd = os.pipe()
    forwarded_fd: int | None = None
    try:
        env = {
            LOG_TO_STDERR_ENV_VAR: "1",
            LOG_TTY_FD_ENV_VAR: str(write_fd),
        }

        with child_logging_popen_kwargs(env) as kwargs:
            forwarded_fd = int(env[LOG_TTY_FD_ENV_VAR])
            assert forwarded_fd != write_fd
            assert kwargs == {"pass_fds": (forwarded_fd,)}

            os.write(forwarded_fd, b"x")
            assert os.read(read_fd, 1) == b"x"
    finally:
        for fd in (read_fd, write_fd, forwarded_fd):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)


@pytest.mark.skipif(not IS_POSIX, reason="fd-based terminal mirroring is POSIX-only")
def test_terminal_stream_handler_writes_to_explicit_log_fd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The terminal mirror can target an inherited fd instead of stderr."""
    read_fd, write_fd = os.pipe()
    handler: logging.Handler | None = None
    try:
        monkeypatch.setenv(LOG_TTY_FD_ENV_VAR, str(write_fd))
        handler = terminal_stream_handler()
        handler.setFormatter(logging.Formatter("%(message)s"))

        record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
        handler.emit(record)

        assert os.read(read_fd, 6) == b"hello\n"
    finally:
        if handler is not None:
            handler.close()
        for fd in (read_fd, write_fd):
            with contextlib.suppress(OSError):
                os.close(fd)
