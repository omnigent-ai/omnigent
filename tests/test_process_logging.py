"""Tests for shared process logging helpers."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import time
from pathlib import Path

import pytest

from omnigent._platform import IS_POSIX
from omnigent.process_logging import (
    DATA_DIR_ENV_VAR,
    LOG_FORCE_COLOR_ENV_VAR,
    LOG_TO_STDERR_ENV_VAR,
    LOG_TTY_FD_ENV_VAR,
    PROCESS_LOG_FILE_ENV_VAR,
    RedactingLogFormatter,
    TerminalLogFormatter,
    _debug_sink_target_loggers,
    _log_once_seen,
    _unlink_if_empty,
    child_logging_popen_kwargs,
    configure_process_logging,
    current_process_log_path,
    log_info_once,
    log_once,
    process_log_dir_reference,
    process_log_reference,
    redact_log_text,
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


def test_redact_log_text_filters_labeled_and_unlabeled_token_shapes() -> None:
    """Secrets are removed even when they have no provider-specific prefix."""
    opaque = "aB3" + "xY7" * 12
    labeled = "lowercase123" * 4
    jwt = ".".join(("eyJ" + "HeaderA1" * 2, "PayloadB2" * 2, "SignatureC3" * 2))
    provider_key = "sk-" + "TestKey123" * 2
    workspace_pat = "dapi" + "Test1234567890"

    samples = (
        (f"Authorization: Bearer {labeled}", labeled),
        (f'headers={{"token": "{labeled}"}}', labeled),
        ("bearer abc:def", "abc:def"),
        ("password=;supersecret", ";supersecret"),
        ("password=p@ss,word", "p@ss,word"),
        ('{"password":"p@ss,word"}', "p@ss,word"),
        (f"response contained {opaque}", opaque),
        (f"session credential {jwt}", jwt),
        (f"provider key {provider_key}", provider_key),
        (f"workspace PAT {workspace_pat}", workspace_pat),
    )

    for sample, secret in samples:
        redacted = redact_log_text(sample)
        assert "[REDACTED]" in redacted
        assert secret not in redacted
        assert redact_log_text(redacted) == redacted


def test_redact_log_text_handles_large_non_token_input_in_linear_time() -> None:
    """Punctuation-heavy log lines do not trigger quadratic regex backtracking."""
    text = "+" * 20_000

    started = time.perf_counter()
    output = redact_log_text(text)
    elapsed = time.perf_counter() - started

    assert output == text
    assert elapsed < 0.5


def test_redact_log_text_preserves_normal_identifiers() -> None:
    """UUIDs, session IDs, hashes, and ordinary prose remain useful in logs."""
    values = (
        "conv_0123456789abcdef0123456789abcdef",
        "123e4567-e89b-12d3-a456-426614174000",
        "0123456789abcdef0123456789abcdef01234567",
        "claude-sonnet-4-20250514",
    )

    for value in values:
        assert redact_log_text(value) == value


def test_redacting_log_formatter_scrubs_interpolated_args_and_tracebacks() -> None:
    """Redaction runs after message interpolation and exception rendering."""
    secret = "aB3" + "xY7" * 12
    formatter = RedactingLogFormatter(use_colors=False)
    try:
        raise ValueError(f"token={secret}")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "omnigent.example",
            logging.ERROR,
            __file__,
            1,
            "request failed for %s",
            (secret,),
            sys.exc_info(),
            "send",
        )

    output = formatter.format(record)

    assert secret not in output
    assert output.count("[REDACTED]") >= 2


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


def test_process_log_dir_reference_follows_the_data_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The directory pointer tracks ``OMNIGENT_DATA_DIR``.

    Unlike :func:`process_log_reference` this never substitutes the caller's
    own log file, so a message about another process names that process's
    tree even when this one has a captured log.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Pytest temp dir, used as the runtime data dir.
    """
    monkeypatch.setattr(
        "omnigent.process_logging._current_process_log_path",
        tmp_path / "mine" / "cli.log",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "elsewhere")
    monkeypatch.setenv(DATA_DIR_ENV_VAR, str(tmp_path / "data"))

    assert process_log_dir_reference("host") == f"{tmp_path / 'data' / 'logs' / 'host'}/"


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


