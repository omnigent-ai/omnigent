"""Unit tests for :class:`ListCommentsTool`."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from omnigent.entities.comment import Comment, CommentsFingerprint
from omnigent.stores.comment_store import CommentStore
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.list_comments import ListCommentsTool

# ── In-memory store stub ──────────────────────────────────────────────────────


class _InMemoryCommentStore(CommentStore):
    """
    Minimal in-memory :class:`CommentStore` for unit tests.

    :param storage_location: Ignored; exists only to satisfy the ABC
        constructor.
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
        Create and store a new comment.

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
        List comments for a conversation, optionally filtered by file.

        :param conversation_id: The conversation to query.
        :param path: When provided, only return comments for this file.
        :returns: Matching comments ordered by ``created_at``.
        """
        results = [
            c
            for c in self._comments.values()
            if c.conversation_id == conversation_id and (path is None or c.path == path)
        ]
        return sorted(results, key=lambda c: c.created_at)

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
        import dataclasses

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
    tool: ListCommentsTool,
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
def tool(store: _InMemoryCommentStore, monkeypatch: pytest.MonkeyPatch) -> ListCommentsTool:
    """
    :class:`ListCommentsTool` wired to an in-memory comment store.

    Patches ``omnigent.runtime.get_comment_store`` so that the tool
    uses *store* without needing the real runtime initialised.
    The import is lazy (inside ``invoke``), so we patch the source module.

    :param store: In-memory comment store fixture.
    :param monkeypatch: pytest monkeypatching fixture.
    :returns: Configured :class:`ListCommentsTool` instance.
    """
    import omnigent.runtime as _runtime

    monkeypatch.setattr(_runtime, "get_comment_store", lambda: store)
    return ListCommentsTool()


# ── Identity tests ─────────────────────────────────────────────────────────────


def test_name() -> None:
    """``name()`` must return the canonical tool name the harness schema uses."""
    assert ListCommentsTool.name() == "list_comments"


def test_description_non_empty() -> None:
    """``description()`` must return a non-empty string (schema sent to LLM)."""
    assert ListCommentsTool.description()


# ── Schema tests ───────────────────────────────────────────────────────────────


def test_schema_shape() -> None:
    """
    ``get_schema()`` must return a valid OpenAI function-calling schema.

    The schema is forwarded verbatim to the LLM. If the top-level
    ``"type"`` or function ``"name"`` is wrong the LLM cannot call the
    tool at all.
    """
    schema = ListCommentsTool().get_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "list_comments"
    params = fn["parameters"]
    assert params["type"] == "object"
    # Both filters are optional
    assert params["required"] == []
    assert "path" in params["properties"]
    assert "status" in params["properties"]
    # Status must enumerate the two valid values
    assert set(params["properties"]["status"]["enum"]) == {"draft", "addressed"}


# ── Error-path tests ───────────────────────────────────────────────────────────


def test_no_conversation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Returns an error when ``ctx.conversation_id`` is ``None``.

    The comment query cannot be scoped to a session without an id;
    returning an error prevents the tool from leaking another session's
    comments or crashing on a ``None`` store key.
    """
    import omnigent.runtime as _runtime

    monkeypatch.setattr(_runtime, "get_comment_store", lambda: _InMemoryCommentStore())
    t = ListCommentsTool()
    result = json.loads(t.invoke("{}", _ctx(conversation_id=None)))
    assert "error" in result
    # Verify the error is specifically about missing conversation context,
    # not a store error — both return "error" but the messages differ.
    assert "conversation" in result["error"]


