"""MessageReaction entity: one user's thumbs on one assistant response."""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class MessageReaction:
    """A thumbs-up or thumbs-down a user gave an assistant response.

    :param conversation_id: The session the response belongs to.
    :param response_id: The assistant turn reacted to, e.g. ``"resp_abc"``.
    :param rating: ``"up"`` or ``"down"``.
    :param note: Optional free text, e.g. what was wrong; ``None`` when absent.
    :param harness: Harness that produced the response, snapshotted at reaction
        time (a session's model can change later); ``None`` when unknown.
    :param model: Model that produced the response, snapshotted likewise.
    :param created_by: Reacting user's email, or ``None`` in single-user mode.
    :param created_at: Unix timestamp (seconds) the reaction was first given.
    :param updated_at: Unix timestamp (microseconds) of the last change.
    """

    conversation_id: str
    response_id: str
    rating: str  # "up" | "down"
    note: str | None
    harness: str | None
    model: str | None
    created_by: str | None
    created_at: int
    updated_at: int
