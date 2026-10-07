"""Explicit shutdown evidence shared by the CLI, host tunnel, and server."""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SHUTDOWN_NOTIFY_TIMEOUT_S = 2.0
SHUTDOWN_INTENT_TTL_MS = 120_000
SHUTDOWN_CLOCK_SKEW_MS = 30_000


def timestamp_ms() -> int:
    """Return the observation time in Unix milliseconds."""
    return time.time_ns() // 1_000_000


class ShutdownIntent(BaseModel):
    """An observed command or signal; ownership does not identify a signal's sender."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    shutdown_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=64)
    reason: Literal[
        "user_stopped_host", "user_stopped_session", "host_interrupted_sigint", "unknown"
    ]
    action: Literal["host_stop", "host_disable", "stop_session", "archive", "signal"]
    initiator: Literal["local_cli", "authenticated_user", "unknown"]
    initiator_user_id: str | None = Field(default=None, max_length=512)
    requested_at_ms: int = Field(default_factory=timestamp_ms, ge=0)
    signal_name: Literal["SIGINT", "SIGTERM", "SIGHUP"] | None = None
    force: bool = False
    daemon_only: bool = False
    host_id: str | None = Field(default=None, max_length=128)
    host_process_id: str | None = Field(default=None, max_length=64)
    host_connection_id: str | None = Field(default=None, max_length=64)
    host_pid: int | None = Field(default=None, gt=0)

    @property
    def requested(self) -> bool:
        """Whether this observation proves a requested shutdown."""
        return (
            (
                self.reason == "host_interrupted_sigint"
                and self.action == "signal"
                and self.signal_name == "SIGINT"
                and self.initiator == "unknown"
            )
            or (
                self.reason == "user_stopped_host"
                and self.action in {"host_stop", "host_disable"}
                and self.initiator in {"local_cli", "authenticated_user"}
            )
            or (
                self.reason == "user_stopped_session"
                and self.action in {"stop_session", "archive"}
                and self.initiator in {"local_cli", "authenticated_user"}
            )
        )

    @property
    def category(self) -> str:
        """Return the readable shutdown category used in diagnostics."""
        if self.reason == "host_interrupted_sigint":
            return "Host stopped by interrupt (SIGINT)"
        if self.reason == "user_stopped_host":
            return "Host stopped by command"
        if self.reason == "user_stopped_session":
            return "Session stopped by user"
        return "Host shutdown cause unknown"

    def log_attrs(self) -> dict[str, Any]:
        """Return stable, flat debug-event fields for downstream per-error joins."""
        return {
            **self.model_dump(),
            "shutdown_category": self.category,
            "requested": self.requested,
        }


def write_shutdown_request(path: Path, intent: ShutdownIntent) -> None:
    """Publish an explicit command atomically, without changing the daemon record."""
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            stream.write(intent.model_dump_json())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_shutdown_request(path: Path, process_id: str, pid: int) -> ShutdownIntent | None:
    """Consume only a recent explicit command addressed to this exact process."""
    try:
        intent = ShutdownIntent.model_validate(json.loads(path.read_text()))
        written_at_ms = path.stat().st_mtime_ns // 1_000_000
    except (OSError, ValueError):
        return None
    if (
        intent.host_process_id != process_id
        or intent.host_pid != pid
        or intent.reason != "user_stopped_host"
        or not intent.requested
        or not 0 <= timestamp_ms() - written_at_ms <= 15_000
    ):
        return None
    with suppress(OSError):
        path.unlink(missing_ok=True)
    return intent
