"""Tests for shared process logging helpers."""

from __future__ import annotations

import contextlib
import io
import logging
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
import weakref
from pathlib import Path

import pytest

from omnigent import process_logging
from omnigent._platform import IS_POSIX
from omnigent.debug_logging import record_to_row
from omnigent.process_logging import (
    DATA_DIR_ENV_VAR,
    LOG_FORCE_COLOR_ENV_VAR,
    LOG_TO_STDERR_ENV_VAR,
    LOG_TTY_FD_ENV_VAR,
    PROCESS_LOG_FILE_ENV_VAR,
    RedactingLogFormatter,
    TerminalLogFormatter,
    TerminalWriter,
    _debug_sink_target_loggers,
    _log_once_seen,
    _unlink_if_empty,
    child_logging_popen_kwargs,
    configure_process_logging,
    current_process_log_path,
    ensure_stdio_survives_unencodable_output,
    log_info_once,
    log_once,
    nonblocking_terminal_stdio,
    process_log_dir_reference,
    process_log_reference,
    redact_log_text,
    terminal_stream_handler,
    terminal_supports_color,
)


@pytest.fixture(autouse=True)
def _isolated_terminal_writers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep one test's terminal writers and mirror mode out of the next test."""
    monkeypatch.setattr(process_logging, "_shared_terminal_writers", {})
    monkeypatch.setattr(process_logging, "_live_terminal_writers", weakref.WeakSet())
    monkeypatch.setattr(process_logging, "_threaded_terminal_mirrors", 0)


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


_LOG_THEN_EXIT = """
import logging, os, sys
from omnigent.process_logging import nonblocking_terminal_stdio, terminal_stream_handler

if OPT_IN:
    nonblocking_terminal_stdio()
handler = terminal_stream_handler()
handler.setFormatter(logging.Formatter("%(message)s"))
for index in range(200):
    handler.handle(logging.makeLogRecord({"msg": "line %d", "args": (index,)}))
EXIT_CALL
"""


@pytest.mark.skipif(not IS_POSIX, reason="fd-based terminal mirroring is POSIX-only")
@pytest.mark.parametrize(
    ("opt_in", "exit_call"),
    [(False, "os._exit(0)"), (True, "sys.exit(0)")],
    ids=["synchronous-then-os-exit", "threaded-then-normal-exit"],
)
def test_terminal_mirror_keeps_lines_logged_just_before_exit(opt_in: bool, exit_call: str) -> None:
    """Outside the opt-in ``os._exit`` loses nothing; inside it the exit drain writes the rest."""
    script = textwrap.dedent(_LOG_THEN_EXIT).replace("OPT_IN", str(opt_in))
    read_fd, write_fd = os.pipe()
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script.replace("EXIT_CALL", exit_call)],
            env={**os.environ, LOG_TTY_FD_ENV_VAR: str(write_fd)},
            pass_fds=(write_fd,),
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        os.close(write_fd)
    with os.fdopen(read_fd, encoding="utf-8") as mirror_output:
        lines = mirror_output.read().splitlines()

    assert completed.returncode == 0, completed.stderr
    assert lines == [f"line {index}" for index in range(200)]


class _FakeTerminal(io.StringIO):
    """In-memory terminal whose writes can be held until the test releases them."""

    def __init__(self, *, tty: bool = True, fail_with: Exception | None = None) -> None:
        super().__init__()
        self._tty = tty
        self.fail_with = fail_with
        self.reading = threading.Event()
        self.reading.set()
        self.write_calls = 0

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        self.write_calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        self.reading.wait()
        return super().write(text)


def _record(message: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, message, args, None)


