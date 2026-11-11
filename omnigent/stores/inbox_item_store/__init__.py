"""Inbox item store: suggestions agents post to their owner's Inbox."""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from typing import Any

from omnigent.entities import InboxItem

# Items one session may post.
INBOX_ITEMS_PER_SESSION = 10


class DuplicateInboxItemError(Exception):
    """Raised when an insert collides with an existing item's duplicate key."""


class InboxSessionCapError(Exception):
    """Raised when the posting session already holds its maximum number of items."""


class InboxItemTooLargeError(Exception):
    """Raised when a text field exceeds the stored-size limit after compression."""


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
    :param actions: Agent-defined actions, each ``{"label": ..., "prompt": ...}``.
    :param media: Validated rich-content blocks, possibly empty.
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
    actions: list[dict[str, str]]
    media: list[dict[str, Any]]
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
        """Insert *item* as an unread item, in one transaction with its admission checks.

        Each session may hold :data:`INBOX_ITEMS_PER_SESSION` items. The count
        and the insert share one write transaction, which serializes them on
        SQLite; on databases with concurrent writers, posts racing for the last
        place can each commit, so the cap is a bound, not an exact limit.

        :param item: The item's fields.
        :param retire_item_id: An expired item holding the same key; its key is
            rewritten to ``"<key>#<its id>"`` in the same transaction so the
            unique index accepts the new item.
        :returns: The created item.
        :raises DuplicateInboxItemError: If another item already holds the key.
        :raises InboxSessionCapError: If the session already holds its maximum.
        :raises InboxItemTooLargeError: If a text field is too large to store.
        """

    @abstractmethod
    def list_for_user(
        self,
        user_id: str,
        *,
        statuses: frozenset[str] | None = None,
        created_after: int | None = None,
        after: tuple[int, str] | None = None,
        limit: int = 50,
    ) -> tuple[list[InboxItem], tuple[int, str] | None]:
        """Return the owner's items, newest first.

        :param user_id: The owner.
        :param statuses: Only items in these statuses; ``None`` for all.
        :param created_after: Only items posted at or after this epoch second.
        :param after: Cursor ``(created_at, id)`` from a previous page.
        :param limit: Page size.
        :returns: The page and the cursor for the next page, or ``None`` at the end.
        """

    @abstractmethod
    def count_unread(self, user_id: str, *, limit: int) -> int:
        """Return how many of the owner's items are unread, counting at most *limit*.

        :param user_id: The owner.
        :param limit: Largest count to return, which bounds the rows scanned.
        """

    @abstractmethod
    def update(
        self,
        item_id: str,
        user_id: str,
        *,
        status: str | None = None,
        resolution: str | None = None,
        resolution_action: int | None = None,
        feedback_text: str | None = None,
        now: int,
    ) -> InboxItem | None:
        """Apply the owner's response to an item.

        Setting a resolution other than ``"none"`` stamps ``resolved_at``.
        Without a resolution, *status* changes only an open (unread or read)
        item, so a late read mark cannot reopen a resolved one.

        :param item_id: Hex UUID of the item.
        :param user_id: The owner the item must belong to.
        :param status: New status, or ``None`` to keep it.
        :param resolution: New resolution, or ``None`` to keep it.
        :param resolution_action: Index of the action taken, stored with the resolution.
        :param feedback_text: New comment, or ``None`` to keep it.
        :param now: Current epoch seconds.
        :returns: The updated item, or ``None`` if missing or owned by someone else.
        :raises InboxItemTooLargeError: If *feedback_text* is too large to store.
        """
