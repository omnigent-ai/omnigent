"""Tests for shared process logging helpers."""

from __future__ import annotations

import contextlib
import logging
import os
import re

import pytest

from omnigent._platform import IS_POSIX
from omnigent.process_logging import (
    LOG_FORCE_COLOR_ENV_VAR,
    LOG_TO_STDERR_ENV_VAR,
    LOG_TTY_FD_ENV_VAR,
    TerminalLogFormatter,
    child_logging_popen_kwargs,
    terminal_stream_handler,
    terminal_supports_color,
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


def test_terminal_log_formatter_colors_level_name() -> None:
    """Terminal logs color the level, source, and function columns."""
    formatter = TerminalLogFormatter(use_colors=True)
    record = logging.LogRecord(
        "omnigent.example",
        logging.INFO,
        __file__,
        1,
        "ready",
        (),
        None,
        "serve",
    )

    output = formatter.format(record)

    assert re.match(
        r"\x1b\[32mINFO \x1b\[0m \d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} "
        r"\x1b\[34mexample\s+\x1b\[0m \x1b\[35mserve\s+\x1b\[0m \| ready",
        output,
    )
    assert record.levelname == "INFO"
    assert "source_name" not in record.__dict__
    assert "func_name" not in record.__dict__


def test_terminal_log_formatter_abbreviates_warning_and_source() -> None:
    """Plain log files use the same aligned, compact columns without color."""
    formatter = TerminalLogFormatter(use_colors=False)
    record = logging.LogRecord(
        "omnigent.codex_native_app_server",
        logging.WARNING,
        __file__,
        1,
        "native-codex: ready",
        (),
        None,
        "native_codex",
    )

    output = formatter.format(record)

    assert re.match(
        r"WARN  \d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} "
        r"codex_native_app_server\s+native_codex\s+\| native-codex: ready",
        output,
    )
    assert "WARNING" not in output
    assert "omnigent.codex_native_app_server" not in output
    assert record.levelname == "WARNING"


def test_terminal_supports_color_no_color_overrides_ambient_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NO_COLOR disables ambient force-color hints, but not Omnigent-owned mirrors."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("CLICOLOR_FORCE", "1")

    assert terminal_supports_color() is False

    monkeypatch.setenv(LOG_FORCE_COLOR_ENV_VAR, "1")

    assert terminal_supports_color() is True


@pytest.mark.skipif(not IS_POSIX, reason="fd-based terminal mirroring is POSIX-only")
def test_terminal_supports_color_checks_explicit_log_fd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Color is enabled only when the explicit mirror fd is a terminal."""
    read_fd, write_fd = os.pipe()
    master_fd: int | None = None
    slave_fd: int | None = None
    try:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv(LOG_TTY_FD_ENV_VAR, str(write_fd))

        assert terminal_supports_color() is False

        monkeypatch.setenv(LOG_FORCE_COLOR_ENV_VAR, "1")

        assert terminal_supports_color() is True

        monkeypatch.delenv(LOG_FORCE_COLOR_ENV_VAR)
        master_fd, slave_fd = os.openpty()
        monkeypatch.setenv(LOG_TTY_FD_ENV_VAR, str(slave_fd))

        assert terminal_supports_color() is True
    finally:
        for fd in (read_fd, write_fd, master_fd, slave_fd):
            if fd is None:
                continue
            with contextlib.suppress(OSError):
                os.close(fd)