@pytest.mark.skipif(not IS_POSIX, reason="fd-based terminal mirroring is POSIX-only")
def test_terminal_stream_handler_does_not_wait_for_a_stalled_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Logging continues while the terminal is not read; skipped lines are reported later."""
    monkeypatch.setattr(process_logging, "_TERMINAL_BACKLOG_CHARS", 4096)
    read_fd, write_fd = os.pipe()  # nothing reads until every line is logged
    handler: logging.Handler | None = None
    # As in the foreground host, whose terminal mirrors write from a thread.
    restore = nonblocking_terminal_stdio()
    try:
        monkeypatch.setenv(LOG_TTY_FD_ENV_VAR, str(write_fd))
        handler = terminal_stream_handler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        assert isinstance(handler, logging.StreamHandler)
        writer = handler.stream
        assert isinstance(writer, TerminalWriter)

        # About 200 KB: more than the pipe buffer plus the backlog.
        logging_thread = threading.Thread(
            target=lambda: [
                handler.handle(_record("line %04d %s", index, "x" * 90)) for index in range(2000)
            ],
            daemon=True,
        )
        logging_thread.start()
        logging_thread.join(10)
        assert not logging_thread.is_alive(), "logging waited for the stalled terminal"

        # The terminal reads again. Nothing else is logged, so drops are reported by the writer.
        output = bytearray()
        os.set_blocking(read_fd, False)
        deadline = time.monotonic() + 10
        while not writer.drain(0.05):
            assert time.monotonic() < deadline, "queued output was never written"
            with contextlib.suppress(BlockingIOError):
                output.extend(os.read(read_fd, 65536))
        with contextlib.suppress(BlockingIOError):
            while chunk := os.read(read_fd, 65536):
                output.extend(chunk)
    finally:
        restore()
        if handler is not None:
            handler.close()
        for fd in (read_fd, write_fd):
            with contextlib.suppress(OSError):
                os.close(fd)

    shown = output.decode().splitlines()
    # Every line is either shown in order or counted by a notice where it was dropped.
    next_index = 0
    notices = 0
    for text in shown:
        notice = re.fullmatch(
            r"\[omnigent\] (\d+) lines? (?:was|were) not shown because this terminal "
            r"stopped reading output; any log lines among them are in the log file\.",
            text,
        )
        if notice is not None:
            notices += 1
            next_index += int(notice.group(1))
        else:
            assert text == f"line {next_index:04d} {'x' * 90}"
            next_index += 1
    # Trailing drops are counted only through the writer's catch-up notice.
    assert next_index == 2000
    assert notices > 0


@pytest.mark.skipif(not IS_POSIX, reason="fd-based terminal mirroring is POSIX-only")
def test_terminal_stream_handlers_share_one_writer_per_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors of one stream or mirror fd share a writer, so their lines keep the order logged."""
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(sys, "stderr", _FakeTerminal())
    restore = nonblocking_terminal_stdio()
    try:
        monkeypatch.setenv(LOG_TTY_FD_ENV_VAR, str(write_fd))
        first, second = terminal_stream_handler(), terminal_stream_handler()
        assert isinstance(first, logging.StreamHandler)
        assert isinstance(second, logging.StreamHandler)
        writer = first.stream
        assert isinstance(writer, TerminalWriter)
        assert second.stream is writer
        for handler in (first, second):
            handler.setFormatter(logging.Formatter("%(message)s"))
        for index in range(20):
            (first, second)[index % 2].handle(_record("line %d", index))
        assert writer.drain(5)
        assert os.read(read_fd, 65536).decode().splitlines() == [f"line {i}" for i in range(20)]

        monkeypatch.delenv(LOG_TTY_FD_ENV_VAR)
        mirror = terminal_stream_handler()
        assert isinstance(mirror, logging.StreamHandler)
        assert mirror.stream is sys.stderr
    finally:
        restore()
        for fd in (read_fd, write_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


@pytest.mark.parametrize(
    "error",
    [OSError(5, "Input/output error"), UnicodeEncodeError("ascii", "✓", 0, 1, "not ASCII")],
    ids=["hang-up", "unencodable"],
)
def test_terminal_writer_stops_writing_after_a_terminal_error(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """A terminal write error ends mirroring instead of retrying or raising to the caller."""
    terminal = _FakeTerminal(fail_with=error)
    writer = TerminalWriter(terminal)

    writer.write("first\n")
    assert writer.drain(5)
    writer.write("second\n")
    assert writer.drain(5)

    assert terminal.write_calls == 1
    assert not writer.writable()
    # The log file records why the terminal went quiet; the writer thread logs it after failing.
    deadline = time.monotonic() + 5
    while "terminal output stopped after a write error" not in caplog.text:
        assert time.monotonic() < deadline, "the stopped mirror was never logged"
        time.sleep(0.01)
    assert type(error).__name__ in caplog.text
    assert "✓" not in caplog.text  # the rejected text stays out of the log file


def test_terminal_writer_stops_when_it_cannot_reopen_after_fork() -> None:
    """A forked child that can't get its own fd must not write to the inherited stream."""
    writer = TerminalWriter(_FakeTerminal())  # no fileno() to duplicate

    writer._reopen_after_fork()

    assert not writer.writable()


def test_terminal_writer_keeps_partial_lines_until_flush() -> None:
    """Text without a newline is written once it is flushed, as ``print(..., end="")`` expects."""
    terminal = _FakeTerminal()
    writer = TerminalWriter(terminal)

    print("Waiting", end="", file=writer)
    time.sleep(0.05)  # time for the writer thread to write, had the text been queued
    assert terminal.getvalue() == ""
    print(" for login...", end="", file=writer, flush=True)
    assert writer.drain(5)

    assert terminal.getvalue() == "Waiting for login..."


def test_terminal_writer_drains_even_when_its_character_count_is_off() -> None:
    """An interrupt between queueing a chunk and counting or announcing it can't hang drain()."""
    terminal = _FakeTerminal()
    writer = TerminalWriter(terminal)
    writer.write("first\n")
    assert writer.drain(5)
    with writer._condition:  # as if interrupted after the append, before the count and notify
        writer._chunks.append("second\n")

    assert writer.drain(5)
    assert terminal.getvalue() == "first\nsecond\n"


def test_terminal_writer_keeps_writing_after_a_refused_thread_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused writer thread (e.g. at shutdown) drops that output, not later output."""
    terminal = _FakeTerminal()
    writer = TerminalWriter(terminal)
    real_start = threading.Thread.start
    refusals = [RuntimeError("can't create new thread at interpreter shutdown")]

    def start(thread: threading.Thread) -> None:
        if refusals and thread.name == "omnigent-terminal-writer":
            raise refusals.pop()
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    writer.write("refused\n")
    writer.write("written\n")
    assert writer.drain(5)

    assert terminal.getvalue() == "written\n"


def test_terminal_writer_bounds_text_without_a_newline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Long output without a newline or flush is written instead of growing in memory."""
    monkeypatch.setattr(process_logging, "_TERMINAL_PARTIAL_LINE_CHARS", 16)
    terminal = _FakeTerminal()
    writer = TerminalWriter(terminal)

    for _ in range(10):
        writer.write("\r#####")

    # Every third 6-character write reaches the 16-character limit and is queued.
    deadline = time.monotonic() + 5
    while len(terminal.getvalue()) < 54:
        assert time.monotonic() < deadline, "partial line was never queued"
        time.sleep(0.01)
    assert terminal.getvalue() == "\r#####" * 9
    assert writer.drain(5)
    assert terminal.getvalue() == "\r#####" * 10


_FORK_WHILE_WRITING = """
import os, select, sys, threading, time, warnings
from omnigent.process_logging import TerminalWriter

read_fd, write_fd = os.pipe()
writer = TerminalWriter(os.fdopen(write_fd, "w", buffering=1))
# More than the pipe holds: the writer thread blocks mid-write, holding the stream's lock.
writer.write("x" * 70000 + "\\n")
time.sleep(0.5)
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    pid = os.fork()
if pid == 0:
    writer.write("from the child\\n")
    EXIT_CALL(0 if writer.drain(10) else 1)
output = bytearray()
deadline = time.monotonic() + 10
while b"from the child\\n" not in output and time.monotonic() < deadline:
    if select.select([read_fd], [], [], max(0.0, deadline - time.monotonic()))[0]:
        output.extend(os.read(read_fd, 65536))
_, status = os.waitpid(pid, 0)
print(os.waitstatus_to_exitcode(status), b"from the child\\n" in output)
"""


@pytest.mark.skipif(not IS_POSIX, reason="os.fork is POSIX-only")
@pytest.mark.parametrize("exit_call", ["os._exit", "sys.exit"])
def test_terminal_writer_in_a_child_forked_mid_write_writes_to_a_fresh_fd(exit_call: str) -> None:
    """A child forked while a parent writer holds the stream's lock still writes and exits."""
    script = textwrap.dedent(_FORK_WHILE_WRITING).replace("EXIT_CALL", exit_call)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["0", "True"]


def test_terminal_writer_shows_one_write_larger_than_the_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backlog limits queued text; a large write to an idle terminal is still shown."""
    monkeypatch.setattr(process_logging, "_TERMINAL_BACKLOG_CHARS", 64)
    terminal = _FakeTerminal()
    writer = TerminalWriter(terminal)
    traceback_text = "".join(f'  File "module_{index}.py", line {index}\n' for index in range(20))

    writer.write(traceback_text)
    assert writer.drain(5)

    assert terminal.getvalue() == traceback_text


def test_nonblocking_terminal_stdio_wraps_only_terminals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Terminal stdio is routed through a writer and restored once its output is written."""
    terminal = _FakeTerminal()
    log_file = _FakeTerminal(tty=False)
    monkeypatch.setattr(sys, "stdout", terminal)
    monkeypatch.setattr(sys, "stderr", log_file)

    restore = nonblocking_terminal_stdio()
    assert isinstance(sys.stdout, TerminalWriter)
    assert sys.stderr is log_file
    print("✓ Connected")
    # A stream another component replaced meanwhile stays theirs; stdout is still restored.
    replacement = io.StringIO()
    monkeypatch.setattr(sys, "stderr", replacement)
    restore()

    assert sys.stdout is terminal
    assert sys.stderr is replacement
    assert terminal.getvalue() == "✓ Connected\n"


@pytest.mark.skipif(not IS_POSIX, reason="fd-based terminal mirroring is POSIX-only")
def test_nonblocking_terminal_stdio_routes_handlers_that_already_write_to_the_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handlers attached before the host starts, e.g. under ``--log-to-stderr``, are moved too.

    They are handed back only once every writer has caught up: a mirror blocked mid-write
    holds its stream's lock, so its handler would block on the next line.
    """
    monkeypatch.setattr(process_logging, "_TERMINAL_EXIT_DRAIN_S", 0.05)
    read_fd, write_fd = os.pipe()  # nothing reads until the restore has been refused
    logger = logging.getLogger("tests.terminal_routing")
    terminal = _FakeTerminal()
    monkeypatch.setattr(sys, "stderr", terminal)
    monkeypatch.setenv(LOG_TTY_FD_ENV_VAR, str(write_fd))
    stderr_handler = logging.StreamHandler(sys.stderr)
    mirror = terminal_stream_handler()  # synchronous: opened before the opt-in
    other_mirror = terminal_stream_handler()  # a second one, on its own dup of the same fd
    assert isinstance(mirror, logging.StreamHandler)
    assert isinstance(other_mirror, logging.StreamHandler)
    mirror_stream, other_mirror_stream = mirror.stream, other_mirror.stream
    log_file = logging.FileHandler(os.devnull)
    handlers: list[logging.Handler] = [stderr_handler, mirror, other_mirror, log_file]
    for handler in handlers:
        logger.addHandler(handler)
    restore = nonblocking_terminal_stdio()
    try:
        assert stderr_handler.stream is sys.stderr
        assert isinstance(sys.stderr, TerminalWriter)
        writer = mirror.stream
        assert isinstance(writer, TerminalWriter)
        assert other_mirror.stream is writer
        assert not isinstance(log_file.stream, TerminalWriter)
        # A mirror opened later for the same file shares the existing mirror's writer.
        later = terminal_stream_handler()
        assert isinstance(later, logging.StreamHandler)
        assert later.stream is writer
        logger.addHandler(later)
        handlers.append(later)

        # More than the pipe holds, so the mirror's writer blocks mid-write.
        for index in range(100):
            mirror.handle(_record("line %d %s", index, "x" * 1000))
        restore()
        assert mirror.stream is writer
        assert isinstance(sys.stderr, TerminalWriter)

        # The terminal reads again; now everything is handed back, once.
        os.set_blocking(read_fd, False)
        deadline = time.monotonic() + 10
        while not writer.drain(0.05):
            assert time.monotonic() < deadline, "queued output was never written"
            with contextlib.suppress(BlockingIOError):
                os.read(read_fd, 65536)
        restore()
        assert sys.stderr is terminal
        assert stderr_handler.stream is terminal
        assert mirror.stream is mirror_stream
        assert other_mirror.stream is other_mirror_stream
        assert not isinstance(later.stream, TerminalWriter)
        assert process_logging._threaded_terminal_mirrors == 0
        restore()
        assert process_logging._threaded_terminal_mirrors == 0
    finally:
        with contextlib.suppress(OSError):
            os.close(read_fd)  # unblocks a writer the test left blocked
        restore()
        for handler in handlers:
            logger.removeHandler(handler)
        log_file.close()
        with contextlib.suppress(OSError):
            os.close(write_fd)


@pytest.mark.skipif(not IS_POSIX, reason="fd-based terminal mirroring is POSIX-only")
def test_terminal_mirror_closes_its_dup_when_it_cannot_open_the_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mirror that fails after duplicating its fd closes the duplicate."""
    read_fd, write_fd = os.pipe()
    real_key = process_logging._mirror_fd_key
    keyed: list[int] = []

    def key_fails_for_the_duplicate(fd: int) -> tuple[object, ...]:
        keyed.append(fd)
        if len(keyed) == 2:
            raise OSError(9, "Bad file descriptor")
        return real_key(fd)

    monkeypatch.setattr(process_logging, "_mirror_fd_key", key_fails_for_the_duplicate)
    monkeypatch.setenv(LOG_TTY_FD_ENV_VAR, str(write_fd))
    restore = nonblocking_terminal_stdio()
    try:
        # dup() returns the lowest free fd, so a leaked duplicate would take this number.
        lowest_free = os.dup(write_fd)
        os.close(lowest_free)
        handler = terminal_stream_handler()
        again = os.dup(write_fd)
        os.close(again)
    finally:
        restore()
        for fd in (read_fd, write_fd):
            os.close(fd)

    assert isinstance(handler, logging.NullHandler)
    assert len(keyed) == 2
    assert again == lowest_free


def test_nonblocking_terminal_stdio_nested_installs_restore_latest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inner restore leaves the outer install in place; the outer one puts everything back."""
    terminal = _FakeTerminal()
    monkeypatch.setattr(sys, "stdout", terminal)
    logger = logging.getLogger("tests.terminal_nesting")
    handler = logging.StreamHandler(terminal)
    logger.addHandler(handler)
    try:
        outer = nonblocking_terminal_stdio()
        writer = sys.stdout
        assert isinstance(writer, TerminalWriter)
        inner = nonblocking_terminal_stdio()
        assert sys.stdout is writer
        assert handler.stream is writer
        assert process_logging._threaded_terminal_mirrors == 2

        inner()
        assert sys.stdout is writer
        assert handler.stream is writer
        assert process_logging._threaded_terminal_mirrors == 1

        outer()
        assert sys.stdout is terminal
        assert handler.stream is terminal
        assert process_logging._threaded_terminal_mirrors == 0
    finally:
        logger.removeHandler(handler)


def test_nonblocking_terminal_stdio_keeps_the_writer_while_the_terminal_is_stalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring a terminal that still is not reading would let the exit-time flush block."""
    monkeypatch.setattr(process_logging, "_TERMINAL_EXIT_DRAIN_S", 0.05)
    terminal = _FakeTerminal()
    terminal.reading.clear()
    monkeypatch.setattr(sys, "stdout", terminal)

    restore = nonblocking_terminal_stdio()
    writer = sys.stdout
    try:
        print("↓ Runner stopped", flush=True)
        restore()
        assert sys.stdout is writer
        # The exit-time drain does not wait again for a terminal that already timed out.
        started = time.monotonic()
        assert not process_logging.drain_terminal_output(5)
        assert time.monotonic() - started < 1
    finally:
        terminal.reading.set()
    assert isinstance(writer, TerminalWriter)
    assert writer.drain(5)
    assert terminal.getvalue() == "↓ Runner stopped\n"


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


@pytest.mark.parametrize("include_whitespace_credentials", [False, True])
def test_redact_log_text_filters_labeled_and_unlabeled_token_shapes(
    include_whitespace_credentials: bool,
) -> None:
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
        redacted = redact_log_text(
            sample, include_whitespace_credentials=include_whitespace_credentials
        )
        assert "[REDACTED]" in redacted
        assert secret not in redacted
        assert (
            redact_log_text(
                redacted, include_whitespace_credentials=include_whitespace_credentials
            )
            == redacted
        )


@pytest.mark.parametrize(
    "message",
    [
        "auth token invalidation failed",
        "password rotation failed",
        "client secret validation pending",
        "db-credential resolver unavailable",
        "DATABASE_PASSWORD synthetic-value",
    ],
)
def test_default_app_logs_skip_whitespace_credential_matching(message: str) -> None:
    """Application logs keep prose while still redacting explicit secret assignments."""
    assert redact_log_text(message) == message
    raw = f"{message}; password=synthetic-assigned-value"
    expected = f"{message}; password=[REDACTED]"
    record = logging.LogRecord("omnigent.example", logging.ERROR, __file__, 1, "%s", (raw,), None)
    record.exc_text = f"ValueError: {raw}"
    record.attributes = {"detail": raw}

    formatter = RedactingLogFormatter(fmt="%(message)s", use_colors=False)
    assert formatter.format(record) == f"{expected}\nValueError: {expected}"
    row = record_to_row(record, source="runner")
    assert row["message"] == expected
    assert row["stack_trace"] == f"ValueError: {expected}"
    assert row["attributes"] == {"detail": expected}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("--token=example", "--token=[REDACTED]"),
        (".token=example", ".token=[REDACTED]"),
        ("..access-token=example", "..access-token=[REDACTED]"),
        ("foo.token=example", "foo.token=[REDACTED]"),
        ("foo-token=example", "foo-token=[REDACTED]"),
        ("_token=example", "_token=[REDACTED]"),
        ("(--password = example )", "(--password = [REDACTED] )"),
        ('{"access_token": "some secret"}', '{"access_token": "[REDACTED]"}'),
        ("{'api-key': 'some secret'}", "{'api-key': '[REDACTED]'}"),
        ("api key: some-secret status=401", "api key: [REDACTED] status=401"),
        ("api key: Bearer short-value status=401", "api key: [REDACTED] status=401"),
        ('"api key" = "some secret"', '"api key" = "[REDACTED]"'),
        ('".db-credential"="example"', '".db-credential"="[REDACTED]"'),
    ],
)
@pytest.mark.parametrize("include_whitespace_credentials", [False, True])
def test_redact_log_text_preserves_named_secret_keys(
    text: str, expected: str, include_whitespace_credentials: bool
) -> None:
    """Punctuation, underscores, and quotes around keys survive value redaction."""
    redacted = redact_log_text(text, include_whitespace_credentials=include_whitespace_credentials)
    assert redacted == expected
    assert (
        redact_log_text(redacted, include_whitespace_credentials=include_whitespace_credentials)
        == redacted
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ERROR failed: password hunter2", "ERROR failed: password [REDACTED]"),
        (
            "ERROR authentication failed: invalid api key a1b2c3d4e5f60718293a4b5c6d7e8f90",
            "ERROR authentication failed: invalid api key [REDACTED]",
        ),
        ("password supersecret status=401", "password [REDACTED] status=401"),
        ("PASSWORD\thunter2 status=401", "PASSWORD\t[REDACTED] status=401"),
        ('"password" "hunter2"', '"password" "[REDACTED]"'),
        ("API\t KEY  hunter2", "API\t KEY  [REDACTED]"),
        ('password "some secret" rejected', 'password "[REDACTED]" rejected'),
        ("--api-key 'some secret' rejected", "--api-key '[REDACTED]' rejected"),
        ("api_key lowercasesecret status=403", "api_key [REDACTED] status=403"),
        ("access token synthetic-access-value", "access token [REDACTED]"),
        ("refresh_token synthetic-refresh-value", "refresh_token [REDACTED]"),
        ("auth-token synthetic-auth-value", "auth-token [REDACTED]"),
        ("auth token Bearer short-value status=401", "auth token [REDACTED] status=401"),
        ("client secret synthetic-client-value", "client secret [REDACTED]"),
        ("passwd p@ss,word rejected", "passwd [REDACTED] rejected"),
        ("password expired", "password [REDACTED]"),
        ('password "is missing"', 'password "[REDACTED]"'),
        ("password=is missing", "password=[REDACTED] missing"),
        (
            "password hunter2 api key abcdef012345 status=401",
            "password [REDACTED] api key [REDACTED] status=401",
        ),
    ],
)
def test_redact_log_text_handles_whitespace_credentials(text: str, expected: str) -> None:
    """Explicit credential labels hide even short/plain values without losing context."""
    redacted = redact_log_text(text, include_whitespace_credentials=True)
    assert redacted == expected
    assert redact_log_text(redacted, include_whitespace_credentials=True) == redacted


