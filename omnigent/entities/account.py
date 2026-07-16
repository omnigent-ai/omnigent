"""Account / user identity entities for the ``accounts`` auth provider.

Plain dataclasses returned from :class:`PermissionStore` user and
token methods. The store layer never returns ORM rows directly so
the runtime stays uncoupled from SQLAlchemy.

The password hash is intentionally NOT included on :class:`Account`
— it's an internal store concept that should never leave the
trust boundary. Routes that need to verify a password fetch the
hash via the dedicated :meth:`PermissionStore.get_password_hash`
method, which is the only place it's surfaced.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class Account:
    """A user row, minus the password hash.

    Returned by user listing / lookup endpoints — safe to serialize
    over the wire. Mirrors the columns of ``SqlUser`` that are
    appropriate to expose to admins (and to the user themselves in
    ``/auth/me``).

    :param id: User identifier — email in header/OIDC modes, chosen
        username in accounts mode.
    :param is_admin: Admin flag (bypasses all permission checks).
    :param created_at: Unix epoch seconds when the row was created.
        ``None`` for legacy rows (e.g. the ``"local"`` backfill).
    :param last_login_at: Unix epoch seconds of the most recent
        ``/auth/login``. ``None`` for users who have never logged
        in (header/OIDC users that just exist by virtue of having
        an upstream identity).
    :param has_password: Whether the row has a ``password_hash``
        set. Useful so the UI can render "External login" vs
        "Password login" badges without exposing the hash itself.
    """

    id: str
    is_admin: bool
    created_at: int | None
    last_login_at: int | None
    has_password: bool


@dataclasses.dataclass(frozen=True)
class AccountToken:
    """An invite or magic-login token row.

    The ``id`` is the secret bearer value — it's only ever returned
    to (a) the admin who minted an invite, exactly once, embedded
    in the copyable URL; and (b) the CLI that minted its own magic
    token. Tokens are NEVER listed in bulk through any route.

    :param id: Opaque random token string. Secret; treat as a
        bearer credential.
    :param kind: ``"invite"`` (anyone may redeem, creates a new
        user) or ``"magic"`` (signs in as :attr:`user_id`).
    :param user_id: For ``magic``, the user the token authenticates
        as. For ``invite``, ``None``.
    :param created_by: For ``invite``, the admin that minted the
        token. For ``magic``, ``None`` (the user mints their own).
    :param created_at: Unix epoch seconds when minted.
    :param expires_at: Unix epoch seconds when the token stops
        being redeemable.
    :param invited_is_admin: For invite tokens, whether the
        resulting user is granted admin rights.
    """

    id: str
    kind: str
    user_id: str | None
    created_by: str | None
    created_at: int
    expires_at: int
    invited_is_admin: bool
