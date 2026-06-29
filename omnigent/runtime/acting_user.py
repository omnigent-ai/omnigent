"""Per-turn acting-user identity for the runner (#5).

The *acting user* is the authenticated collaborator who triggered the current
turn — the sender of the message being processed — as opposed to the session
*owner* (whose host runs the runner). In a shared session those differ, and
tool execution needs the actor's identity to act under their own credentials
(resolve their per-user vault secret, attribute their side effects).

The runner's turn entry sets this from the inbound message's ``created_by``;
deep tool-dispatch code reads it. A :class:`~contextvars.ContextVar` carries it
across ``await`` boundaries and ``asyncio.to_thread`` hops (the context is
copied into the worker thread) without threading the identity through every
dispatch signature in the runner's large request path. It is process-global to
the runner and turn-scoped via :func:`acting_user_scope`, so concurrent turns —
each running in its own asyncio task with its own copied context — never see
each other's actor.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

# Default ``None`` = "no distinct actor" (single-user mode, owner-initiated
# turn, or an unthreaded path). Readers must treat ``None`` as "fall back to
# ambient/owner behavior", never as an error.
_ACTING_USER: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "omnigent_acting_user", default=None
)


def get_acting_user() -> str | None:
    """Return the acting user for the current turn, or ``None``.

    :returns: The authenticated id of the collaborator who triggered the
        current turn (e.g. ``"bob@example.com"``), or ``None`` when there is
        no distinct actor / the identity wasn't threaded.
    """
    return _ACTING_USER.get()


@contextmanager
def acting_user_scope(user_id: str | None) -> Iterator[None]:
    """Bind the acting user for the duration of a ``with`` block.

    Used by the runner at turn entry so every tool dispatched while processing
    that turn observes the actor. The previous value is restored on exit, so
    nested/sequential turns don't leak identity into one another.

    :param user_id: The acting user to bind, or ``None`` to explicitly clear.
    """
    token = _ACTING_USER.set(user_id)
    try:
        yield
    finally:
        _ACTING_USER.reset(token)