@pytest.mark.parametrize(
    "label",
    [
        "DATABASE_PASSWORD",
        "databasepassword",
        "_password",
        ".password",
        "db.password",
        "db-password",
        "--db-password",
        "db.PASSWD",
        "PROVIDER_API_KEY",
        "provider.api key",
        "service.client secret",
        "SESSION_TOKEN",
        "db.secret",
        "db-credential",
        "token.secret",
        ".token",
        "_token",
        "--credential",
    ],
)
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("synthetic-value", "[REDACTED]"),
        ("'synthetic value'", "'[REDACTED]'"),
        ("Bearer synthetic-value", "[REDACTED]"),
    ],
)
def test_redact_log_text_handles_prefixed_whitespace_credentials(
    label: str, value: str, expected: str
) -> None:
    """Environment, config, and CLI credential keys retain their complete prefix."""
    text = f"ERROR failed: {label} {value} status=401"
    redacted = redact_log_text(text, include_whitespace_credentials=True)
    assert redacted == f"ERROR failed: {label} {expected} status=401"
    assert redact_log_text(redacted, include_whitespace_credentials=True) == redacted


@pytest.mark.parametrize(
    "text",
    [
        "ERROR password authentication failed for user example",
        "ERROR api key is missing; configure the provider",
        "ERROR refresh token has expired; authenticate again",
        "ERROR client secret was not configured",
        "ERROR access token validation failed",
        "ERROR token refresh failed",
        "WARN token budget exceeded; token count 8192",
        "ERROR credential resolution failed",
        "ERROR secret provider unavailable",
        "WARN TOKEN usage 8192; SECRET provider failed; CREDENTIAL resolution failed",
        "ERROR DATABASE_PASSWORD is missing",
        "ERROR PROVIDER_API_KEY was not configured",
        "ERROR app.auth-token has expired",
        "ERROR db.password authentication failed for user example",
        "ERROR service.credential validation failed",
        "WARN password_file unavailable; api_key_path not found",
        "WARN database_password_file unavailable; provider.api_key_path not found",
        "WARN prefix_token_count 8192",
        "ERROR password\nconnection refused",
        "ERROR api\nkey unavailable",
    ],
)
@pytest.mark.parametrize("include_whitespace_credentials", [False, True])
def test_redact_log_text_preserves_credential_diagnostic_prose(
    text: str, include_whitespace_credentials: bool
) -> None:
    """Credential diagnostics and token accounting are not whitespace assignments."""
    assert (
        redact_log_text(text, include_whitespace_credentials=include_whitespace_credentials)
        == text
    )


