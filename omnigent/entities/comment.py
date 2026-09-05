"""Comment entity: a single per-review comment."""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class Comment:
    """A single review comment anchored to a text range in a file.

    :param id: UUID for this comment, e.g. ``"a1b2c3d4-..."``.
    :param conversation_id: The conversation this comment belongs to,
        e.g. ``"conv_abc123"``.
    :param path: File path relative to the workspace root,
        e.g. ``"src/App.tsx"``.
    :param start_index: 0-based absolute character offset (inclusive)
        within the file where the anchor range begins.
    :param end_index: 0-based absolute character offset (exclusive)
        within the file where the anchor range ends.
    :param body: The comment text.
    :param status: One of ``"draft"`` (open, not yet addressed)
        or ``"addressed"`` (fixed by the agent or user).
    :param created_at: Unix timestamp (seconds) when the comment was
        created.
    :param updated_at: Unix timestamp in **microseconds** when the
        comment was last mutated (body or status change); set at
        creation time for never-edited comments. Microsecond precision
        (vs ``created_at``'s seconds) so back-to-back mutations inside
        the same second still produce distinct, ordered values for the
        session comments fingerprint, while staying an exact integer
        in JavaScript (epoch-µs < ``Number.MAX_SAFE_INTEGER``).
    :param anchor_content: Plain-text snapshot of the selected range at
        comment creation time. Used to re-anchor the comment when the
        file is subsequently edited. ``None`` for legacy comments.
    :param created_by: Email of the user who created the comment,
        e.g. ``"alice@example.com"``. ``None`` for legacy comments created
        before per-user attribution was added, or in single-user mode.
    :param edited_at: Unix timestamp in **microseconds** of the last
        **body** edit, or ``None`` when the text was never rewritten.
        Unlike ``updated_at`` it is not bumped by status-only changes
        (e.g. marking a comment addressed), so clients can surface an
        "edited" indicator only when the visible text actually changed.
    """

    id: str
    conversation_id: str
    path: str
    start_index: int
    end_index: int
    body: str
    status: str  # "draft" | "addressed"
    created_at: int  # unix timestamp (seconds)
    updated_at: int  # unix timestamp (microseconds) of last body/status mutation
    anchor_content: str | None = None
    created_by: str | None = None
    edited_at: int | None = None


@dataclasses.dataclass
class CommentsFingerprint:
    """Change-detection summary of one conversation's comments.

    Consumed by the session-list surfaces (``GET /v1/sessions`` and
    ``WS /v1/sessions/updates``) so clients can detect that a session's
    comments changed without fetching them: an add or edit bumps
    ``last_updated_at``, a delete changes ``count``. Together the two
    fields change for every mutation kind.

    :param count: Total number of comments (any status) currently
        stored for the conversation.
    :param last_updated_at: Unix timestamp in microseconds of the most
        recently mutated comment (max ``updated_at`` across the
        conversation's comments).
    """

    count: int
    last_updated_at: int
