"""Work-item entity — persisted in the ``work_items`` table.

A work item is a unit of tracked work — created by hand or ingested from
Slack, email, GitHub, or Jira — that an agent can pick up and process in a
linked conversation (the conversation tree already gives each item its own
sub-session/thread, plan, and history). Work items are deliberately a thin
layer *over* conversations rather than a revival of the old, removed ``tasks``
table: the item row carries lifecycle/source/dedup metadata and points at the
conversation that does the work.
"""

from __future__ import annotations

from dataclasses import dataclass

# Allowed lifecycle states. Single source of truth for the store, tools, and
# API so they validate the same set.
WORK_ITEM_STATUSES: frozenset[str] = frozenset(
    {"new", "planned", "in_progress", "blocked", "needs_review", "done"}
)

# Allowed intake sources.
WORK_ITEM_SOURCES: frozenset[str] = frozenset({"manual", "slack", "email", "github", "jira"})


@dataclass
class WorkItem:
    """
    A work item persisted in the ``work_items`` table.

    :param id: Opaque primary key, e.g. ``"wi_a1b2c3..."``.
    :param source: Where the item came from — one of
        :data:`WORK_ITEM_SOURCES` (``"manual"``, ``"slack"``,
        ``"email"``, ``"github"``, ``"jira"``).
    :param title: Short human-readable title.
    :param dedup_key: Idempotency key (globally UNIQUE), e.g.
        ``"github:acme/app#123"``. Re-ingesting the same external event
        resolves to the existing row instead of creating a duplicate.
    :param status: Lifecycle state — one of :data:`WORK_ITEM_STATUSES`.
    :param created_at: Unix epoch seconds at row creation.
    :param external_id: The source's native identifier (issue/PR number,
        message ts, …), or ``None`` for ``"manual"`` items.
    :param body: Optional longer description / original payload text.
    :param pr_url: Optional pull-request URL once the work produces one.
    :param conversation_id: The conversation/sub-session processing this
        item, or ``None`` before one is started. Cleared (``SET NULL``) if
        that conversation is deleted.
    :param assignee_user_id: User the item is assigned to, or ``None``.
    :param created_by: User id that created the item, or ``None`` (e.g. a
        service-ingested item).
    :param plan: Optional free-text / JSON plan the agent recorded for the
        item, surfaced in the UI.
    :param updated_at: Unix epoch seconds of the last write, or ``None`` if
        the row has never been updated.
    """

    id: str
    source: str
    title: str
    dedup_key: str
    status: str
    created_at: int
    external_id: str | None = None
    body: str | None = None
    pr_url: str | None = None
    conversation_id: str | None = None
    assignee_user_id: str | None = None
    created_by: str | None = None
    plan: str | None = None
    updated_at: int | None = None
