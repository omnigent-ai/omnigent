"""Narrow lifecycle API for operator-enabled, trusted Python extensions."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

from omnigent.util.json_types import JsonValue


@dataclass(frozen=True)
class SessionStatusNotification:
    """A best-effort status edge, without transcript, credentials, or errors."""

    session_id: str
    status: str
    response_id: str | None = None
    type: Literal["session.status"] = field(default="session.status", init=False)


@dataclass(frozen=True)
class ExtensionContext:
    """Identity and operator configuration; never grants raw application stores."""

    extension_id: str
    version: str
    config: Mapping[str, JsonValue]
    logger: logging.Logger


@dataclass(frozen=True)
class ExtensionHooks:
    """Async observers returned by a server entrypoint's ``activate(context)``."""

    on_session_status: Callable[[SessionStatusNotification], Awaitable[None]] | None = None
    shutdown: Callable[[], Awaitable[None]] | None = None


_status_sink: ContextVar[Callable[[SessionStatusNotification], None] | None] = ContextVar(
    "extension_status_sink", default=None
)


def notify_session_status(session_id: str, status: str, response_id: str | None) -> None:
    """Enqueue metadata for the current app's observers without delaying a turn."""
    sink = _status_sink.get()
    if sink is not None:
        sink(SessionStatusNotification(session_id, status, response_id))
