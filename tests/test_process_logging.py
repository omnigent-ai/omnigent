"""Tests for shared process logging helpers."""

from __future__ import annotations

import contextlib
import logging
import os
import re
from pathlib import Path

import pytest

from omnigent._platform import IS_POSIX
from omnigent.process_logging import (
    DATA_DIR_ENV_VAR,
    LOG_FORCE_COLOR_ENV_VAR,
    LOG_TO_STDERR_ENV_VAR,
    LOG_TTY_FD_ENV_VAR,
    PROCESS_LOG_FILE_ENV_VAR,
    TerminalLogFormatter,
    child_logging_popen_kwargs,
    configure_process_logging,
    current_process_log_path,
    process_log_reference,
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


def test_process_log_reference_names_this_process_log_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The reference points at the captured log file, home-collapsed.

    Error messages that tell the reader to "see the runner log" embed this
    string, so it must name the exact file and stay free of the account name.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Pytest temp dir, used as a fake ``$HOME``.
    """
    monkeypatch.setattr("omnigent.process_logging._current_process_log_path", None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    log_path = tmp_path / ".omnigent" / "logs" / "runner" / "runner-conv_ab12.log"
    monkeypatch.setenv(PROCESS_LOG_FILE_ENV_VAR, str(log_path))

    assert process_log_reference("runner") == "~/.omnigent/logs/runner/runner-conv_ab12.log"


def test_process_log_reference_falls_back_to_the_destination_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without a captured log file the reference points at the log directory.

    A runner started with stdio inherited has no log file of its own; the
    error must still say where that destination's logs live.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Pytest temp dir, used as the runtime data dir.
    """
    monkeypatch.setattr("omnigent.process_logging._current_process_log_path", None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "elsewhere")
    monkeypatch.delenv(PROCESS_LOG_FILE_ENV_VAR, raising=False)
    monkeypatch.setenv(DATA_DIR_ENV_VAR, str(tmp_path / "data"))

    assert process_log_reference("runner") == f"{tmp_path / 'data' / 'logs' / 'runner'}/"


def test_configure_process_logging_publishes_its_log_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A process that allocates its own log file reports that file.

    ``current_process_log_path`` is what lets an error name the real log even
    when no parent published one in the environment.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Pytest temp dir holding the log file.
    """
    monkeypatch.setattr("omnigent.process_logging._current_process_log_path", None)
    monkeypatch.delenv(PROCESS_LOG_FILE_ENV_VAR, raising=False)
    log_path = tmp_path / "runner-self-allocated.log"
    logger_name = "omnigent.test_process_logging"

    configure_process_logging(
        "runner",
        log_path=log_path,
        logger_names=(logger_name,),
        root=False,
    )
    try:
        assert current_process_log_path() == log_path
    finally:
        logger = logging.getLogger(logger_name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