@pytest.mark.parametrize(
    "text",
    [
        "+" * 20_000,
        "a-" * 8_192,
        ("ERROR " + "a-" * 9_000)[:16_384],
        ("synthetic-token-marker" * 4_000)[:65_536],
        ("synthetic.token.marker" * 4_000)[:65_536],
        "api" + " " * 65_000 + "key is missing",
        "password is missing; api key is missing\n" * 1_600,
    ],
    ids=[
        "punctuation",
        "short-hyphenated",
        "error-short-hyphenated",
        "hyphenated",
        "dotted",
        "spaced-label",
        "diagnostic-prose",
    ],
)
@pytest.mark.parametrize("include_whitespace_credentials", [False, True])
def test_redact_log_text_handles_large_non_token_input_in_linear_time(
    text: str, include_whitespace_credentials: bool
) -> None:
    """Long possible key names do not trigger quadratic regex backtracking."""

    # Measure this thread's CPU work, excluding parallel CI scheduling delays.
    started = time.thread_time()
    output = redact_log_text(text, include_whitespace_credentials=include_whitespace_credentials)
    elapsed = time.thread_time() - started

    assert output == text
    assert elapsed < 0.5


def test_redact_log_text_handles_large_bearer_token_in_linear_time() -> None:
    """A maximum-size stderr line can be redacted without blocking the runner."""
    text = "Bearer " + ("synthetic-token-marker" * 4_000)[:65_529]

    started = time.thread_time()
    output = redact_log_text(text)
    elapsed = time.thread_time() - started

    assert output == "Bearer [REDACTED]"
    assert elapsed < 0.5


