"""User summary entity for admin listings."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UserRecord:
    """A user row as surfaced to admins.

    A lightweight projection of the ``users`` table — enough for the
    admin user list without leaking the ORM row or password hash.

    :param user_id: The user identifier — an email in header/OIDC
        modes, a username in accounts mode, ``"local"`` in
        single-user mode, e.g. ``"alice@example.com"``.
    :param is_admin: Whether the user holds the admin flag.
    """

    user_id: str
    is_admin: bool
