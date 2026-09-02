"""Comments store: manages per-review comments for a conversation."""

from abc import ABC, abstractmethod

from omnigent.entities import Comment, CommentsFingerprint


class CommentStore(ABC):
    """Abstract base for file comment persistence.

    Manages the lifecycle of per-review comments: creation,
    listing with optional path filtering, status/body mutation,
    single-comment deletion, and bulk conversation cleanup.
    """

    def __init__(self, storage_location: str) -> None:
        """Initialize the comments store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    @abstractmethod
    def get(self, comment_id: str, conversation_id: str) -> Comment | None:
        """Fetch a single comment by id, scoped to a conversation, without mutating it.

        The lookup is scoped to ``conversation_id`` so callers cannot read
        a comment that belongs to a different conversation: a comment whose
        owning conversation does not match is treated as not found.

        :param comment_id: The comment to fetch, e.g. ``"a1b2c3d4-..."``.
        :param conversation_id: The conversation the comment must belong to,
            e.g. ``"conv_abc123"``. A comment owned by any other conversation
            is reported as not found (``None``).
        :returns: The :class:`Comment`, or ``None`` if no comment with that
            id exists or it is not owned by ``conversation_id``.
        """
        ...

    @abstractmethod
    def add(
        self,
        conversation_id: str,
        path: str,
        body: str,
        start_index: int,
        end_index: int,
        anchor_content: str | None = None,
        created_by: str | None = None,
    ) -> Comment:
        """Create and persist a new comment.

        :param conversation_id: The owning conversation,
            e.g. ``"conv_abc123"``.
        :param path: File path relative to workspace root,
            e.g. ``"src/App.tsx"``.
        :param body: The comment text.
        :param start_index: 0-based absolute character offset (inclusive)
            within the file where the anchor range begins.
        :param end_index: 0-based absolute character offset (exclusive)
            within the file where the anchor range ends.
        :param anchor_content: Plain-text snapshot of the selected range,
            used to re-anchor the comment after file edits. ``None`` if
            not provided.
        :param created_by: Email of the creating user, e.g.
            ``"alice@example.com"``. ``None`` in single-user mode.
        :returns: The newly created :class:`Comment`.
        """
        ...

    @abstractmethod
    def list_for_conversation(
        self,
        conversation_id: str,
        path: str | None = None,
    ) -> list[Comment]:
        """Return all comments for a conversation, optionally filtered by file.

        :param conversation_id: The conversation to query,
            e.g. ``"conv_abc123"``.
        :param path: When provided, only return comments for this file,
            e.g. ``"src/App.tsx"``. ``None`` returns all files.
        :returns: List of matching :class:`Comment` objects ordered by
            ``created_at`` ascending.
        """
        ...

    @abstractmethod
    def update_comment(
        self,
        comment_id: str,
        conversation_id: str,
        *,
        status: str | None = None,
        body: str | None = None,
    ) -> Comment | None:
        """Update mutable fields on a comment, scoped to a conversation.

        The update is scoped to ``conversation_id`` so callers cannot mutate
        a comment that belongs to a different conversation: a comment whose
        owning conversation does not match is left untouched and reported as
        not found.

        :param comment_id: The comment to update,
            e.g. ``"a1b2c3d4-..."``.
        :param conversation_id: The conversation the comment must belong to,
            e.g. ``"conv_abc123"``. A comment owned by any other conversation
            is not modified and reported as not found (``None``).
        :param status: New status, e.g. ``"addressed"``. ``None`` leaves
            it unchanged.
        :param body: New comment body text. ``None`` leaves it unchanged.
            A body rewrite also stamps ``edited_at`` (status-only changes
            do not), so clients can flag visibly edited comments.
        :returns: The updated :class:`Comment`, or ``None`` if no comment with
            that id exists or it is not owned by ``conversation_id``.
        """
        ...

    @abstractmethod
    def delete(self, comment_id: str, conversation_id: str) -> Comment | None:
        """Delete a single comment by id, scoped to a conversation.

        The delete is scoped to ``conversation_id`` so callers cannot delete
        a comment that belongs to a different conversation: a comment whose
        owning conversation does not match is left in place and reported as
        not found.

        :param comment_id: The comment to delete, e.g. ``"a1b2c3d4-..."``.
        :param conversation_id: The conversation the comment must belong to,
            e.g. ``"conv_abc123"``. A comment owned by any other conversation
            is not deleted and reported as not found (``None``).
        :returns: The deleted :class:`Comment`, or ``None`` if no comment with
            that id exists or it is not owned by ``conversation_id``.
        """
        ...

    @abstractmethod
    def get_comments_fingerprints(
        self, conversation_ids: list[str]
    ) -> dict[str, CommentsFingerprint]:
        """Return a change-detection fingerprint per conversation, batched.

        One aggregate query for the whole batch — called per tick by the
        ``WS /v1/sessions/updates`` stream and per page by
        ``GET /v1/sessions``, so it must not fan out per conversation.

        :param conversation_ids: The conversations to summarize,
            e.g. ``["conv_abc123", "conv_def456"]``.
        :returns: Map from conversation id to its
            :class:`CommentsFingerprint`. Conversations with no comments
            are absent from the map.
        """
        ...

    @abstractmethod
    def remove_conversation(self, conversation_id: str) -> None:
        """Delete all comments for a conversation.

        Called when the conversation itself is deleted so the database
        does not hold orphaned comment rows indefinitely.

        :param conversation_id: The conversation whose comments to remove,
            e.g. ``"conv_abc123"``.
        """
        ...
