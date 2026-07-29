"""
Data models for runtime deferred actions and audit trails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

DeferredActionStatus = Literal[
    "PENDING",
    "APPROVED",
    "REJECTED",
    "EXPIRED",
    "EXECUTED",
    "FAILED",
    "HASH_DRIFT",
]


@dataclass(frozen=True)
class DeferredManifest:
    """
    Captured manifest representing a deferred tool call effect.
    """

    tool: str
    arguments: dict[str, Any]
    base_hash: str
    session_id: str
    target: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert manifest to a standard JSON serializable dict."""
        return {
            "tool": self.tool,
            "arguments": self.arguments,
            "base_hash": self.base_hash,
            "session_id": self.session_id,
            "target": self.target,
        }


@dataclass
class DeferredAuditEvent:
    """
    Append-only record of a state transition or action taken on a deferred action.
    """

    id: str
    action_id: str
    event_type: Literal[
        "CREATE",
        "APPROVE",
        "REJECT",
        "EXECUTE",
        "EXPIRE",
        "FAIL",
        "HASH_DRIFT",
    ]
    timestamp: str
    actor: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeferredAction:
    """
    Runtime record representing a frozen deferred tool execution gate.
    """

    id: str
    manifest: DeferredManifest
    manifest_hash: str
    status: DeferredActionStatus
    created_at: str
    expires_at: str
    session_id: str
    task_id: str | None = None
    deciding_policy: str | None = None
    reason: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert action object to a dictionary payload."""
        return {
            "id": self.id,
            "manifest": self.manifest.to_dict(),
            "manifest_hash": self.manifest_hash,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "deciding_policy": self.deciding_policy,
            "reason": self.reason,
            "error_message": self.error_message,
        }
