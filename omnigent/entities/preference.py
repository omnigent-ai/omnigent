"""Per-user preference entity."""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class UserPreference:
    """A single per-user sidebar preference row.

    A generic ``key`` → JSON-``value`` pair scoped to one user, so pins,
    collapsed sections, and expanded projects are three rows and a future
    preference needs no migration. The value is stored as an opaque JSON
    string; the route layer parses it back to structured data.

    :param user_id: The owner, e.g. ``"alice@example.com"``.
    :param key: The preference name, e.g. ``"pinned_conversation_ids"``.
    :param value: The preference value as a JSON string, e.g. ``'["a","b"]'``.
    :param updated_at: Unix epoch seconds of the most recent write, so stale
        rows are prunable. ``0`` on rows written before the column existed.
    """

    user_id: str
    key: str
    value: str
    updated_at: int = 0
