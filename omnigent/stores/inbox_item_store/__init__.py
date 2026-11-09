"""Inbox item store: suggestions agents post to their owner's Inbox."""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from typing import Any

from omnigent.entities import InboxItem


class DuplicateInboxItemError(Exception):
    """Raised when an insert collides with an existing item's duplicate key."""


@dataclasses.dataclass(frozen=True)
class NewInboxItem:
    """Fields for an item to insert; the store assigns ``id`` and timestamps.

    :param user_id: Owner of the item.
    :param source_session_id: Session whose agent posted it.
    :param agent_id: Agent of that session.
    :param prompt_version: Content hash of the agent bundle, or ``None``.
    :param kind: ``"followup"``, ``"info"``, or ``"reminder"``.
    :param title: One-line summary.
    :param body_md: Markdown body.
    :param links: Evidence links; the first is the primary link.
    :param suggested_prompt: Prompt for the Start session action, or ``None``.
    :param primary_link: Canonical form of the first link, or ``None``.
    :param topic: Normalized topic slug.
    :param dedupe_key: Versioned duplicate-detection key.
    :param expires_at: Unix epoch seconds after which the key may be reused.
    """

    user_id: str
    source_session_id: str
    agent_id: str
    prompt_version: str | None
    kind: str
    title: str
    body_md: str
    links: list[dict[str, Any]]
    suggested_prompt: str | None
    primary_link: str | None
    topic: str
    dedupe_key: str
    expires_at: int | None = None


class InboxItemStore(ABC):
    """Abstract base for Inbox item persistence. Every method is scoped to one owner."""

    def __init__(self, storage_location: str) -> None:
        """Initialize the store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def get(self, item_id: str, user_id: str) -> InboxItem | None:
        """Return the owner's item, or ``None`` if missing or owned by someone else.

        :param item_id: Hex UUID of the item.
        :param user_id: The owner the item must belong to.
        """

    @abstractmethod
    def get_by_dedupe_key(self, user_id: str, dedupe_key: str) -> InboxItem | None:
        """Return the owner's item holding *dedupe_key*, or ``None``.

        :param user_id: The owner to search.
        :param dedupe_key: A versioned key, e.g. ``"v1:3f2a..."``.
        """

    @abstractmethod
    def create(self, item: NewInboxItem, *, retire_item_id: str | None = None) -> InboxItem:
        """Insert *item* as an unread item.

        :param item: The item's fields.
        :param retire_item_id: An expired item holding the same key; its key is
            rewritten to ``"<key>#<its id>"`` in the same transaction so the
            unique index accepts the new item.
        :returns: The created item.
        :raises DuplicateInboxItemError: If another item already holds the key.
        """

    @abstractmethod
    def list_for_user(
        self,
        user_id: str,
        *,
        states: frozenset[str] | None = None,
        created_after: int | None = None,
        after: tuple[int, str] | None = None,
        limit: int = 50,
    ) -> tuple[list[InboxItem], tuple[int, str] | None]:
        """Return the owner's items, newest first.

        :param user_id: The owner.
        :param states: Only items in these states; ``None`` for all.
        :param created_after: Only items posted at or after this epoch second.
        :param after: Cursor ``(created_at, id)`` from a previous page.
        :param limit: Page size.
        :returns: The page and the cursor for the next page, or ``None`` at the end.
        """

    @abstractmethod
    def count_unread(self, user_id: str) -> int:
        """Return how many of the owner's items are unread.

        :param user_id: The owner.
        """

    @abstractmethod
    def count_for_session(self, user_id: str, source_session_id: str) -> int:
        """Return how many items the owner received from one session.

        :param user_id: The owner.
        :param source_session_id: The posting session.
        """

    @abstractmethod
    def update(
        self,
        item_id: str,
        user_id: str,
        *,
        state: str | None = None,
        feedback: str | None = None,
        feedback_text: str | None = None,
        now: int,
    ) -> InboxItem | None:
        """Apply the owner's response to an item.

        Moving out of ``unread`` stamps ``read_at`` once. Any change of state
        to ``done`` or ``dismissed``, or any feedback, stamps ``acted_at``.

        :param item_id: Hex UUID of the item.
        :param user_id: The owner the item must belong to.
        :param state: New state, or ``None`` to keep it.
        :param feedback: New feedback, or ``None`` to keep it.
        :param feedback_text: New comment, or ``None`` to keep it.
        :param now: Current epoch seconds.
        :returns: The updated item, or ``None`` if missing or owned by someone else.
        """