def test_no_store_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Returns an error when ``get_comment_store()`` returns ``None``.

    This happens in deployments that don't initialise a comment store
    (e.g. standalone REPL without Omnigent server). The tool must surface a
    clear message rather than raising AttributeError.
    """
    import omnigent.runtime as _runtime

    monkeypatch.setattr(_runtime, "get_comment_store", lambda: None)
    t = ListCommentsTool()
    result = json.loads(t.invoke("{}", _ctx()))
    assert "error" in result
    assert "comment store" in result["error"]


# ── Happy-path tests ───────────────────────────────────────────────────────────


def test_returns_empty_when_no_comments(tool: ListCommentsTool) -> None:
    """
    Returns ``{"comments": []}`` when the session has no comments.

    An empty list (not ``None`` or a missing key) lets the agent know
    there is nothing to address rather than crashing on a missing key.
    """
    result = _invoke(tool, {})
    assert result == {"comments": []}, (
        f"Expected empty comments list, got {result!r}. "
        "If 'comments' key is missing the harness schema parsing breaks."
    )


def test_returns_all_comments_for_session(
    tool: ListCommentsTool,
    store: _InMemoryCommentStore,
) -> None:
    """
    Returns all comments belonging to the current session.

    Comments from other sessions must not appear — they are a different
    user's data. If the store leaks cross-session data this test fails.
    """
    c1 = store.add("conv-123", "app.py", "Fix typo", 0, 10)
    c2 = store.add("conv-123", "util.py", "Rename variable", 50, 60)
    # Comment from a different session — must not appear.
    store.add("conv-OTHER", "app.py", "Other session comment", 0, 5)

    result = _invoke(tool, {})
    ids = {c["id"] for c in result["comments"]}
    assert ids == {c1.id, c2.id}, (
        f"Expected exactly {c1.id!r} and {c2.id!r} in result, got {ids!r}. "
        "Cross-session comment leakage or missing comments."
    )


def test_filter_by_path(
    tool: ListCommentsTool,
    store: _InMemoryCommentStore,
) -> None:
    """
    ``path`` argument narrows results to a single file.

    The agent calls ``list_comments(path="app.py")`` to see only the
    comments on the file it is currently editing. If path filtering
    leaks other files' comments the agent gets irrelevant noise.
    """
    c1 = store.add("conv-123", "app.py", "Typo", 0, 10)
    store.add("conv-123", "util.py", "Rename", 50, 60)

    result = _invoke(tool, {"path": "app.py"})
    ids = {c["id"] for c in result["comments"]}
    assert ids == {c1.id}, (
        f"Expected only app.py comment ({c1.id!r}), got {ids!r}. "
        "Path filter is not applied or is filtering incorrectly."
    )


def test_filter_by_status_draft(
    tool: ListCommentsTool,
    store: _InMemoryCommentStore,
) -> None:
    """
    ``status="draft"`` returns only open (unaddressed) comments.

    The agent calls this before a fix pass to see only what needs doing.
    If addressed comments bleed through, the agent re-processes already-
    handled items.
    """
    draft = store.add("conv-123", "app.py", "Needs fix", 0, 10)
    addressed = store.add("conv-123", "app.py", "Already done", 20, 30)
    store.update_comment(addressed.id, "conv-123", status="addressed")

    result = _invoke(tool, {"status": "draft"})
    ids = {c["id"] for c in result["comments"]}
    assert ids == {draft.id}, (
        f"Expected only draft comment ({draft.id!r}), got {ids!r}. "
        f"If addressed comment {addressed.id!r} appears, status filter is broken."
    )


def test_filter_by_status_addressed(
    tool: ListCommentsTool,
    store: _InMemoryCommentStore,
) -> None:
    """
    ``status="addressed"`` returns only resolved comments.

    Allows the agent (or user) to review what has already been handled.
    """
    store.add("conv-123", "app.py", "Needs fix", 0, 10)
    addressed = store.add("conv-123", "app.py", "Already done", 20, 30)
    store.update_comment(addressed.id, "conv-123", status="addressed")

    result = _invoke(tool, {"status": "addressed"})
    ids = {c["id"] for c in result["comments"]}
    assert ids == {addressed.id}, (
        f"Expected only addressed comment ({addressed.id!r}), got {ids!r}."
    )


def test_filter_by_path_and_status(
    tool: ListCommentsTool,
    store: _InMemoryCommentStore,
) -> None:
    """
    Both ``path`` and ``status`` filters can be combined.

    The intersection must be applied: only comments matching BOTH
    constraints appear. Cross-product leakage would include comments
    from the wrong file or with the wrong status.
    """
    target = store.add("conv-123", "app.py", "Draft on app.py", 0, 10)
    # Same file, addressed — must be excluded by status filter.
    other_status = store.add("conv-123", "app.py", "Addressed on app.py", 20, 30)
    store.update_comment(other_status.id, "conv-123", status="addressed")
    # Same status, different file — must be excluded by path filter.
    store.add("conv-123", "util.py", "Draft on util.py", 0, 5)

    result = _invoke(tool, {"path": "app.py", "status": "draft"})
    ids = {c["id"] for c in result["comments"]}
    assert ids == {target.id}, (
        f"Expected only {target.id!r} (app.py + draft), got {ids!r}. "
        "Combined path+status filter is broken."
    )


def test_comment_fields_are_complete(
    tool: ListCommentsTool,
    store: _InMemoryCommentStore,
) -> None:
    """
    Each returned comment includes all fields the agent needs.

    The agent reads ``id``, ``path``, ``body``, ``status``, and
    ``anchor_content`` to understand and address each comment. A missing
    field would silently cause the agent to pass wrong data to
    ``update_comment`` or misread the comment text.
    """
    c = store.add(
        "conv-123",
        "app.py",
        "Rename x to count",
        start_index=5,
        end_index=15,
        anchor_content="x = 0",
    )

    result = _invoke(tool, {})
    assert len(result["comments"]) == 1
    entry = result["comments"][0]

    # Each field is checked individually so a failure message names the
    # missing field rather than just "assertion failed".
    assert entry["id"] == c.id
    assert entry["path"] == "app.py"
    assert entry["body"] == "Rename x to count"
    assert entry["status"] == "draft"
    assert entry["start_index"] == 5
    assert entry["end_index"] == 15
    assert entry["anchor_content"] == "x = 0"
    assert entry["conversation_id"] == "conv-123"


def test_malformed_json_arguments(tool: ListCommentsTool) -> None:
    """
    Malformed JSON arguments return a structured error, not an exception.

    The harness may send a non-JSON string if the LLM produces bad output.
    The tool must return ``{"error": ...}`` rather than raising and crashing
    the tool dispatch loop.
    """
    result = json.loads(tool.invoke("not-valid-json{", _ctx()))
    assert "error" in result
    assert "malformed" in result["error"]


def test_empty_arguments_string(
    tool: ListCommentsTool,
    store: _InMemoryCommentStore,
) -> None:
    """
    An empty arguments string is treated as no filters (returns all).

    The harness may send ``""`` when the LLM calls the tool without
    arguments. ``json.loads("")`` raises; the tool guards against this
    with ``if arguments.strip()``.
    """
    store.add("conv-123", "app.py", "comment", 0, 5)
    result = json.loads(tool.invoke("", _ctx()))
    assert "comments" in result
    # The single comment must appear — empty args means "no filter", not "nothing".
    assert len(result["comments"]) == 1, (
        f"Expected 1 comment for empty-args call, got {len(result['comments'])}. "
        "If 0, the tool may be treating '' as an error instead of 'no filter'."
    )