def test_configure_process_logging_forwards_custom_debug_log_send(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omnigent import debug_logging

    captured: list[object] = []

    def send(batch: list[dict[str, object]]) -> None:
        captured.append(batch)

    def attach(
        loggers: list[logging.Logger],
        *,
        source: str,
        level: int,
        send: debug_logging.DebugLogSend | None = None,
    ) -> None:
        captured.extend((loggers, source, level, send))

    monkeypatch.setattr(debug_logging, "attach_debug_log_sink", attach)
    logger_name = "omnigent.test_custom_debug_send"
    configure_process_logging(
        "integration",
        log_path=tmp_path / "integration.log",
        level=logging.WARNING,
        logger_names=(logger_name,),
        root=False,
        debug_log_send=send,
    )
    try:
        assert captured[1:] == ["integration", logging.WARNING, send]
    finally:
        logger = logging.getLogger(logger_name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_unlink_if_empty_sweeps_only_empty_files(tmp_path: Path) -> None:
    """The exit sweep removes an empty log, keeps a written one, tolerates absence."""
    empty = tmp_path / "empty.log"
    empty.touch()
    written = tmp_path / "written.log"
    written.write_text("one line\n")

    _unlink_if_empty(empty)
    _unlink_if_empty(written)
    _unlink_if_empty(tmp_path / "missing.log")

    assert not empty.exists()
    assert written.exists()


def test_configure_registers_the_empty_log_sweep_for_self_allocated_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only a self-allocated log path gets the exit sweep.

    A crash before the first record used to leave a fresh empty log
    behind on every start (dozens a day for crash-at-birth hosts). A
    parent-published (env) or explicit ``log_path`` is the caller's to
    manage, so no hook is registered for those.
    """
    registered: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "omnigent.process_logging.atexit.register",
        lambda fn, *args: registered.append((fn, *args)),
    )
    monkeypatch.setattr("omnigent.process_logging._current_process_log_path", None)
    monkeypatch.delenv(PROCESS_LOG_FILE_ENV_VAR, raising=False)
    monkeypatch.setenv(DATA_DIR_ENV_VAR, str(tmp_path))
    logger_name = "omnigent.test_empty_log_sweep"

    path = configure_process_logging("host", logger_names=(logger_name,), root=False)
    try:
        assert registered == [(_unlink_if_empty, path)]

        registered.clear()
        explicit = configure_process_logging(
            "host",
            log_path=tmp_path / "explicit.log",
            logger_names=(logger_name,),
            root=False,
        )
        assert registered == []
        assert explicit == tmp_path / "explicit.log"
    finally:
        logger = logging.getLogger(logger_name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_debug_sink_targets_follow_non_propagating_package_logger() -> None:
    # cli_diagnostics sets our package loggers to propagate=False with their own
    # handlers, so records logged under them never reach root. The debug-log
    # sink (attached to root) must therefore also attach to such loggers, or it
    # sees nothing — the bug that left server/host rows undelivered.
    name = "omnigent.test.sink_target_propagation"
    logger = logging.getLogger(name)
    original = logger.propagate
    try:
        logger.propagate = False
        targets = _debug_sink_target_loggers((name,), root=True)
        assert logging.getLogger() in targets  # root, for propagating loggers
        assert logger in targets  # and the non-propagating package logger itself

        logger.propagate = True
        targets = _debug_sink_target_loggers((name,), root=True)
        # A propagating logger reaches root already; root-only avoids double-ship.
        assert targets == [logging.getLogger()]
    finally:
        logger.propagate = original


def test_log_info_once_dedupes_identical_and_relogs_changed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same formatted line logs once per process; a changed line logs again."""
    logger = logging.getLogger("omnigent.test.log_info_once")
    # The dedup set is process-global; clear it so a prior test cannot mask this.
    _log_once_seen.clear()
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_info_once(logger, "routing decision provider=%s", "alpha")
        log_info_once(logger, "routing decision provider=%s", "alpha")  # identical -> dropped
        log_info_once(logger, "routing decision provider=%s", "beta")  # changed -> logged
    messages = [r.getMessage() for r in caplog.records if r.name == logger.name]
    assert messages == [
        "routing decision provider=alpha",
        "routing decision provider=beta",
    ]


def test_log_once_respects_level_and_captures_exc_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """log_once emits at the given level with the traceback, then dedupes repeats."""
    logger = logging.getLogger("omnigent.test.log_once")
    _log_once_seen.clear()
    with caplog.at_level(logging.WARNING, logger=logger.name):
        try:
            raise ValueError("boom")
        except ValueError:
            log_once(logger, logging.WARNING, "codex catalog probe failed", exc_info=True)
            log_once(logger, logging.WARNING, "codex catalog probe failed", exc_info=True)
    records = [r for r in caplog.records if r.name == logger.name]
    assert len(records) == 1  # identical repeat dropped
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is not None  # first occurrence keeps its traceback
