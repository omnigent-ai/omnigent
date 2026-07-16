"""Host access checks for route handlers.

Provides :func:`check_host_access`, the single resolver every host
read/use path routes through. Mirrors
:func:`omnigent.server.permissions.check_session_access` but for hosts:

1. auth disabled (``user_id is None``) → allow (single-user/local)
2. admin → allow (reuses the global ``users.is_admin`` flag)
3. host owner → allow (no grant row needed)
4. otherwise → delegate to the host permission grant check

See ``specs/admin-host-management-spec.md``.
"""

from __future__ import annotations

from omnigent.stores.host_permission_store import HostPermissionStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store import PermissionStore

# Host permission levels. Host-local on purpose — they share the
# session model's integers (so the DB CHECK and any shared comparison
# stay aligned) but carry host-meaningful names: "use" is not "edit".
HOST_LEVEL_VIEW = 1
HOST_LEVEL_USE = 2
HOST_LEVEL_MANAGE = 3
# Effective-only: the owner/admin display level. Never stored as a grant
# (the CHECK constraint forbids 4); returned by get_host_permission_level
# so the UI can label owner/admin access.
HOST_LEVEL_OWNER = 4


def check_host_access(
    user_id: str | None,
    host_id: str,
    required_level: int,
    host_permission_store: HostPermissionStore,
    host_store: HostStore,
    permission_store: PermissionStore | None = None,
) -> bool:
    """Check whether *user_id* may act on a host at *required_level*.

    Resolution order:

    1. ``user_id is None`` (auth disabled) → allow, consistent with the
       single-user/local behavior the host routes already use.
    2. Admin → allow (when *permission_store* is supplied; the admin
       flag is global, stored on ``users.is_admin``).
    3. Host owner → allow at every level (owner needs no grant row).
    4. Otherwise → the user's host grant must be ``>= required_level``.

    A missing host resolves to no access for non-owners (the grant
    lookup misses); callers that must distinguish 404 from 403 look the
    host up themselves first.

    :param user_id: The authenticated caller, e.g. ``"alice@example.com"``,
        or ``None`` when auth is disabled.
    :param host_id: The host to check, e.g. ``"host_a1b2c3d4..."``.
    :param required_level: Minimum numeric level needed
        (1=view, 2=use, 3=manage).
    :param host_permission_store: Store for host grant lookups.
    :param host_store: Store for host owner lookup.
    :param permission_store: Session permission store, consulted only
        for the global admin flag. ``None`` skips the admin bypass.
    :returns: ``True`` if access is allowed, ``False`` otherwise.
    """
    if user_id is None:
        return True

    if permission_store is not None and permission_store.is_admin(user_id):
        return True

    host = host_store.get_host(host_id)
    if host is not None and host.owner == user_id:
        return True

    return host_permission_store.check_access(user_id, host_id, required_level)


def get_host_permission_level(
    user_id: str | None,
    host_id: str,
    host_permission_store: HostPermissionStore,
    host_store: HostStore,
    permission_store: PermissionStore | None = None,  # noqa: ARG001 — kept for call-site parity with check_host_access
) -> int | None:
    """Return the caller's effective host permission level for UI display.

    This is a *display* value paired with ``owned_by_current_user`` in
    host responses, so it must stay coherent with ownership: only an
    actual owner (or the auth-disabled local caller) resolves to
    :data:`HOST_LEVEL_OWNER`. An admin is an access *override*, not an
    ownership claim — an admin viewing a host they do not own shows their
    real grant level (``view``/``use``/``manage``) or ``None`` if they
    have no grant, never a spurious ``owner``. (Admin access still works:
    :func:`check_host_access` applies the admin bypass for the access
    decision; this function only governs the label.)

    :param user_id: The authenticated caller, or ``None``.
    :param host_id: The host to check.
    :param host_permission_store: Store for host grant lookups.
    :param host_store: Store for host owner lookup.
    :param permission_store: Unused; kept for signature parity with
        :func:`check_host_access` so call sites pass the same args.
    :returns: Numeric level (1/2/3/4), or ``None`` when no grant and not
        the owner.
    """
    if user_id is None:
        return HOST_LEVEL_OWNER

    host = host_store.get_host(host_id)
    if host is not None and host.owner == user_id:
        return HOST_LEVEL_OWNER

    return host_permission_store.get_permission_level(user_id, host_id)
