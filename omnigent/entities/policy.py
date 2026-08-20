"""Policy entity — persisted in the ``policies`` table.

Policies are either session-scoped (``session_id`` set) or
server-wide defaults (``session_id`` is ``None``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Policy:
    """
    A policy persisted in the ``policies`` table.

    Session-scoped policies are created via
    ``POST /v1/sessions/{session_id}/policies``. Server-wide
    default policies are created via ``POST /v1/policies`` and
    have ``session_id=None``.

    Each policy references a handler — a Python callable
    (``type="python"``) or an HTTPS endpoint (``type="url"``).

    :param id: Opaque primary key, e.g. ``"pol_a1b2c3..."``.
    :param name: Human-readable name. Unique within its session
        (composite UNIQUE on ``(session_id, name)``), or globally
        unique for default policies,
        e.g. ``"block_non_feature_branch_push"``.
    :param session_id: The session this policy is scoped to,
        e.g. ``"conv_abc123"``. ``None`` for server-wide
        default policies.
    :param scope: ``"default"`` for server-wide policies;
        ``"session"`` for session-scoped policies.
    :param created_at: Unix epoch seconds at row creation.
    :param type: Handler discriminator: ``"python"`` or
        ``"url"``.
    :param handler: Dotted import path (``type="python"``)
        or HTTPS URL (``type="url"``).
    :param factory_params: Dict of kwargs passed to the handler
        when it is a factory function, e.g.
        ``{"limit": 10}``. ``None`` when the handler is a direct
        callable or for ``type="url"``.
    :param enabled: Whether the engine consults this policy.
        Defaults to ``True``.
    :param updated_at: Unix epoch seconds of the last write, or
        ``None`` if the row has never been updated.
    :param created_by: User ID of the admin who created this
        policy, e.g. ``"alice@example.com"``. ``None`` in
        single-user mode or for session-scoped policies.
    """

    id: str
    name: str
    session_id: str | None
    scope: str
    created_at: int
    type: str
    handler: str
    factory_params: dict[str, Any] | None = None
    enabled: bool = True
    updated_at: int | None = None
    created_by: str | None = None
