"""Server policy for granting another user or group access to a session.

The session share route applies these checks. Automations that share their runs
reuse them when they save and when they run. The functions take ``app.state`` and
plain values, not a request, so background paths can call them too.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_READ, SharingMode, workspace_sharing_blocked
from omnigent.server.sharing_settings import PublicSharingMaxLevel

# Levels a read-only sharing mode allows. Downstream deployments may patch this to add levels.
READ_ONLY_MODE_LEVELS: frozenset[int] = frozenset({LEVEL_READ})

# Levels an automation may grant; Edit would turn off its runs' orchestration. The
# permission store enforces it; widening it needs no migration.
SCHEDULED_TASK_GRANT_LEVELS: frozenset[int] = frozenset({LEVEL_READ})


def _sharing_mode(state: Any) -> SharingMode:
    """Return the live sharing mode from ``app.state``; ``ON`` when hand-built without it."""
    return getattr(state, "sharing_mode", lambda: SharingMode.ON)()


def require_sharing_mode_allows_grant(
    state: Any,
    *,
    level: int,
    load_workspace: Callable[[], str | None],
) -> None:
    """Raise unless the sharing mode allows a new grant at *level*.

    Only new grants are gated; revoking, listing and owner grants never call this.
    The mode is read once. Checks run in this order: ``OFF`` refuses everything;
    ``RESTRICTED_READ_ONLY`` refuses a home or root workspace, even for read; read-only
    modes refuse any level outside :data:`READ_ONLY_MODE_LEVELS`.

    An unknown workspace (``None``) is allowed under ``RESTRICTED_READ_ONLY``, matching
    the session share route. Callers that must fail closed without a workspace, such as
    automation runs, check that themselves.

    :param state: ``app.state``, which carries the live policy callables.
    :param level: The level to grant, e.g. ``LEVEL_READ``.
    :param load_workspace: Returns the session's working directory, or ``None`` when
        unknown. Called only under ``RESTRICTED_READ_ONLY``, so a store read happens
        only when the policy needs it.
    :raises OmnigentError: ``FORBIDDEN`` naming the refused policy.
    """
    mode = _sharing_mode(state)
    if mode == SharingMode.OFF:
        raise OmnigentError(
            "Sharing has been disabled for this Omnigent server.",
            code=ErrorCode.FORBIDDEN,
        )
    # A home or root directory is too broad to expose, even read-only.
    if mode == SharingMode.RESTRICTED_READ_ONLY and workspace_sharing_blocked(load_workspace()):
        raise OmnigentError(
            "This session's working directory (a home or root directory) "
            "cannot be shared on this Omnigent server.",
            code=ErrorCode.FORBIDDEN,
        )
    read_only = mode in (SharingMode.READ_ONLY, SharingMode.RESTRICTED_READ_ONLY)
    if read_only and level not in READ_ONLY_MODE_LEVELS:
        raise OmnigentError(
            "Sharing is limited to read-only access on this Omnigent server.",
            code=ErrorCode.FORBIDDEN,
        )


def require_public_grant_allowed(state: Any, *, level: int) -> None:
    """Raise unless a ``__public__`` grant at *level* is allowed.

    Checks the public-access kill switch first, then the public ceiling. The kill
    switch is independent of the sharing mode, so user-to-user sharing still works
    when it is off. Call :func:`require_sharing_mode_allows_grant` first.

    :param state: ``app.state``, which carries ``public_sharing`` and
        ``public_sharing_max_level``; a hand-built state defaults to enabled, read.
    :param level: The level to grant, e.g. ``LEVEL_READ``.
    :raises OmnigentError: ``FORBIDDEN`` when the kill switch is off, or
        ``INVALID_INPUT`` when *level* is above the ceiling.
    """
    if not getattr(state, "public_sharing", lambda: True)():
        raise OmnigentError(
            "Public access has been disabled for this Omnigent server.",
            code=ErrorCode.FORBIDDEN,
        )
    ceiling = getattr(state, "public_sharing_max_level", lambda: PublicSharingMaxLevel.READ)()
    if level > ceiling.level:
        raise OmnigentError(
            f"Public access is limited to {ceiling.value} (level {ceiling.level})",
            code=ErrorCode.INVALID_INPUT,
        )
