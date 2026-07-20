"""Shared process logging setup for Omnigent entrypoints."""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from omnigent._platform import IS_POSIX

DATA_DIR_ENV_VAR = "OMNIGENT_DATA_DIR"
LOG_LEVEL_ENV_VAR = "OMNIGENT_LOG_LEVEL"
LOG_TO_STDERR_ENV_VAR = "OMNIGENT_LOG_TO_STDERR"
LOG_FORCE_COLOR_ENV_VAR = "OMNIGENT_LOG_FORCE_COLOR"
PROCESS_LOG_FILE_ENV_VAR = "OMNIGENT_PROCESS_LOG_FILE"
LOG_TTY_FD_ENV_VAR = "OMNIGENT_LOG_TTY_FD"

DEFAULT_LOG_SOURCE_WIDTH = 32
DEFAULT_LOG_FUNC_WIDTH = 18
DEFAULT_LOG_PREFIX_FORMAT = (
    "%(levelname)s %(asctime)s.%(msecs)03d %(source_name)s %(func_name)s | "
)
DEFAULT_LOG_FORMAT = f"{DEFAULT_LOG_PREFIX_FORMAT}%(message)s"
DEFAULT_LOG_DATEFMT = "%m-%d %H:%M:%S"
_LEVEL_WIDTH = 5
_ANSI_RESET = "\x1b[0m"
_SOURCE_COLOR = "\x1b[34m"
_FUNCTION_COLOR = "\x1b[35m"
_LEVEL_NAMES = {
    logging.WARNING: "WARN",
    logging.CRITICAL: "CRIT",
}
_LEVEL_COLORS = {
    logging.DEBUG: "\x1b[36m",
    logging.INFO: "\x1b[32m",
    logging.WARNING: "\x1b[33m",
    logging.ERROR: "\x1b[31m",
    logging.CRITICAL: "\x1b[91m",
}


def format_log_level_name(levelno: int, levelname: str, *, use_colors: bool) -> str:
    """Return the aligned display level for one log record."""
    display = _LEVEL_NAMES.get(levelno, levelname)
    display = display[:_LEVEL_WIDTH].ljust(_LEVEL_WIDTH)
    color = _LEVEL_COLORS.get(levelno) if use_colors else None
    return f"{color}{display}{_ANSI_RESET}" if color is not None else display


