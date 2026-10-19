"""Structured JSON log lines, selected with ``OMNIGENT_LOG_FORMAT=json``.

Each record becomes one JSON object on one line. The core keys follow Go
``log/slog``'s JSON handler (``time``, ``level``, ``msg``, ``source``), a
shape most log pipelines index without a custom parser::

    {"time": "2026-10-13T17:05:01.123456Z", "level": "INFO",
     "source": "server/app.py:42", "msg": "...", "logger": "omnigent.server.app",
     "trace_id": "4bf9...", "request_id": "a1b2...", "session_id": "conv_..."}

Request context (``trace_id``, ``request_id``, ``session_id``, ``runner_id``,
``user_id``) is added when bound, and any ``extra=`` fields ride as further
top-level keys. Each serialized value is redacted and capped at
:data:`MAX_ATTR_BYTES` UTF-8 bytes, including JSON quoting and escaping.
Non-finite numbers are emitted as JSON ``null``, including in nested values.
"""

from __future__ import annotations

import json
import logging
import math
import os
import traceback
from datetime import datetime, timezone
from typing import Any

from opentelemetry import trace

from omnigent.debug_logging import (
    current_request_id,
    current_runner_id,
    current_session_id,
    current_user_id,
)
from omnigent.process_logging import redact_log_text

LOG_FORMAT_ENV_VAR = "OMNIGENT_LOG_FORMAT"

# Per-value bound, including JSON quoting and escaping.
MAX_ATTR_BYTES = 16 * 1024
_TRUNCATED_SUFFIX = "...[truncated]"

# slog's level names: there is no CRITICAL, and WARNING is spelled WARN.
_LEVEL_NAMES = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARN",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "ERROR",
}

# Attributes every LogRecord carries; anything else on a record came from
# ``extra=`` (or a filter) and is emitted as a field.
_RESERVED_RECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
) | {
    "message",
    "asctime",
    # Display columns omnigent.process_logging's filter sets for text formats.
    "source_name",
    "func_name",
    # uvicorn's colorized copy of the message.
    "color_message",
}


def json_logging_requested() -> bool:
    """Return whether ``OMNIGENT_LOG_FORMAT`` selects JSON output."""
    return os.environ.get(LOG_FORMAT_ENV_VAR, "").strip().lower() == "json"


def _bounded(value: str) -> str:
    """Redact *value* and cap its serialized form at :data:`MAX_ATTR_BYTES`."""
    value = redact_log_text(value)
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) <= MAX_ATTR_BYTES:
        return value
    low, high = 0, min(len(value), MAX_ATTR_BYTES)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = value[:middle] + _TRUNCATED_SUFFIX
        if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) <= MAX_ATTR_BYTES:
            low = middle
        else:
            high = middle - 1
    return value[:low] + _TRUNCATED_SUFFIX


def _field_value(value: object) -> object:
    """Return a JSON-safe, bounded form of one extra field."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _bounded(value)
    try:
        normalized = json.loads(
            json.dumps(value, default=str, ensure_ascii=False), parse_constant=lambda _: None
        )
        encoded = json.dumps(normalized, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return _bounded(repr(value))
    # Nested structures stay structured (the pipeline flattens them) unless
    # they are oversized or carried a secret; then they degrade to text.
    if len(encoded.encode("utf-8")) > MAX_ATTR_BYTES or redact_log_text(encoded) != encoded:
        return _bounded(encoded)
    return normalized


def _source(record: logging.LogRecord) -> str:
    """Return ``parent_dir/file.py:line``, like the Go handler's ``source``."""
    parent = os.path.basename(os.path.dirname(record.pathname))
    filename = os.path.basename(record.pathname)
    return f"{parent}/{filename}:{record.lineno}" if parent else f"{filename}:{record.lineno}"


def _rfc3339(created: float) -> str:
    """Return *created* (epoch seconds) as RFC 3339 UTC with microseconds."""
    stamp = datetime.fromtimestamp(created, tz=timezone.utc)
    return stamp.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _trace_id() -> str | None:
    """Return the active trace id as 32 hex characters, or ``None``."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return trace.format_trace_id(span_context.trace_id)


class JsonFormatter(logging.Formatter):
    """Format records as single-line, slog-keyed JSON."""

    def format(self, record: logging.LogRecord) -> str:
        """
        Render *record* as one JSON line.

        :param record: Record to format.
        :returns: JSON object text with no trailing newline.
        """
        entry: dict[str, Any] = {
            "time": _rfc3339(record.created),
            "level": _LEVEL_NAMES.get(record.levelno, record.levelname),
            "source": _source(record),
            "msg": self.message(record),
            "logger": record.name,
        }
        ambient = {
            "trace_id": _trace_id(),
            "request_id": current_request_id(),
            "session_id": current_session_id(),
            "runner_id": current_runner_id(),
            "user_id": current_user_id(),
        }
        for key, value in ambient.items():
            if value:
                entry[key] = value
        for key, value in self.extra_fields(record).items():
            # An explicit extra wins over the ambient value of the same name.
            entry[key] = value
        if record.exc_info:
            exc = record.exc_info[1]
            entry["error"] = f"{type(exc).__name__}: {exc}" if exc else ""
            entry["stack"] = "".join(traceback.format_exception(*record.exc_info))
        elif record.exc_text:
            entry["stack"] = record.exc_text
        if record.stack_info:
            entry["stack_info"] = record.stack_info
        return json.dumps(
            {key: _field_value(value) for key, value in entry.items()},
            ensure_ascii=False,
            allow_nan=False,
        )

    def message(self, record: logging.LogRecord) -> str:
        """Return the ``msg`` value for *record*: its formatted message."""
        return record.getMessage()

    def extra_fields(self, record: logging.LogRecord) -> dict[str, object]:
        """
        Return the fields *record* carries beyond the standard LogRecord ones.

        :param record: Record to inspect.
        :returns: Mapping of extra field name to value.
        """
        return {
            key: value
            for key, value in vars(record).items()
            if key not in _RESERVED_RECORD_ATTRS and not key.startswith("_")
        }
