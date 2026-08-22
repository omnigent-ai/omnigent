"""Idempotency binding for automation-safe session-shell creation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from omnigent.db.db_models import current_workspace_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.schemas import SessionCreateRequest
from omnigent.stores.conversation_store import (
    SESSION_CREATE_KEY_HASH_LABEL,
    SESSION_CREATE_OWNER_HASH_LABEL,
    SESSION_CREATE_REQUEST_HASH_LABEL,
)

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
_MAX_IDEMPOTENCY_KEY_BYTES = 255
_CREATE_BINDING_VERSION = "session-create-v1"


@dataclass(frozen=True)
class SessionCreateIdempotency:
    """Server-owned binding for one idempotent session-create request."""

    conversation_id: str
    labels: dict[str, str]

    def matches(self, labels: dict[str, str]) -> bool:
        """Return whether persisted labels exactly match this binding."""
        return all(labels.get(key) == value for key, value in self.labels.items())


def build_session_create_idempotency(
    raw_key: str | None,
    body: SessionCreateRequest,
    user_id: str | None,
) -> SessionCreateIdempotency | None:
    """Validate and bind an optional create idempotency key.

    The first safe slice intentionally supports only an empty, top-level,
    external session shell. Host provisioning, worktree creation, routing,
    label mutation, and initial turn dispatch all have side effects outside
    the conversation-row transaction; claiming idempotency for those shapes
    before they carry their own durable operation state would be unsafe.

    :param raw_key: Raw ``Idempotency-Key`` header, or ``None`` when omitted.
    :param body: Validated session-create body.
    :param user_id: Authenticated principal, or ``None`` in single-user mode.
    :returns: A deterministic binding, or ``None`` when the header is absent.
    :raises OmnigentError: If the key or request shape is unsupported.
    """
    if raw_key is None:
        return None
    key_bytes = raw_key.encode("utf-8")
    if (
        not key_bytes
        or len(key_bytes) > _MAX_IDEMPOTENCY_KEY_BYTES
        or any(byte < 0x21 or byte > 0x7E for byte in key_bytes)
    ):
        raise OmnigentError(
            "Idempotency-Key must be 1-255 visible ASCII characters without whitespace",
            code=ErrorCode.INVALID_INPUT,
        )
    _require_safe_shell_shape(body)

    principal = user_id if user_id is not None else RESERVED_USER_LOCAL
    workspace = str(current_workspace_id())
    request_json = json.dumps(
        body.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    key_hash = hashlib.sha256(key_bytes).hexdigest()
    owner_hash = hashlib.sha256(principal.encode("utf-8")).hexdigest()
    request_hash = hashlib.sha256(request_json).hexdigest()
    identity = b"\0".join(
        (
            _CREATE_BINDING_VERSION.encode("ascii"),
            workspace.encode("ascii"),
            principal.encode("utf-8"),
            key_bytes,
        )
    )
    conversation_id = hashlib.sha256(identity).digest()[:16].hex()
    return SessionCreateIdempotency(
        conversation_id=conversation_id,
        labels={
            SESSION_CREATE_KEY_HASH_LABEL: key_hash,
            SESSION_CREATE_OWNER_HASH_LABEL: owner_hash,
            SESSION_CREATE_REQUEST_HASH_LABEL: request_hash,
        },
    )


def _require_safe_shell_shape(body: SessionCreateRequest) -> None:
    """Reject create side effects that do not yet have durable replay state."""
    unsupported = (
        bool(body.initial_items)
        or bool(body.labels)
        or body.parent_session_id is not None
        or body.sub_agent_name is not None
        or body.host_type != "external"
        or body.host_id is not None
        or body.sandbox_provider is not None
        or body.workspace is not None
        or body.git is not None
        or body.terminal_launch_args is not None
        or body.model_override is not None
        or body.reasoning_effort is not None
        or body.cost_control_mode_override is not None
        or body.subagent_routing_override is not None
        or body.harness_override is not None
        or body.smart_routing_message is not None
    )
    if unsupported:
        raise OmnigentError(
            "Idempotency-Key currently supports only an empty top-level external "
            "session shell (agent_id and optional title)",
            code=ErrorCode.INVALID_INPUT,
        )
