"""User credential entity (#5) — metadata for a secret in the per-user vault.

One row per (user, name): a collaborator's own secret (git token, aws profile,
databricks token, …) stored encrypted server-side so it can be resolved for
*their* tool actions in a shared session. The entity is metadata only — the
encrypted value never rides on it; it's fetched separately and decrypted by the
vault, so listings can't leak secrets.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UserCredential:
    """Metadata for one stored secret (the value lives encrypted in the store).

    :param id: Opaque primary key, e.g. ``"cred_a1b2c3..."``.
    :param user_id: The owning user (the only one who can read/use it).
    :param name: Logical name the user/agent references, e.g. ``"github"``,
        ``"aws"``, ``"databricks:prod"`` (unique per user).
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None``.
    """

    id: str
    user_id: str
    name: str
    created_at: int
    updated_at: int | None = None
