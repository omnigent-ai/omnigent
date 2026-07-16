"""Session permission entity."""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class SessionPermission:
    """A single permission grant on a session.

    :param user_id: The grantee, e.g. ``"alice@example.com"``
        or ``"__public__"`` for public access.
    :param conversation_id: The session this grant applies to,
        e.g. ``"conv_abc123"``.
    :param level: Numeric permission level: ``1`` = read,
        ``2`` = edit, ``3`` = manage. Comparison is ``>=``.
    """

    user_id: str
    conversation_id: str
    level: int


@dataclasses.dataclass
class HostPermission:
    """A single permission grant on a host.

    Shares a host the grantee does not own with them. The host owner
    and admins need no grant — their access is resolved by
    :func:`omnigent.server.host_permissions.check_host_access`.

    :param user_id: The grantee, e.g. ``"alice@example.com"``.
    :param host_id: The host this grant applies to, e.g.
        ``"host_a1b2c3d4..."``.
    :param level: Numeric permission level: ``1`` = view, ``2`` = use,
        ``3`` = manage. Comparison is ``>=``.
    :param created_at: Unix epoch seconds the grant was created.
    :param updated_at: Unix epoch seconds the grant was last changed.
    :param created_by: User ID that created the grant, or ``None``.
    """

    user_id: str
    host_id: str
    level: int
    created_at: int
    updated_at: int
    created_by: str | None = None


@dataclasses.dataclass(frozen=True)
class ResolvedAccess:
    """The raw permission inputs for one ``(user, conversation)`` pair.

    Fetched in a single store round-trip so a caller that needs both the
    access decision *and* the displayed level can derive both without
    re-querying. Pure data — the resolution policy (admin bypass, public
    fallback, sub-agent delegation) lives in
    :mod:`omnigent.server.permissions`, not here.

    :param is_admin: Whether the user has the global admin flag set.
    :param user_grant_level: The user's own grant level on the
        conversation (``1`` = read, ``2`` = edit, ``3`` = manage,
        ``4`` = owner), or ``None`` if they have no direct grant.
    :param public_grant_level: The ``"__public__"`` sentinel grant level
        on the conversation (same ``1``–``4`` scale), or ``None`` if the
        session is not public.
    """

    is_admin: bool
    user_grant_level: int | None
    public_grant_level: int | None
