"""Inbox item entity: one suggestion an agent posted to its owner's Inbox."""

from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass
class InboxItem:
    """A suggestion an agent posted to a user's Inbox.

    :param id: Hex UUID of the item, e.g. ``"3f2a..."``.
    :param user_id: Owner of the item, e.g. ``"alice@example.com"``, or the
        reserved ``"local"`` user in single-user mode.
    :param source_session_id: Session whose agent posted the item,
        e.g. ``"conv_abc123"``.
    :param agent_id: Agent of that session.
    :param prompt_version: Content hash of the agent bundle the session ran,
        or ``None`` when it could not be resolved.
    :param kind: ``"followup"``, ``"info"``, or ``"reminder"``.
    :param title: One-line summary shown on the collapsed card.
    :param body_md: Markdown body.
    :param links: Evidence links, each ``{"type": "session" | "pr" | "url",
        "ref": ..., "label": ...}``; the first is the primary link.
    :param actions: Agent-defined actions, each ``{"label": ..., "prompt": ...}``.
        Choosing one opens a new session prefilled with its prompt.
    :param media: Typed rich-content blocks drawn on the card, such as
        ``{"type": "chart", "chart": {...}}``; see :mod:`omnigent.server.inbox_media`.
    :param primary_link: Canonical form of the first link, or ``None``.
    :param topic: The agent's short slug for what the item is about,
        e.g. ``"rampup-flag"``.
    :param dedupe_key: Versioned duplicate-detection key; see
        :mod:`omnigent.server.inbox_dedupe`.
    :param state: ``"unread"``, ``"read"``, ``"done"``, or ``"dismissed"``.
    :param resolution: How the owner closed the item: ``"none"`` while open,
        ``"action_taken"``, or a Dismiss reason (``"already_done"``,
        ``"useful"``, ``"not_relevant"``, ``"incorrect"``, ``"not_now"``).
    :param feedback_text: The user's comment, or ``None``.
    :param created_at: Unix epoch seconds when the item was posted.
    :param resolved_at: Unix epoch seconds when the owner last resolved it
        (took an action or dismissed it), or ``None``.
    :param expires_at: Unix epoch seconds after which a new post with the
        same key creates a fresh item, or ``None`` for never.
    :param resolution_action: Index into ``actions`` of the action taken, or ``None``.
    """

    id: str
    user_id: str
    source_session_id: str
    agent_id: str
    prompt_version: str | None
    kind: str
    title: str
    body_md: str
    links: list[dict[str, Any]]
    actions: list[dict[str, str]]
    media: list[dict[str, Any]]
    primary_link: str | None
    topic: str
    dedupe_key: str
    state: str
    resolution: str
    feedback_text: str | None
    created_at: int
    resolved_at: int | None = None
    expires_at: int | None = None
    resolution_action: int | None = None
