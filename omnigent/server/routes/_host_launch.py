"""Ownership-checked resolution for host runner launches.

Two routes spawn a runner subprocess on a user's host machine and
bind it to a session: ``POST /v1/sessions`` (inline host launch) and
``POST /v1/hosts/{host_id}/runners``. A runner executes arbitrary
tools (shell, file I/O) on the host as that host's user, so a launch
must be authorized against BOTH the host and the session:

- the caller must own the target host (else they could run code on
  another user's machine — cross-user RCE), and
- the caller must own the target session (else they could bind their
  runner to another user's session, or another user's host to their
  session — cross-user hijack / data theft).

Centralizing the checks here keeps the two call sites from drifting
(the original bug was each site enforcing a different subset).
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.permissions import check_session_access
from omnigent.stores import ConversationStore
from omnigent.stores.host_store import Host, HostStore, host_is_live
from omnigent.stores.permission_store import PermissionStore


@dataclass
class HostLaunchTarget:
    """A host + session pair the caller is authorized to launch on.

    :param host: The persistent host record (owned by the caller).
    :param conn: The live host WebSocket connection on this replica,
        used to send the launch frame.
    :param conv: The session/conversation the runner will bind to.
    """

    host: Host
    conn: HostConnection
    conv: Conversation


def resolve_host_owner(
    *,
    user_id: str | None,
    host_id: str,
    host_store: HostStore,
    is_admin: bool = False,
) -> Host:
    """
    Authorize that the caller owns a known host.

    Every route that reaches a host on the caller's behalf must pass
    this first so the owner check can't drift between them: the runner
    launch (via :func:`resolve_host_launch`) AND the session-create
    workspace probe, which sends a ``host.stat`` to the host. The
    original bug had that probe contacting another user's host before
    any ownership check. When ``user_id`` is ``None`` (auth disabled)
    the check is skipped, consistent with single-user/local behavior.

    :param user_id: Authenticated caller, e.g. ``"alice@example.com"``,
        or ``None`` when auth is disabled.
    :param host_id: Target host id, e.g. ``"host_a1b2c3d4..."``.
    :param host_store: Persistent host registrations.
    :returns: The host record owned by the caller.
    :raises HTTPException: 404 if the host is unknown; 403 if it is
        owned by a different user.
    """
    host = host_store.get_host(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="host not found")
    if user_id is not None and not is_admin and host.user_id != user_id:
        raise HTTPException(status_code=403, detail="not your host")
    return host


def resolve_host_launch(
    *,
    user_id: str | None,
    host_id: str,
    session_id: str,
    host_store: HostStore,
    host_registry: HostRegistry,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
) -> HostLaunchTarget:
    """
    Resolve and authorize a host runner launch.

    Verifies the host exists, is owned by the caller, and is online,
    and that the caller owns the target session, before any runner is
    spawned. When ``user_id`` is ``None`` (auth disabled) the host-owner
    check is skipped; when ``permission_store`` is ``None`` (auth
    disabled) the session-owner check is skipped — both consistent with
    the single-user/local deployment behavior elsewhere.

    :param user_id: Authenticated caller, e.g. ``"alice@example.com"``,
        or ``None`` when auth is disabled.
    :param host_id: Target host id, e.g. ``"host_a1b2c3d4..."``.
    :param session_id: Session to bind the runner to, e.g.
        ``"conv_abc123"``.
    :param host_store: Persistent host registrations.
    :param host_registry: In-memory live host connections (this replica).
    :param conversation_store: Conversation lookups (also used by the
        session-access check for sub-agent parent delegation).
    :param permission_store: Session permission store, or ``None`` to
        skip the session-owner check (auth disabled).
    :returns: A :class:`HostLaunchTarget` with the validated host,
        connection, and conversation.
    :raises HTTPException: 404 if the host or session is missing (or the
        session is not owned by the caller — 404, not 403, so other
        users' sessions aren't enumerable); 403 if the host is owned by
        a different user.
    :raises OmnigentError: 409 (CONFLICT) if the host is genuinely offline,
        or 400 (WRONG_REPLICA) if it is live but its tunnel is on another
        replica (a wrong-replica landing — the caller re-addresses keyless).
    """
    host = resolve_host_owner(
        user_id=user_id,
        host_id=host_id,
        host_store=host_store,
        is_admin=(
            permission_store is not None
            and user_id is not None
            and permission_store.is_admin(user_id)
        ),
    )

    conn = host_registry.get(host_id)
    if conn is None:
        # The host's tunnel isn't on this replica. If the store shows it still
        # live (online + fresh heartbeat), it's up on another replica — a
        # wrong-replica landing — so surface WRONG_REPLICA (400, distinct code)
        # and let the client re-address keyless. Otherwise it's genuinely
        # offline → CONFLICT (409). Mirrors RunnerRouter._runner_absent_code
        # and hosts._host_absent_error.
        if host_is_live(host):
            raise OmnigentError("host is on another replica", code=ErrorCode.WRONG_REPLICA)
        raise OmnigentError("host is offline", code=ErrorCode.CONFLICT)

    conv = conversation_store.get_conversation(session_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="session not found")

    # A runner executes tools as the session's driver, so only the
    # session owner may bind one. A non-owner has no owner-level grant
    # and is rejected. 404 (not 403) avoids leaking the existence of
    # other users' sessions.
    if permission_store is not None and not check_session_access(
        user_id,
        session_id,
        LEVEL_OWNER,
        permission_store,
        conversation_store,
    ):
        raise HTTPException(status_code=404, detail="session not found")

    return HostLaunchTarget(host=host, conn=conn, conv=conv)
