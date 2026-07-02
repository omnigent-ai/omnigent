"""Project entity — a first-class, shareable collection of conversations."""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class Project:
    """A first-class project.

    Conversations reference their project via ``conversation.project_id``;
    the project's ACL (``project_permissions``) is inherited by every
    conversation filed under it, so sharing a project grants access to all
    of its chats — including ones added after the share.

    :param id: Unique project identifier, e.g. ``"proj_e4f5a6b7..."``.
    :param name: Human-readable project name, e.g. ``"Q3 launch"``.
    :param created_by: The owner's user id, or ``None`` if that account was
        deleted.
    :param created_at: Unix epoch seconds when the project was created.
    :param updated_at: Unix epoch seconds of the last mutation (e.g. rename).
    """

    id: str
    name: str
    created_by: str | None
    created_at: int
    updated_at: int