def _compact_field(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 3:
        return value[-width:]
    return "..." + value[-(width - 3) :]


def _color_field(value: str, color: str, *, use_colors: bool) -> str:
    return f"{color}{value}{_ANSI_RESET}" if use_colors else value


def short_logger_name(name: str) -> str:
    """Return a compact, fixed-column logger source name."""
    for prefix in ("omnigent.", "omnigent_ui_sdk."):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    return _compact_field(name, DEFAULT_LOG_SOURCE_WIDTH)


def short_function_name(name: str | None) -> str:
    """Return a compact function name for log display."""
    return _compact_field(name or "-", DEFAULT_LOG_FUNC_WIDTH)


def format_log_source_name(name: str, *, use_colors: bool) -> str:
    """Return the padded, optionally colored logger source column."""
    display = short_logger_name(name).ljust(DEFAULT_LOG_SOURCE_WIDTH)
    return _color_field(display, _SOURCE_COLOR, use_colors=use_colors)


def format_log_function_name(name: str | None, *, use_colors: bool) -> str:
    """Return the padded, optionally colored function column."""
    display = short_function_name(name).ljust(DEFAULT_LOG_FUNC_WIDTH)
    return _color_field(display, _FUNCTION_COLOR, use_colors=use_colors)


@contextmanager
def log_record_display_fields(
    record: logging.LogRecord,
    *,
    use_colors: bool,
    format_level: bool = True,
) -> Iterator[None]:
    """Temporarily add Omnigent display columns to a log record."""
    original_levelname = record.levelname
    display_fields = ("source_name", "func_name")
    originals = {
        field: (field in record.__dict__, record.__dict__.get(field)) for field in display_fields
    }
    if format_level:
        record.levelname = format_log_level_name(
            record.levelno,
            original_levelname,
            use_colors=use_colors,
        )
    record.source_name = format_log_source_name(record.name, use_colors=use_colors)
    record.func_name = format_log_function_name(record.funcName, use_colors=use_colors)
    try:
        yield
    finally:
        record.levelname = original_levelname
        for field, (had_field, value) in originals.items():
            if had_field:
                setattr(record, field, value)
            else:
                record.__dict__.pop(field, None)


class TerminalLogFormatter(logging.Formatter):
    """Formatter for mirrored terminal logs with optional colored levels."""

    def __init__(
        self,
        fmt: str = DEFAULT_LOG_FORMAT,
        datefmt: str = DEFAULT_LOG_DATEFMT,
        *,
        use_colors: bool,
    ) -> None:
        super().__init__(fmt, datefmt=datefmt)
        self._use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        with log_record_display_fields(record, use_colors=self._use_colors):
            return super().format(record)


def data_dir() -> Path:
    """Return the runtime data directory used for DBs, artifacts, and logs."""
    value = os.environ.get(DATA_DIR_ENV_VAR)
    return Path(value).expanduser() if value else Path.home() / ".omnigent"


def logs_root() -> Path:
    """Return ``<data-dir>/logs``."""
    return data_dir() / "logs"


def process_log_dir(destination: str, *, root: str | Path | None = None) -> Path:
    """Return the directory for one process-log destination."""
    base = Path(root).expanduser() if root is not None else logs_root()
    return base / destination


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def create_process_log_path(
    destination: str,
    *,
    root: str | Path | None = None,
    prefix: str | None = None,
) -> Path:
    """Create and return a unique timestamped log path."""
    log_dir = process_log_dir(destination, root=root)
    log_dir.mkdir(parents=True, exist_ok=True)
    base = prefix or f"{destination}-"
    for counter in range(100):
        suffix = "" if counter == 0 else f"-{counter}"
        candidate = log_dir / f"{base}{_timestamp()}{suffix}.log"
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise FileExistsError(f"could not allocate a {destination!r} log file in {log_dir}")


def open_process_log_file(
    destination: str,
    *,
    root: str | Path | None = None,
    prefix: str | None = None,
) -> tuple[Path, BinaryIO]:
    """Create and open a process log file for binary stdout/stderr capture."""
    path = create_process_log_path(destination, root=root, prefix=prefix)
    return path, open(path, "ab", buffering=0)


def env_truthy(value: str | None) -> bool:
    """Return whether an environment-style boolean value is truthy."""
    return value is not None and value.strip().lower() not in {"", "0", "false", "no", "off"}


def effective_log_level(default: str = "INFO") -> int:
    """Resolve the effective numeric logging level from ``OMNIGENT_LOG_LEVEL``."""
    name = os.environ.get(LOG_LEVEL_ENV_VAR, default).upper()
    value = getattr(logging, name, None)
    return value if isinstance(value, int) else logging.INFO


def should_log_to_stderr() -> bool:
    """Return whether process logs should also mirror to an interactive stderr."""
    return env_truthy(os.environ.get(LOG_TO_STDERR_ENV_VAR))


def _process_log_file_from_env() -> Path | None:
    value = os.environ.get(PROCESS_LOG_FILE_ENV_VAR)
    return Path(value).expanduser() if value else None


def _terminal_stream() -> object | None:
    fd_value = os.environ.get(LOG_TTY_FD_ENV_VAR)
    if fd_value and IS_POSIX:
        try:
            fd = int(fd_value)
            dup = os.dup(fd)
            return os.fdopen(dup, "w", buffering=1, encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return None
    if sys.stderr.isatty():
        return sys.stderr
    return None


def terminal_supports_color() -> bool:
    """Return whether the requested terminal mirror can render ANSI colors."""
    # Omnigent-owned mirrors (omnidev panes) may force ANSI; otherwise NO_COLOR wins.
    if env_truthy(os.environ.get(LOG_FORCE_COLOR_ENV_VAR)):
        return True
    if os.environ.get("NO_COLOR") is not None:
        return False
    if env_truthy(os.environ.get("FORCE_COLOR")) or env_truthy(os.environ.get("CLICOLOR_FORCE")):
        return True
    fd_value = os.environ.get(LOG_TTY_FD_ENV_VAR)
    if fd_value and IS_POSIX:
        try:
            return os.isatty(int(fd_value))
        except (OSError, ValueError):
            return False
    return sys.stderr.isatty()


def terminal_stream_handler() -> logging.Handler:
    """Return a stream handler for the requested terminal mirror."""
    stream = _terminal_stream()
    if stream is None:
        return logging.NullHandler()
    handler = logging.StreamHandler(stream)
    handler._omnigent_process_log_stderr = True
    return handler


def terminal_log_formatter() -> logging.Formatter:
    """Return the formatter used by mirrored terminal process logs."""
    return TerminalLogFormatter(use_colors=terminal_supports_color())


def configure_process_logging(
    destination: str,
    *,
    log_path: str | Path | None = None,
    level: int | None = None,
    log_to_stderr: bool | None = None,
    logger_names: Sequence[str] = ("omnigent",),
    root: bool = True,
    force: bool = False,
) -> Path:
    """Configure Python logging for one process destination.

    The returned file always receives logs. Stderr receives logs only when
    requested and an interactive terminal stream is available.
    """
    resolved_level = effective_log_level() if level is None else level
    path = Path(log_path).expanduser() if log_path is not None else _process_log_file_from_env()
    if path is None:
        path = create_process_log_path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)

    formatter = TerminalLogFormatter(use_colors=False)
    handlers: list[logging.Handler] = []

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setLevel(resolved_level)
    file_handler.setFormatter(formatter)
    file_handler._omnigent_process_log_path = str(path)
    handlers.append(file_handler)

    mirror = should_log_to_stderr() if log_to_stderr is None else log_to_stderr
    if mirror:
        stream_handler = terminal_stream_handler()
        if not isinstance(stream_handler, logging.NullHandler):
            stream_handler.setLevel(resolved_level)
            stream_handler.setFormatter(terminal_log_formatter())
            handlers.append(stream_handler)

    if root:
        root_logger = logging.getLogger()
        root_logger.setLevel(resolved_level)
        if force:
            logging.basicConfig(
                level=resolved_level,
                format=DEFAULT_LOG_FORMAT,
                datefmt=DEFAULT_LOG_DATEFMT,
                handlers=handlers,
                force=True,
            )
            # ``basicConfig`` uses the supplied handlers as-is.
        else:
            for handler in handlers:
                _add_handler_once(root_logger, handler)

    for name in logger_names:
        logger = logging.getLogger(name)
        logger.setLevel(resolved_level)
        if not logger.propagate or not root:
            for handler in handlers:
                _add_handler_once(logger, handler)

    logging.captureWarnings(True)
    return path


def _add_handler_once(logger: logging.Logger, handler: logging.Handler) -> None:
    path = getattr(handler, "_omnigent_process_log_path", None)
    is_stderr = getattr(handler, "_omnigent_process_log_stderr", False)
    for existing in logger.handlers:
        if path is not None and getattr(existing, "_omnigent_process_log_path", None) == path:
            handler.close()
            return
        if is_stderr and getattr(existing, "_omnigent_process_log_stderr", False):
            handler.close()
            return
    logger.addHandler(handler)


@contextmanager
def child_logging_popen_kwargs(env: dict[str, str]) -> Iterator[dict[str, object]]:
    """Prepare inherited terminal-fd kwargs for a child process.

    Mutates *env* only when ``--log-to-stderr`` requested a mirror and the
    current process has an interactive stderr. On POSIX the returned kwargs
    include ``pass_fds`` so a detached child can still write logs to that TTY.
    """
    owned_fd: int | None = None
    if env_truthy(env.get(LOG_TO_STDERR_ENV_VAR)) and IS_POSIX:
        fd_text = env.get(LOG_TTY_FD_ENV_VAR)
        if fd_text:
            with contextlib.suppress(OSError, ValueError):
                owned_fd = os.dup(int(fd_text))
                os.set_inheritable(owned_fd, True)
                env[LOG_TTY_FD_ENV_VAR] = str(owned_fd)
        else:
            with contextlib.suppress(OSError):
                if sys.stderr.isatty():
                    owned_fd = os.dup(sys.stderr.fileno())
                    os.set_inheritable(owned_fd, True)
                    env[LOG_TTY_FD_ENV_VAR] = str(owned_fd)
    try:
        fd_values: list[int] = []
        fd_text = env.get(LOG_TTY_FD_ENV_VAR)
        if fd_text and IS_POSIX:
            with contextlib.suppress(ValueError):
                fd_values.append(int(fd_text))
        yield {"pass_fds": tuple(fd_values)} if fd_values else {}
    finally:
        if owned_fd is not None:
            with contextlib.suppress(OSError):
                os.close(owned_fd)