@pytest.mark.parametrize(
    "label", ["password", "api key", "access token", "DATABASE_PASSWORD", "provider.token"]
)
@pytest.mark.parametrize("value", ["synthetic-token-marker", "synthetic.token.marker"])
def test_redact_log_text_handles_large_whitespace_credentials_in_linear_time(
    label: str, value: str
) -> None:
    """Maximum-size credential values retain bounded redaction cost on repeated passes."""
    text = f"ERROR failed: {label} " + (value * 4_000)[:65_000] + " status=401"

    started = time.thread_time()
    output = redact_log_text(text, include_whitespace_credentials=True)
    repeated = redact_log_text(output, include_whitespace_credentials=True)
    elapsed = time.thread_time() - started

    assert output == f"ERROR failed: {label} [REDACTED] status=401"
    assert repeated == output
    assert elapsed < 0.5


@pytest.mark.parametrize("prefix", ["a_", "a.", "a-"])
@pytest.mark.parametrize("suffix", ["PASSWORD", "TOKEN"])
def test_redact_log_text_handles_large_prefixed_keys_in_linear_time(
    prefix: str, suffix: str
) -> None:
    """Key prefixes are scanned only from their start, including on repeated redaction."""
    label = prefix * 32_000 + suffix
    text = f"ERROR failed: {label} synthetic-value status=401"

    started = time.thread_time()
    output = redact_log_text(text, include_whitespace_credentials=True)
    repeated = redact_log_text(output, include_whitespace_credentials=True)
    elapsed = time.thread_time() - started

    assert output == f"ERROR failed: {label} [REDACTED] status=401"
    assert repeated == output
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
        "omnigent.harnesses.codex_native.app_server",
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
        r"codex_native\.app_server\s+native_codex\s+\| native-codex: ready",
        output,
    )
    assert "WARNING" not in output
    assert "omnigent.harnesses.codex_native.app_server" not in output
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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see https://u:p@h.example/x", "see https://[REDACTED]@h.example/x"),
        ("git clone ssh://git@github.com/o/r", "git clone ssh://[REDACTED]@github.com/o/r"),
        ("plain https://h.example/p?q=1#f", "plain https://h.example/p?q=1#f"),
        ("an email a@b.c and a path /x@y", "an email a@b.c and a path /x@y"),
    ],
)
def test_redact_log_text_strips_url_userinfo(text: str, expected: str) -> None:
    assert redact_log_text(text) == expected


