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

# The acting user's vault secrets for this turn, already resolved + mapped to
# env-var names by the server (see
# :func:`omnigent.runtime.credentials.resolve_user_credential_env`) and pushed
# on the turn dispatch. ``None`` = nothing to inject. Kept alongside the actor
# id because it shares the same turn lifetime and threading constraints.
_ACTING_CREDENTIAL_ENV: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "omnigent_acting_credential_env", default=None
)


def get_acting_user() -> str | None:
    """Return the acting user for the current turn, or ``None``.

    :returns: The authenticated id of the collaborator who triggered the
        current turn (e.g. ``"bob@example.com"``), or ``None`` when there is
        no distinct actor / the identity wasn't threaded.
    """
    return _ACTING_USER.get()


def get_acting_credential_env() -> dict[str, str]:
    """Return the acting user's credential env overlay for this turn.

    :returns: ``{ENV_VAR: value}`` to overlay onto that user's tool
        subprocesses, or ``{}`` when there is nothing to inject.
    """
    return _ACTING_CREDENTIAL_ENV.get() or {}


@contextmanager
def acting_user_scope(
    user_id: str | None, credential_env: dict[str, str] | None = None
) -> Iterator[None]:
    """Bind the acting user (and their credential env) for a ``with`` block.

    Used by the runner at turn entry so every tool dispatched while processing
    that turn observes the actor and their credentials. Previous values are
    restored on exit, so nested/sequential turns don't leak into one another.

    :param user_id: The acting user to bind, or ``None`` to explicitly clear.
    :param credential_env: The actor's resolved env overlay, or ``None``.
    """
    user_token = _ACTING_USER.set(user_id)
    cred_token = _ACTING_CREDENTIAL_ENV.set(credential_env)
    try:
        yield
    finally:
        _ACTING_CREDENTIAL_ENV.reset(cred_token)
        _ACTING_USER.reset(user_token)
