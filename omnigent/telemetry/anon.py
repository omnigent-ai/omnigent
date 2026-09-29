"""Anonymised user identity for telemetry events.

The gateway never receives a raw user id.  Events carry
``anon_user_id`` — the first 16 hex chars of
``sha256("<installation_id>:<user_id>")`` — so a single user's events can
be correlated within an installation without identifying them.
"""

from __future__ import annotations

import hashlib


def anon_user_id(user_id: str | None, installation_id: str | None) -> str | None:
    """Hash *user_id* into the telemetry-safe anonymous identifier.

    :param user_id: The authenticated user id, e.g. ``"alice@example.com"``.
        ``None`` in single-user mode, where there is nobody to attribute to.
    :param installation_id: Server-side installation ID, used as the salt so
        the same user hashes differently across installations.
    :returns: First 16 hex chars of the salted SHA-256 digest, or ``None``
        when *user_id* is ``None``.
    """
    if user_id is None:
        return None
    salt = f"{installation_id}:{user_id}" if installation_id else user_id
    return hashlib.sha256(salt.encode()).hexdigest()[:16]


def current_anon_user_id() -> str | None:
    """Hash the ambient request user, for emitters with no ``user_id`` in scope.

    Reads the request-scoped user the server middleware binds, which a task
    spawned while handling a request (e.g. the runner relay) inherits.  Prefer
    passing an in-scope ``user_id`` to :func:`anon_user_id`; this is for
    callsites too deep to thread one through.

    :returns: The ambient user's anonymous identifier, or ``None`` when no
        request user is bound (single-user mode, or a task started outside a
        request).
    """
    from omnigent.debug_logging import current_user_id
    from omnigent.telemetry.installation_id import get_installation_id

    return anon_user_id(current_user_id(), get_installation_id())
