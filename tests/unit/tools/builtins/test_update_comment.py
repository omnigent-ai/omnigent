"""Unit tests for :class:`UpdateCommentTool`."""

from __future__ import annotations

import dataclasses
import json
import time
from typing import Any

import pytest

from omnigent.entities.comment import Comment, CommentsFingerprint
from omnigent.stores.comment_store import CommentStore
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.update_comment import UpdateCommentTool

# ── In-memory store stub ──────────────────────────────────────────────────────


class _InMemoryCommentStore(CommentStore):
    """
    Minimal in-memory :class:`CommentStore` for unit tests.

    :param storage_location: Ignored; satisfies the ABC constructor.
    """

    def __init__(self) -> None:
        """Initialize with an empty comment dict."""
        super().__init__(storage_location="memory://")
        self._comments: dict[str, Comment] = {}

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
        """
        Create and persist a new comment.

        :param conversation_id: Owning conversation id.
        :param path: File path.
        :param body: Comment text.
        :param start_index: Start character offset.
        :param end_index: End character offset.
        :param anchor_content: Anchored text snapshot.
        :param created_by: Email of creating user.
        :returns: The created :class:`Comment`.
        """
        import uuid

        comment = Comment(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            path=path,
            body=body,
            start_index=start_index,
            end_index=end_index,
            anchor_content=anchor_content,
            created_by=created_by,
            status="draft",
            # One clock read: created_at derives from updated_at's instant,
            # matching the SQL store's never-edited invariant.
            created_at=(created_us := time.time_ns() // 1_000) // 1_000_000,
            updated_at=created_us,
        )
        self._comments[comment.id] = comment
        return comment

    def get(self, comment_id: str, conversation_id: str) -> Comment | None:
        """
        Fetch a comment by id, scoped to a conversation.

        :param comment_id: The comment id.
        :param conversation_id: The conversation the comment must belong to;
            a comment owned by another conversation is reported as not found.
        :returns: The :class:`Comment`, or ``None`` if not found or not owned
            by ``conversation_id``.
        """
        comment = self._comments.get(comment_id)
        if comment is None or comment.conversation_id != conversation_id:
            return None
        return comment

    def list_for_conversation(
        self,
        conversation_id: str,
        path: str | None = None,
    ) -> list[Comment]:
        """
        List comments for a conversation.

        :param conversation_id: The conversation to query.
        :param path: Optional file path filter.
        :returns: Matching comments.
        """
        return [
            c
            for c in self._comments.values()
            if c.conversation_id == conversation_id and (path is None or c.path == path)
        ]

    def update_comment(
        self,
        comment_id: str,
        conversation_id: str,
        *,
        status: str | None = None,
        body: str | None = None,
    ) -> Comment | None:
        """
        Update mutable fields on a comment, scoped to a conversation.

        :param comment_id: The comment to update.
        :param conversation_id: The conversation the comment must belong to;
            a comment owned by another conversation is left untouched and
            reported as not found.
        :param status: New status.
        :param body: New body text.
        :returns: Updated :class:`Comment`, or ``None`` if not found or not
            owned by ``conversation_id``.
        """
        comment = self._comments.get(comment_id)
        if comment is None or comment.conversation_id != conversation_id:
            return None
        updated = dataclasses.replace(
            comment,
            status=status if status is not None else comment.status,
            body=body if body is not None else comment.body,
            updated_at=time.time_ns() // 1_000,
        )
        self._comments[comment_id] = updated
        return updated

    def delete(self, comment_id: str, conversation_id: str) -> Comment | None:
        """
        Delete a comment by id, scoped to a conversation.

        :param comment_id: The comment to delete.
        :param conversation_id: The conversation the comment must belong to;
            a comment owned by another conversation is left in place and
            reported as not found.
        :returns: Deleted :class:`Comment`, or ``None`` if not found or not
            owned by ``conversation_id``.
        """
        comment = self._comments.get(comment_id)
        if comment is None or comment.conversation_id != conversation_id:
            return None
        return self._comments.pop(comment_id, None)

    def get_comments_fingerprints(
        self, conversation_ids: list[str]
    ) -> dict[str, CommentsFingerprint]:
        """
        Return per-conversation comment fingerprints.

        :param conversation_ids: The conversations to summarize.
        :returns: Map from conversation id to fingerprint; conversations
            with no comments are absent.
        """
        result: dict[str, CommentsFingerprint] = {}
        for cid in conversation_ids:
            comments = [c for c in self._comments.values() if c.conversation_id == cid]
            if comments:
                result[cid] = CommentsFingerprint(
                    count=len(comments),
                    last_updated_at=max(c.updated_at for c in comments),
                )
        return result

    def remove_conversation(self, conversation_id: str) -> None:
        """
        Remove all comments for a conversation.

        :param conversation_id: The conversation whose comments to remove.
        """
        to_delete = [
            cid for cid, c in self._comments.items() if c.conversation_id == conversation_id
        ]
        for cid in to_delete:
            del self._comments[cid]


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ctx(conversation_id: str | None = "conv-123") -> ToolContext:
    """
    Build a :class:`ToolContext` for testing.

    :param conversation_id: Session id to embed.
    :returns: A minimal :class:`ToolContext` instance.
    """
    return ToolContext(task_id="task-test", agent_id="ag-test", conversation_id=conversation_id)


def _invoke(
    tool: UpdateCommentTool,
    args: dict[str, Any],
    conversation_id: str | None = "conv-123",
) -> dict[str, Any]:
    """
    Invoke *tool* and parse the JSON response.

    :param tool: The tool under test.
    :param args: Arguments dict to JSON-encode.
    :param conversation_id: Session id for the context.
    :returns: Parsed response dict.
    """
    return json.loads(tool.invoke(json.dumps(args), _ctx(conversation_id)))


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def store() -> _InMemoryCommentStore:
    """
    Fresh in-memory comment store for each test.

    :returns: An empty :class:`_InMemoryCommentStore`.
    """
    return _InMemoryCommentStore()


@pytest.fixture()
def tool(store: _InMemoryCommentStore, monkeypatch: pytest.MonkeyPatch) -> UpdateCommentTool:
    """
    :class:`UpdateCommentTool` wired to an in-memory comment store.

    Patches ``omnigent.runtime.get_comment_store`` so the tool uses
    *store* without needing the real runtime initialised.
    The import is lazy (inside ``invoke``), so we patch the source module.

    :param store: In-memory comment store fixture.
    :param monkeypatch: pytest monkeypatching fixture.
    :returns: Configured :class:`UpdateCommentTool` instance.
    """
    import omnigent.runtime as _runtime

    monkeypatch.setattr(_runtime, "get_comment_store", lambda: store)
    return UpdateCommentTool()


# ── Identity tests ─────────────────────────────────────────────────────────────


def test_name() -> None:
    """``name()`` must return the canonical tool name used by the harness schema."""
    assert UpdateCommentTool.name() == "update_comment"


def test_description_non_empty() -> None:
    """``description()`` must return a non-empty string (sent to the LLM as schema)."""
    assert UpdateCommentTool.description()


# ── Schema tests ───────────────────────────────────────────────────────────────


def test_schema_shape() -> None:
    """
    ``get_schema()`` must return a valid OpenAI function-calling schema.

    ``comment_id`` and ``status`` are required — the LLM must supply both
    for the tool to do anything useful. If either is absent from
    ``"required"`` the LLM may omit it and the tool silently errors.
    """
    schema = UpdateCommentTool().get_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "update_comment"
    params = fn["parameters"]
    assert params["type"] == "object"
    assert set(params["required"]) == {"comment_id", "status"}
    assert "comment_id" in params["properties"]
    assert "status" in params["properties"]
    assert set(params["properties"]["status"]["enum"]) == {"draft", "addressed"}


# ── Error-path tests ───────────────────────────────────────────────────────────


def test_no_conversation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Returns an error when ``ctx.conversation_id`` is ``None``.

    Without a session id the tool cannot verify ownership; an error is
    returned rather than allowing a cross-session update.
    """
    import omnigent.runtime as _runtime

    monkeypatch.setattr(_runtime, "get_comment_store", lambda: _InMemoryCommentStore())
    t = UpdateCommentTool()
    result = json.loads(
        t.invoke('{"comment_id": "x", "status": "addressed"}', _ctx(conversation_id=None))
    )
    assert "error" in result
    assert "conversation" in result["error"]


def test_no_store_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Returns an error when ``get_comment_store()`` returns ``None``.

    Deployments without Omnigent server do not initialise a comment store;
    the tool must surface a clear message rather than raising on
    ``None.get(...)``.
    """
    import omnigent.runtime as _runtime

    monkeypatch.setattr(_runtime, "get_comment_store", lambda: None)
    t = UpdateCommentTool()
    result = json.loads(t.invoke('{"comment_id": "x", "status": "addressed"}', _ctx()))
    assert "error" in result
    assert "comment store" in result["error"]


def test_missing_comment_id(tool: UpdateCommentTool) -> None:
    """
    Returns an error when ``comment_id`` is absent from arguments.

    The schema marks ``comment_id`` as required, but the tool validates
    defensively because the LLM might still omit it. A missing id would
    otherwise cause a store lookup with ``None``.
    """
    result = _invoke(tool, {"status": "addressed"})
    assert "error" in result
    assert "comment_id" in result["error"]


def test_missing_status(tool: UpdateCommentTool) -> None:
    """
    Returns an error when ``status`` is absent from arguments.

    Without a status value there is nothing to update; an error prevents
    a no-op store write and surfaces the problem to the LLM.
    """
    result = _invoke(tool, {"comment_id": "some-id"})
    assert "error" in result
    assert "status" in result["error"]


def test_invalid_status(tool: UpdateCommentTool, store: _InMemoryCommentStore) -> None:
    """
    Returns an error when ``status`` is not one of the valid values.

    The store only accepts ``"draft"`` or ``"addressed"``; writing an
    invalid status would corrupt the comment's state machine. The tool
    validates before touching the store.
    """
    c = store.add("conv-123", "app.py", "comment", 0, 5)
    result = _invoke(tool, {"comment_id": c.id, "status": "resolved"})
    assert "error" in result
    assert "invalid status" in result["error"]
    # The comment must be unchanged after a rejected update.
    unchanged = store.get(c.id, "conv-123")
    assert unchanged is not None
    assert unchanged.status == "draft", (
        "Comment status was mutated despite an invalid-status error being returned."
    )


def test_comment_not_found(tool: UpdateCommentTool) -> None:
    """
    Returns an error when the comment id does not exist.

    The agent may pass a stale id (e.g. from a previous turn's
    list_comments call after the comment was deleted). The tool must
    return a clear error rather than silently succeeding.
    """
    result = _invoke(tool, {"comment_id": "nonexistent-id", "status": "addressed"})
    assert "error" in result
    assert "not found" in result["error"]


def test_cross_session_comment_rejected(
    tool: UpdateCommentTool,
    store: _InMemoryCommentStore,
) -> None:
    """
    Returns an error when the comment belongs to a different session.

    Multi-user isolation: a comment created in session B must not be
    updatable from session A, even if the caller knows the comment id.
    If this check is missing, any session can mutate any comment.
    """
    # Comment belongs to a DIFFERENT session.
    other = store.add("conv-OTHER", "app.py", "Other session comment", 0, 5)

    # Invoke from session "conv-123" — should be rejected.
    result = _invoke(tool, {"comment_id": other.id, "status": "addressed"})
    assert "error" in result
    assert "not found" in result["error"], (
        f"Expected 'not found' error for cross-session comment, got: {result!r}. "
        "If missing, session isolation is broken — any session can update any comment."
    )
    # The comment status must be unchanged after the rejected call.
    unchanged = store.get(other.id, "conv-OTHER")
    assert unchanged is not None
    assert unchanged.status == "draft"


# ── Happy-path tests ───────────────────────────────────────────────────────────


def test_updates_status_to_addressed(
    tool: UpdateCommentTool,
    store: _InMemoryCommentStore,
) -> None:
    """
    Successfully updates a draft comment to ``"addressed"``.

    This is the primary happy-path: the agent calls ``update_comment``
    after fixing the issue a draft comment describes. The response must
    contain the updated comment, and the store must reflect the change.
    """
    c = store.add("conv-123", "app.py", "Rename x to count", 0, 10)
    assert c.status == "draft"

    result = _invoke(tool, {"comment_id": c.id, "status": "addressed"})
    assert "comment" in result, f"Expected 'comment' key in response, got {result!r}."

    returned = result["comment"]
    assert returned["id"] == c.id
    assert returned["status"] == "addressed", (
        f"Returned status is {returned['status']!r}; expected 'addressed'. "
        "If still 'draft', update_comment did not persist the change."
    )
    # Store must also reflect the update — not just the return value.
    stored = store.get(c.id, "conv-123")
    assert stored is not None
    assert stored.status == "addressed", (
        f"Store status is {stored.status!r} after update_comment returned 'addressed'. "
        "The tool may be returning the pre-update value instead of the stored one."
    )


def test_updates_status_back_to_draft(
    tool: UpdateCommentTool,
    store: _InMemoryCommentStore,
) -> None:
    """
    An already-addressed comment can be reset to ``"draft"``.

    Allows the user to reopen a comment that was incorrectly marked as
    done. The tool must accept ``"draft"`` as a valid target status.
    """
    c = store.add("conv-123", "app.py", "Fix me", 0, 5)
    store.update_comment(c.id, "conv-123", status="addressed")

    result = _invoke(tool, {"comment_id": c.id, "status": "draft"})
    assert "comment" in result
    assert result["comment"]["status"] == "draft"


def test_response_contains_all_comment_fields(
    tool: UpdateCommentTool,
    store: _InMemoryCommentStore,
) -> None:
    """
    The returned comment dict contains all entity fields.

    The agent may display or log the updated comment; if fields are
    missing it gets incomplete data and may misreport the result.
    """
    c = store.add(
        "conv-123",
        "app.py",
        "Fix indentation",
        start_index=10,
        end_index=20,
        anchor_content="  x = 1",
    )
    result = _invoke(tool, {"comment_id": c.id, "status": "addressed"})
    entry = result["comment"]

    assert entry["id"] == c.id
    assert entry["path"] == "app.py"
    assert entry["body"] == "Fix indentation"
    assert entry["status"] == "addressed"
    assert entry["start_index"] == 10
    assert entry["end_index"] == 20
    assert entry["anchor_content"] == "  x = 1"
    assert entry["conversation_id"] == "conv-123"