def test_redact_log_text_handles_large_url_without_userinfo_in_linear_time() -> None:
    text = "https://" + "a" * 200_000 + " " + "://" * 50_000
    started = time.thread_time()
    output = redact_log_text(text)
    elapsed = time.thread_time() - started

    assert output == text
    assert elapsed < 0.5


class _FakeStream:
    """A stdio stub recording ``reconfigure`` calls for the stdio-hardening tests."""

    def __init__(self, encoding: str, *, raises: bool = False) -> None:
        self.encoding = encoding
        self.raises = raises
        self.reconfigured: dict[str, object] | None = None

    def reconfigure(self, **kwargs: object) -> None:
        if self.raises:
            raise ValueError("stream detached")
        self.reconfigured = kwargs
        self.encoding = str(kwargs.get("encoding", self.encoding))


def test_stdio_hardening_reconfigures_legacy_windows_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows, non-UTF-8 stdio is reconfigured to utf-8/replace."""
    monkeypatch.setattr(sys, "platform", "win32")
    out, err = _FakeStream("cp1252"), _FakeStream("cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    ensure_stdio_survives_unencodable_output()

    assert out.reconfigured == {"encoding": "utf-8", "errors": "replace"}
    assert err.reconfigured == {"encoding": "utf-8", "errors": "replace"}


def test_stdio_hardening_relaxes_errors_on_posix_legacy_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy-encoding stream off Windows keeps its encoding, relaxed errors.

    ``PYTHONIOENCODING=cp1252`` (or a C/latin-1 locale) reproduces the same
    ``UnicodeEncodeError`` on any OS; the stream must degrade unencodable
    glyphs instead of aborting, without changing the encoding the consumer
    asked for.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    out, err = _FakeStream("cp1252"), _FakeStream("cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    ensure_stdio_survives_unencodable_output()

    assert out.reconfigured == {"errors": "replace"}
    assert err.reconfigured == {"errors": "replace"}
    assert out.encoding == "cp1252"


def test_stdio_hardening_skips_already_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """A UTF-8 stream is not reconfigured again (any platform)."""
    for platform in ("win32", "linux"):
        monkeypatch.setattr(sys, "platform", platform)
        out = _FakeStream("utf-8")
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", _FakeStream("utf-8"))

        ensure_stdio_survives_unencodable_output()

        assert out.reconfigured is None


def test_stdio_hardening_tolerates_reconfigure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream whose ``reconfigure`` raises does not abort startup."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", _FakeStream("cp1252", raises=True))
    monkeypatch.setattr(sys, "stderr", _FakeStream("cp1252", raises=True))

    ensure_stdio_survives_unencodable_output()  # must not raise
