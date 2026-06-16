"""
Always-on CLI diagnostics log.

Captures exceptions, warnings, and diagnostic info to a per-invocation
log file under ``~/.omnigent/logs/cli-*.log``. Separate from the
``--log`` conversation JSON transcript and the ``--debug-events`` SSE
tape — this layer is always on so crash context is available even when
the user didn't know to enable debugging ahead of time.

**Privacy contract:** At ``INFO`` level, no user prompts, message text,
tool arguments, or conversation content are logged. Only lifecycle
events (startup, shutdown, error tracebacks) appear. A redaction
filter strips obvious secrets (``Authorization`` headers, bearer
tokens, env vars matching ``*_TOKEN`` / ``*_API_KEY`` / ``*SECRET*``,
``sk-*``, ``dapi*``). Redaction runs on the fully-formatted output
(after ``%``-interpolation and traceback rendering) so secrets in
``logger.info("key=%s", val)`` args and exception frames are covered.

Log files are created with ``0o600`` permissions and pruned to keep
at most :data:`MAX_LOG_FILES` entries. A ``latest-cli.log`` symlink
is maintained for quick access.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import cast

from omnigent_ui_sdk import state_dir

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Subdirectory under :func:`state_dir` for CLI diagnostic logs.
_LOGS_SUBDIR = "logs"

#: Maximum number of ``cli-*.log`` files kept before pruning.
MAX_LOG_FILES = 20

#: Per-file size cap before rotation (bytes).
MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB

#: Backup count for the rotating handler (per invocation — rarely
#: hits this, but guards runaway loops).
_BACKUP_COUNT = 1

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CliLogContext:
    """
    Returned by :func:`setup_cli_logging` to let callers reference
    the log path (e.g. in error messages, the Ctrl+O debug overlay,
    or ``/report``).

    :param path: Absolute path to the current invocation's log file,
        e.g. ``~/.omnigent/logs/cli-20260518-143012-12345-a1b2c3.log``.
    :param invocation_id: Short unique id for this CLI run, e.g.
        ``"12345-a1b2c3"``.
    """

    path: Path
    invocation_id: str


# Module-level holder so ``current_cli_log_path()`` works without
# threading the context through every call site.
_current: CliLogContext | None = None


@dataclass(frozen=True)
class _LoggingStreamSnapshot:
    """
    Original stream for a logging handler retargeted during TUI stderr capture.

    :param handler: Stream handler whose output was redirected.
    :param stream: Stream the handler wrote to before redirection.
    """

    handler: logging.StreamHandler[io.TextIOBase]
    stream: io.TextIOBase


_redirected_logging_streams: list[_LoggingStreamSnapshot] = []

# ---------------------------------------------------------------------------
# Secret redaction filter
# ---------------------------------------------------------------------------

#: Patterns that match values likely to be secrets.  Applied to every
#: log record's formatted message before it hits the file.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # Header values: "Authorization: Bearer xxx" or "bearer xxx"
    re.compile(r"(?i)(authorization\s*[:=]\s*)\S+"),
    re.compile(r"(?i)(bearer\s+)\S+"),
    # Env-var style keys: FOO_TOKEN=xxx, FOO_API_KEY=xxx, ...
    re.compile(r"(?i)(\b\w*(?:token|api_key|secret|password)\s*[:=]\s*)\S+"),
    # Anthropic / OpenAI style keys
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"),
    # Databricks PATs
    re.compile(r"\bdapi[A-Za-z0-9]{10,}\b"),
]
_REDACTED = "[REDACTED]"


def _redact(text: str) -> str:
    """
    Replace secret-shaped substrings in *text* with :data:`_REDACTED`.

    :param text: Arbitrary log text (may include tracebacks).
    :returns: Scrubbed text.
    """
    for pat in _SECRET_PATTERNS:
        text = pat.sub(
            lambda m: m.group(1) + _REDACTED if m.lastindex else _REDACTED,
            text,
        )
    return text


class _RedactingFormatter(logging.Formatter):
    """
    Formatter that scrubs obvious secrets from the *final* formatted
    output — after ``%``-interpolation of ``record.args`` and after
    traceback rendering.

    A ``logging.Filter`` on ``record.msg`` would run *before*
    formatting, so secrets passed as ``logger.info("key=%s", secret)``
    or appearing in exception tracebacks would slip through.
    Overriding :meth:`format` is the correct interception point
    because the base class returns the fully-assembled string
    (message + traceback) and nothing downstream mutates it before
    the handler writes.
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        Format *record* then redact secrets from the result.

        :param record: The log record to format.
        :returns: Formatted, redacted string ready for the handler.
        """
        return _redact(super().format(record))


class _RedactingStderr(io.TextIOBase):
    """
    Text stream that redirects stderr writes to the CLI log with redaction.

    :param inner: Open log file handle receiving redirected stderr writes.
    :param original: Original terminal stderr stream to restore after the TUI
        exits.
    """

    def __init__(self, inner: io.TextIOWrapper, original: io.TextIOBase) -> None:
        """
        Create a redacting wrapper around an open log file.

        :param inner: Log file handle opened in append text mode.
        :param original: Original terminal stderr stream saved for restoration.
        """
        self._inner = inner
        self._original_stderr = original

    def write(self, text: str) -> int:
        """
        Redact and write a stderr text chunk to the log file.

        :param text: Text sent to ``sys.stderr.write``.
        :returns: The length of the caller's original text.
        """
        self._inner.write(_redact(text))
        return len(text)

    def flush(self) -> None:
        """
        Flush the wrapped log file.

        :returns: ``None``.
        """
        self._inner.flush()

    def close(self) -> None:
        """
        Close the wrapped log file.

        :returns: ``None``.
        """
        if self.closed:
            return
        super().close()
        if not self._inner.closed:
            self._inner.close()

    def isatty(self) -> bool:
        """
        Report that redirected stderr is not an interactive terminal.

        :returns: Always ``False`` because writes are redirected to a file.
        """
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _log_dir() -> Path:
    """
    Return the CLI diagnostics log directory.

    Uses :func:`omnigent_ui_sdk.state_dir` as the shared
    ``~/.omnigent`` root so the path is defined in one place.

    :returns: ``~/.omnigent/logs``.
    """
    return Path(state_dir()) / _LOGS_SUBDIR


def setup_cli_logging(argv: list[str]) -> CliLogContext:
    """
    Configure the always-on CLI diagnostics log.

    Creates the log directory, opens a per-invocation log file,
    installs the redaction filter, wires up the ``omnigent`` and
    ``omnigent_ui_sdk`` logger hierarchies, and prunes old log
    files beyond :data:`MAX_LOG_FILES`.

    Call as early as possible in :func:`omnigent.cli.main` —
    before Click dispatch — so unhandled startup exceptions are
    captured.

    :param argv: ``sys.argv[1:]`` snapshot, logged as the first line
        for post-mortem context.
    :returns: A :class:`CliLogContext` with the log path and
        invocation id.
    """
    global _current

    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    invocation_id = f"{os.getpid():05d}-{_short_id()}"
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"cli-{timestamp}-{invocation_id}.log"
    log_path = log_dir / filename

    # Rotating handler — caps a single invocation at MAX_LOG_BYTES.
    handler = RotatingFileHandler(
        log_path,
        maxBytes=MAX_LOG_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    # Best-effort 0600 permissions on the log file.
    with contextlib.suppress(OSError):
        os.chmod(log_path, 0o600)

    handler.setFormatter(
        _RedactingFormatter(
            fmt="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    # Wire our two package hierarchies at INFO so their records reach
    # the file handler.
    for name in ("omnigent", "omnigent_ui_sdk"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False

    # Suppress noisy third-party loggers that are commonly present.
    for name in ("httpx", "httpcore", "asyncio", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)

    # NOTE: stderr is NOT redirected here — that's scoped to the TUI
    # lifetime via ``redirect_stderr_to_log`` /
    # ``restore_stderr``.  Non-TUI subcommands (``server``,
    # ``version``, one-shot ``run -p``) keep stderr on the
    # terminal so Click errors, tracebacks, and Ctrl-C output
    # remain visible.

    # Symlink latest-cli.log → this file.
    _update_latest_symlink(log_dir, log_path)

    # Prune old cli-*.log files beyond the cap.
    _prune_old_logs(log_dir)

    ctx = CliLogContext(path=log_path, invocation_id=invocation_id)
    _current = ctx

    # First line: the invocation context for post-mortem debugging.
    root = logging.getLogger("omnigent.cli_diagnostics")
    root.info("CLI start — argv=%s pid=%d", argv, os.getpid())

    return ctx


def current_cli_log_path() -> Path | None:
    """
    Return the active invocation's log path, or ``None`` if
    :func:`setup_cli_logging` has not been called yet.

    :returns: Absolute path to the current ``cli-*.log`` file, or
        ``None``.
    """
    return _current.path if _current is not None else None


def install_asyncio_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    """
    Install a custom exception handler on *loop* that logs unhandled
    asyncio exceptions (e.g. fire-and-forget tasks that raise) to
    the CLI diagnostics log instead of printing to stderr.

    :param loop: The running event loop (typically from
        ``asyncio.get_running_loop()`` inside the REPL's async
        context).
    """
    logger = logging.getLogger("omnigent.asyncio")

    def _handler(
        loop: asyncio.AbstractEventLoop,  # noqa: ARG001 — signature mandated by asyncio
        context: dict[str, object],
    ) -> None:
        """
        Log unhandled asyncio exceptions with full traceback.

        :param loop: The event loop that caught the exception.
        :param context: Exception context dict from asyncio.
        """
        exc = context.get("exception")
        msg = context.get("message", "Unhandled asyncio exception")
        if isinstance(exc, BaseException):
            logger.error("%s", msg, exc_info=exc)
        else:
            logger.error("asyncio: %s — context=%s", msg, context)

    loop.set_exception_handler(_handler)


def log_cli_error_hint(exc: BaseException) -> None:
    """
    Print a one-line pointer to the log file on stderr.

    Call this in the outermost exception handler (``main()``) so the
    user knows where to find the full traceback. No-op if
    :func:`setup_cli_logging` was never called.

    :param exc: The exception that triggered the hint.
    """
    path = current_cli_log_path()
    if path is None:
        return
    # Log the full traceback to the file.
    log_cli_exception(exc, prefix="Fatal CLI error")
    # One quiet line on the real terminal for the user.  sys.stderr
    # may have been redirected to the log file, so reach through to
    # the original.
    dest = getattr(sys.stderr, "_original_stderr", sys.stderr)
    print(f"Details logged to {path}", file=dest)


def print_setup_hint() -> None:
    """
    Print a one-line configuration-recovery hint on stderr.

    Used by the top-level :func:`omnigent.cli.main` exception
    handlers so any error the CLI surfaces ends with a pointer to
    the model-configuration command. The dominant root cause for CLI
    failures in the wild is a missing or misconfigured model
    credential — a hint that nudges the user toward
    ``omnigent setup`` keeps the recovery path obvious without
    requiring per-call classification of "is this auth?".

    Like :func:`log_cli_error_hint`, the line is written through
    to the original ``stderr`` so it survives any logging-driven
    stderr redirection that may have already happened during the
    failing turn.

    :returns: ``None``.
    """
    dest = getattr(sys.stderr, "_original_stderr", sys.stderr)
    print(
        "If this looks like an auth or configuration problem, run "
        "`omnigent setup` to configure a model credential.",
        file=dest,
    )


def log_cli_exception(exc: BaseException, *, prefix: str = "CLI error") -> None:
    """
    Write a CLI exception and traceback to the active diagnostics log.

    Use this for expected CLI exception boundaries that should be
    visible in ``cli-*.log`` without necessarily printing the
    user-facing "Details logged..." hint.

    :param exc: Exception to record, e.g. ``click.ClickException("bad")``.
    :param prefix: Log message prefix, e.g. ``"Fatal CLI error"``.
    :returns: ``None``.
    """
    if current_cli_log_path() is None:
        return
    logging.getLogger("omnigent.cli_diagnostics").error(
        "%s: %s",
        prefix,
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def redirect_stderr_to_log() -> None:
    """
    Replace ``sys.stderr`` with a file object appending to the active
    CLI log.

    Call when the TUI takes over the terminal.  Every write that
    previously went to the terminal's stderr now lands in the CLI
    diagnostic log.  The original stderr is saved so
    :func:`restore_stderr` can bring it back when the TUI exits.

    No-op if :func:`setup_cli_logging` has not been called yet or
    if the log file cannot be opened.
    """
    path = current_cli_log_path()
    if path is None:
        return
    try:
        log_fh = open(path, "a", encoding="utf-8")  # noqa: SIM115 — intentionally kept open for TUI lifetime
    except OSError as exc:
        log_cli_exception(exc, prefix="Failed to redirect stderr to CLI log")
        return
    original = cast(io.TextIOBase, sys.stderr)
    redirected = _RedactingStderr(log_fh, original)
    sys.stderr = redirected
    _retarget_stderr_logging_handlers(original, redirected)


def restore_stderr() -> None:
    """
    Undo :func:`redirect_stderr_to_log` — restore the real terminal
    stderr.

    Safe to call even if the redirect was never installed.
    """
    original = getattr(sys.stderr, "_original_stderr", None)
    if original is None:
        return
    redirected = sys.stderr
    sys.stderr = original
    _restore_logging_handlers()
    try:
        redirected.close()
    except OSError as exc:
        log_cli_exception(exc, prefix="Failed to close redirected stderr")


def _retarget_stderr_logging_handlers(
    original: io.TextIOBase,
    redirected: io.TextIOBase,
) -> None:
    """
    Point existing stderr-backed logging handlers at redirected stderr.

    ``logging.StreamHandler`` stores a concrete stream object at handler
    construction time.  Replacing ``sys.stderr`` later does not affect
    handlers that already captured the old stream, including root handlers
    installed by third-party SDKs.  During the TUI lifetime, retarget those
    handlers so their warning/error records land in the diagnostics log
    instead of painting over prompt-toolkit.

    :param original: Stderr stream that was current before redirection.
    :param redirected: Replacement stream writing to the CLI log.
    :returns: ``None``.
    """
    global _redirected_logging_streams

    if _redirected_logging_streams:
        return
    seen_handlers: set[int] = set()
    for logger in _existing_loggers():
        for handler in logger.handlers:
            if id(handler) in seen_handlers:
                continue
            seen_handlers.add(id(handler))
            if not isinstance(handler, logging.StreamHandler):
                continue
            stream = cast(io.TextIOBase, handler.stream)
            if stream is not original:
                continue
            handler.setStream(redirected)
            _redirected_logging_streams.append(
                _LoggingStreamSnapshot(handler=handler, stream=stream)
            )


def _restore_logging_handlers() -> None:
    """
    Restore logging handlers retargeted by stderr redirection.

    :returns: ``None``.
    """
    global _redirected_logging_streams

    snapshots = _redirected_logging_streams
    _redirected_logging_streams = []
    for snapshot in snapshots:
        snapshot.handler.setStream(snapshot.stream)


def _existing_loggers() -> list[logging.Logger]:
    """
    Return root plus all instantiated loggers in the logging registry.

    :returns: Existing loggers that may own stderr-backed handlers.
    """
    loggers = [logging.getLogger()]
    loggers.extend(
        logger
        for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )
    return loggers


def _short_id() -> str:
    """
    Generate a 6-character hex id for the invocation.

    :returns: A short random hex string, e.g. ``"a1b2c3"``.
    """
    return os.urandom(3).hex()


def _update_latest_symlink(log_dir: Path, log_path: Path) -> None:
    """
    Point ``latest-cli.log`` at *log_path*.

    Best-effort — silently ignored if the filesystem doesn't support
    symlinks (e.g. some Windows configurations).

    :param log_dir: Parent directory containing the symlink.
    :param log_path: Absolute path to the current log file.
    """
    link = log_dir / "latest-cli.log"
    try:
        link.unlink(missing_ok=True)
        link.symlink_to(log_path.name)
    except OSError:
        pass


def _safe_mtime(path: Path) -> float:
    """Return *path*'s mtime, or ``0.0`` if it has vanished.

    ``_prune_old_logs`` runs at the start of every ``omnigent run``, so two
    concurrent launches can glob the same log set then race to delete it. A
    plain ``p.stat()`` in the sort key would then hit a just-removed file and
    raise ``FileNotFoundError``, aborting the whole prune and crashing CLI
    startup. Treat a vanished file as oldest (it's already gone, so the
    suppressed ``unlink`` below is a no-op).
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _prune_old_logs(log_dir: Path) -> None:
    """
    Remove the oldest ``cli-*.log`` files when the count exceeds
    :data:`MAX_LOG_FILES`.

    Sorts by mtime (oldest first) and removes excess files. Backup
    files from rotation (``cli-*.log.1``) are included in the count.

    :param log_dir: Directory to prune.
    """
    pattern = "cli-*.log*"
    logs = sorted(log_dir.glob(pattern), key=_safe_mtime)
    # Keep the newest MAX_LOG_FILES; delete the rest.
    excess = logs[: max(0, len(logs) - MAX_LOG_FILES)]
    for old in excess:
        with contextlib.suppress(OSError):
            old.unlink()
