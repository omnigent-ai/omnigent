"""Authenticated attachment identity and conservative terminal input evidence."""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import QueryParams
from starlette.types import Scope

TERMINAL_WRITER_QUERY = "_omnigent_terminal_writer"
_TERMINAL_WRITER_SCOPE = "omnigent.terminal_writer"


class TerminalWriter(BaseModel):
    """Identity authenticated at attachment, never inferred from a runner token."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    user_id: str | None = Field(default=None, min_length=1)
    account_generation: str | None = Field(default=None, min_length=1)

    def same_account(self, other: TerminalWriter) -> bool:
        return self.user_id is not None and (
            self.user_id,
            self.account_generation,
        ) == (other.user_id, other.account_generation)


def bind_terminal_writer(scope: Scope, writer: TerminalWriter) -> None:
    """Bind identity supplied by a trusted ingress to this attachment only."""
    scope[_TERMINAL_WRITER_SCOPE] = writer


def bind_relay_terminal_writer(scope: Scope) -> None:
    """Decode server-owned query metadata inside authenticated tunnel dispatch.

    Never call this on a browser or local network handshake. Those routes must
    authenticate their own actor or bind an explicit unknown writer instead.
    """
    values = QueryParams(scope.get("query_string", b"")).getlist(TERMINAL_WRITER_QUERY)
    writer = TerminalWriter()
    if len(values) == 1:
        with contextlib.suppress(ValidationError):
            writer = TerminalWriter.model_validate_json(values[0])
    bind_terminal_writer(scope, writer)


def terminal_writer_from_scope(scope: Scope) -> TerminalWriter:
    """Read typed trusted state; browser JSON, headers and queries are not proof."""
    writer = scope.get(_TERMINAL_WRITER_SCOPE)
    return writer if isinstance(writer, TerminalWriter) else TerminalWriter()


@dataclass(frozen=True)
class TerminalWriterSnapshot:
    incarnation_id: str
    revision: int
    writer: TerminalWriter


class TerminalWriterEvidence:
    """Accumulate writers for one process incarnation without parsing keystrokes.

    The launcher must create this before input is possible, share it across all
    attachments, and replace it on process restart. Complete observation requires
    every writer path, including local tmux and programmatic input, to be covered.
    """

    def __init__(self, incarnation_id: str, *, complete_observation: bool = False) -> None:
        if not incarnation_id:
            raise ValueError("Terminal process incarnation is required")
        self._incarnation_id = incarnation_id
        self._complete_observation = complete_observation
        self._revision = 0
        self._writer: TerminalWriter | None = None
        self._lock = threading.Lock()

    @property
    def incarnation_id(self) -> str:
        """Opaque process identity captured by the launch-scoped input callback."""
        return self._incarnation_id

    def record(self, writer: TerminalWriter) -> None:
        """Record before forwarding bytes; unknown or mixed input stays unknown."""
        with self._lock:
            self._revision += 1
            if self._writer is None:
                self._writer = writer
            elif not self._writer.same_account(writer):
                self._writer = TerminalWriter()

    def snapshot(self, incarnation_id: str) -> TerminalWriterSnapshot | None:
        """Capture evidence; callers must recheck revision after registration awaits."""
        with self._lock:
            if incarnation_id != self._incarnation_id:
                return None
            return TerminalWriterSnapshot(
                incarnation_id=self._incarnation_id,
                revision=self._revision,
                writer=self._writer
                if self._complete_observation and self._writer is not None
                else TerminalWriter(),
            )
