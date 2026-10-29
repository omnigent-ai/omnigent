"""Provider execution locators carried separately from native tool arguments.

These identifiers do not establish a requester. The runner must resolve them
against accepted inputs and retained execution state before authorizing work.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class CodexExecutionIdentity:
    """The provider session, thread and turn that issued a tool call."""

    session_id: str
    thread_id: str
    turn_id: str

    def to_wire(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
        }

    @classmethod
    def from_wire(cls, value: object) -> CodexExecutionIdentity | None:
        """Decode a complete locator; missing or malformed identities stay unknown."""
        if not isinstance(value, dict):
            return None
        identifiers: list[str] = []
        for key in ("session_id", "thread_id", "turn_id"):
            identifier = value.get(key)
            if not isinstance(identifier, str) or not 0 < len(identifier) <= 256:
                return None
            identifiers.append(identifier)
        return cls(*identifiers)

    @classmethod
    def from_mcp_meta(cls, meta: object) -> CodexExecutionIdentity | None:
        """Read Codex's retained turn metadata, never the bridge's active turn."""
        if not isinstance(meta, dict):
            return None
        identity = cls.from_wire(meta.get("x-codex-turn-metadata"))
        if identity is None:
            return None
        for key, expected in (
            ("threadId", identity.thread_id),
            ("sessionId", identity.session_id),
        ):
            if key in meta and meta[key] != expected:
                return None
        return identity


current_codex_execution: ContextVar[CodexExecutionIdentity | None] = ContextVar(
    "current_codex_execution", default=None
)
