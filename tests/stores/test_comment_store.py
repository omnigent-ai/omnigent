"""Tests for :class:`SqlAlchemyCommentStore`.

Exercises all public CRUD methods against a real SQLite database
(migrations applied via :func:`get_or_create_engine`), following the
same pattern used by :mod:`tests.stores.test_artifact_store`.

The ``db_uri`` fixture in the root conftest creates a fresh per-test
SQLite file and tears it down automatically.
"""

from __future__ import annotations

import pytest

from omnigent.entities.comment import CommentsFingerprint
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyCommentStore:
    """A fresh :class:`SqlAlchemyCommentStore` backed by the test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest fixture.
    :returns: A ready-to-use :class:`SqlAlchemyCommentStore` instance.
    """
    return SqlAlchemyCommentStore(db_uri)


# ── add ───────────────────────────────────────────────────────────────────────


def test_add_returns_comment_with_id(store: SqlAlchemyCommentStore) -> None:
    """``add`` returns a Comment with a non-empty id and correct field values.

    If this test fails the add method is not returning the persisted entity or
    one of the field mappings is broken.
    """
    comment = store.add(
        conversation_id="conv_abc",
        path="src/app.py",
        body="Needs a null check",
        start_index=4,
        end_index=20,
    )

    # The store must assign a non-empty UUID-style id.
    assert comment.id, "add() must return a Comment with a non-empty id"
    assert comment.conversation_id == "conv_abc", (
        "conversation_id must be echoed back exactly as given"
    )
    assert comment.path == "src/app.py"
    assert comment.start_index == 4
    assert comment.end_index == 20
    assert comment.body == "Needs a null check"
    # New comments must start in draft status.
    assert comment.status == "draft", (
        f"Expected new comment status 'draft', got {comment.status!r}"
    )
    # created_at must be a positive epoch timestamp.
    assert comment.created_at > 0, (
        f"created_at must be a positive epoch int, got {comment.created_at!r}"
    )
    # created_by defaults to None when not supplied.
    assert comment.created_by is None, (
        f"created_by should be None when not passed to add(), got {comment.created_by!r}"
    )


def test_add_stores_created_by(store: SqlAlchemyCommentStore) -> None:
    """``add`` with created_by= persists the value and returns it on round-trip.

    Verifies the author email survives the add() call, the _to_entity()
    mapping, and a subsequent get() fetch — the full persistence round-trip.
    If this fails, created_by is either not written to the DB or not mapped
    back onto the entity.
    """
    comment = store.add(
        conversation_id="conv_author",
        path="src/app.py",
        body="Review this",
        start_index=0,
        end_index=10,
        created_by="alice@example.com",
    )

    # add() return value must carry the author.
    assert comment.created_by == "alice@example.com", (
        f"Expected created_by='alice@example.com' from add(), got {comment.created_by!r}. "
        "The value was not stored or not mapped back onto the entity."
    )

    # get() must also return the stored author.
    fetched = store.get(comment.id, "conv_author")
    assert fetched is not None
    assert fetched.created_by == "alice@example.com", (
        f"Expected created_by='alice@example.com' from get(), got {fetched.created_by!r}. "
        "The value was persisted but _to_entity() is not mapping the column."
    )


def test_add_is_persisted_and_retrievable(store: SqlAlchemyCommentStore) -> None:
    """Comment added by ``add`` is immediately visible via ``list_for_conversation``.

    Confirms the comment was actually written to the DB, not just returned
    in-memory.
    """
    comment = store.add(
        conversation_id="conv_persist",
        path="utils.py",
        body="Add type hint",
        start_index=0,
        end_index=10,
    )

    listed = store.list_for_conversation("conv_persist")

    # Exactly one comment should exist for this fresh conversation.
    assert len(listed) == 1, (
        f"Expected 1 comment after one add(), got {len(listed)}. "
        "The comment was not persisted or listing is scoped incorrectly."
    )
    assert listed[0].id == comment.id
    assert listed[0].body == "Add type hint"


# ── list_for_conversation ─────────────────────────────────────────────────────


def test_list_for_conversation_returns_all_comments(store: SqlAlchemyCommentStore) -> None:
    """``list_for_conversation`` without path filter returns all comments."""
    store.add(
        conversation_id="conv_list",
        path="a.py",
        body="First",
        start_index=0,
        end_index=5,
    )
    store.add(
        conversation_id="conv_list",
        path="b.py",
        body="Second",
        start_index=0,
        end_index=6,
    )

    all_comments = store.list_for_conversation("conv_list")

    # Both comments must be returned.
    assert len(all_comments) == 2, (
        f"Expected 2 comments, got {len(all_comments)}. "
        "The path=None case is not returning all conversation comments."
    )
    bodies = {c.body for c in all_comments}
    assert bodies == {"First", "Second"}


def test_list_for_conversation_with_path_filter(store: SqlAlchemyCommentStore) -> None:
    """``list_for_conversation`` with path= returns only matching comments.

    A comment on a.py must not appear when listing b.py.
    """
    store.add(
        conversation_id="conv_filter",
        path="a.py",
        body="On A",
        start_index=0,
        end_index=4,
    )
    store.add(
        conversation_id="conv_filter",
        path="b.py",
        body="On B",
        start_index=0,
        end_index=4,
    )

    a_only = store.list_for_conversation("conv_filter", path="a.py")
    b_only = store.list_for_conversation("conv_filter", path="b.py")

    # Each filtered list must contain exactly the matching comment.
    assert len(a_only) == 1, (
        f"Expected 1 comment for a.py, got {len(a_only)}. "
        "The path filter is not scoping results correctly."
    )
    assert a_only[0].body == "On A"

    assert len(b_only) == 1, f"Expected 1 comment for b.py, got {len(b_only)}"
    assert b_only[0].body == "On B"


def test_list_for_conversation_returns_empty_for_unknown_conversation(
    store: SqlAlchemyCommentStore,
) -> None:
    """``list_for_conversation`` returns [] for a conversation with no comments."""
    result = store.list_for_conversation("conv_nobody")

    # The result must be an empty list, not None or an error.
    assert result == [], f"Expected [] for an unknown conversation, got {result!r}"


def test_list_for_conversation_isolation(store: SqlAlchemyCommentStore) -> None:
    """Comments from conversation A are invisible to conversation B.

    The conversation_id must act as an isolation boundary.
    """
    store.add(
        conversation_id="conv_A",
        path="x.py",
        body="Only in A",
        start_index=0,
        end_index=9,
    )

    b_comments = store.list_for_conversation("conv_B")

    # Conversation B must see zero comments even though conv_A has one.
    assert b_comments == [], (
        f"Expected no comments for conv_B, got {b_comments}. "
        "Comments are leaking across conversation boundaries."
    )


def test_list_for_conversation_ordered_by_created_at(
    store: SqlAlchemyCommentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``list_for_conversation`` returns comments in ``created_at`` ascending order.

    The oldest comment must come first regardless of path or start_index.
    """
    # created_at is seconds-granular; advance the clock a second per add so the
    # two comments get distinct created_at and the chronological assertion does
    # not hinge on the same-second (id) tiebreaker.
    from omnigent.stores.comment_store import sqlalchemy_store as _comment_store_mod

    clock_us = [1_700_000_000_000_000]

    def _fake_now_us() -> int:
        clock_us[0] += 1_000_000
        return clock_us[0]

    monkeypatch.setattr(_comment_store_mod, "now_epoch_us", _fake_now_us)

    c1 = store.add(
        conversation_id="conv_order",
        path="z.py",
        body="First added",
        start_index=0,
        end_index=5,
    )
    c2 = store.add(
        conversation_id="conv_order",
        path="a.py",
        body="Second added",
        start_index=0,
        end_index=6,
    )

    listed = store.list_for_conversation("conv_order")

    # Must be chronological: c1 was added first.
    assert listed[0].id == c1.id, (
        "First comment in listing should be the earliest (c1), "
        f"but got {listed[0].id!r}. The ORDER BY created_at asc may be broken."
    )
    assert listed[1].id == c2.id


# ── get ───────────────────────────────────────────────────────────────────────


def test_get_returns_comment_by_id(store: SqlAlchemyCommentStore) -> None:
    """``get`` returns the matching Comment when the id exists.

    If this test fails, ``get`` is not querying the right row or the
    returned entity has stale/missing field values.
    """
    comment = store.add(
        conversation_id="conv_get",
        path="read_me.py",
        body="Checked via get",
        start_index=2,
        end_index=15,
    )

    fetched = store.get(comment.id, "conv_get")

    # Must return the same comment, not None.
    assert fetched is not None, (
        "get() returned None for an id that was just added. "
        "The row was not persisted or get() is not querying correctly."
    )
    assert fetched.id == comment.id
    assert fetched.conversation_id == "conv_get"
    assert fetched.path == "read_me.py"
    assert fetched.start_index == 2
    assert fetched.end_index == 15
    assert fetched.body == "Checked via get"
    # Status must still be draft — get() must not mutate anything.
    assert fetched.status == "draft", (
        f"Expected status 'draft' (get must not mutate), got {fetched.status!r}"
    )


def test_get_returns_none_for_missing_id(store: SqlAlchemyCommentStore) -> None:
    """``get`` returns ``None`` when the comment id does not exist.

    Verifies the no-row path returns None rather than raising.
    """
    result = store.get("nonexistent-uuid-for-get", "conv_9f8e7d")

    assert result is None, (
        f"Expected None for an unknown comment id, got {result!r}. "
        "get() should return None, not raise, for missing ids."
    )


def test_get_returns_none_for_wrong_conversation(store: SqlAlchemyCommentStore) -> None:
    """``get`` returns ``None`` when the comment belongs to another conversation.

    Multi-user isolation: a comment created in conversation A must not be
    readable via ``get`` scoped to conversation B, even when the caller
    knows the exact comment id. If this returns the comment, the scoping is
    broken and any conversation could read another's comments by id.
    """
    comment = store.add(
        conversation_id="conv_a1b2c3",
        path="owned.py",
        body="Belongs to conv_owner",
        start_index=0,
        end_index=4,
    )

    # Same id, but scoped to a different conversation -> not found.
    cross = store.get(comment.id, "conv_d4e5f6")
    assert cross is None, (
        f"Expected None for a comment owned by a different conversation, got {cross!r}. "
        "get() must scope by conversation_id so callers cannot read across conversations."
    )

    # The owning conversation still sees it.
    owned = store.get(comment.id, "conv_a1b2c3")
    assert owned is not None and owned.id == comment.id, (
        "get() scoped to the owning conversation must still return the comment."
    )


def test_get_does_not_mutate_status(store: SqlAlchemyCommentStore) -> None:
    """``get`` does not change the comment's status or any other field.

    Calling get() twice must return the same status both times, and the
    comment must still be visible with the original status in listings.
    """
    comment = store.add(
        conversation_id="conv_get_immut",
        path="stable.py",
        body="Should not change",
        start_index=0,
        end_index=6,
    )

    store.get(comment.id, "conv_get_immut")
    store.get(comment.id, "conv_get_immut")

    # The listing must show the original draft status unchanged.
    listed = store.list_for_conversation("conv_get_immut")
    assert len(listed) == 1
    assert listed[0].status == "draft", (
        f"Status changed after get() calls — expected 'draft', got {listed[0].status!r}. "
        "get() must be read-only."
    )


# ── update_comment ────────────────────────────────────────────────────────────


def test_update_comment_status(store: SqlAlchemyCommentStore) -> None:
    """``update_comment`` with status= changes the status field only."""
    comment = store.add(
        conversation_id="conv_upd",
        path="api.py",
        body="Original",
        start_index=0,
        end_index=8,
    )

    updated = store.update_comment(comment.id, "conv_upd", status="addressed")

    assert updated is not None, "update_comment must return the updated Comment"
    # Status must be the new value.
    assert updated.status == "addressed", (
        f"Expected status 'addressed', got {updated.status!r}. "
        "update_comment is not persisting the status change."
    )
    # Body must be unchanged.
    assert updated.body == "Original", (
        f"Body changed unexpectedly: got {updated.body!r}. "
        "update_comment with only status= should not touch body."
    )


def test_update_comment_body(store: SqlAlchemyCommentStore) -> None:
    """``update_comment`` with body= changes the body field only."""
    comment = store.add(
        conversation_id="conv_upd_body",
        path="api.py",
        body="Old text",
        start_index=0,
        end_index=8,
    )

    updated = store.update_comment(comment.id, "conv_upd_body", body="New text")

    assert updated is not None
    assert updated.body == "New text", f"Expected body 'New text', got {updated.body!r}"
    # Status must remain 'draft'.
    assert updated.status == "draft", (
        f"Status changed unexpectedly to {updated.status!r}. "
        "update_comment with only body= should not touch status."
    )


def test_update_comment_both_fields(store: SqlAlchemyCommentStore) -> None:
    """``update_comment`` with both status= and body= updates both."""
    comment = store.add(
        conversation_id="conv_upd_both",
        path="x.py",
        body="Before",
        start_index=0,
        end_index=6,
    )

    updated = store.update_comment(comment.id, "conv_upd_both", status="addressed", body="After")

    assert updated is not None
    assert updated.status == "addressed"
    assert updated.body == "After"


def test_update_comment_returns_none_for_missing(store: SqlAlchemyCommentStore) -> None:
    """``update_comment`` returns ``None`` when the comment id does not exist."""
    result = store.update_comment("nonexistent-uuid-xyz", "conv_5c6d7e", status="addressed")

    # Must return None, not raise, for an unknown id.
    assert result is None, f"Expected None for an unknown comment id, got {result!r}"


def test_update_comment_wrong_conversation_is_noop(store: SqlAlchemyCommentStore) -> None:
    """``update_comment`` scoped to another conversation does not mutate the comment.

    Multi-user isolation: knowing a comment id from conversation A must not
    let a caller update it via conversation B. The call returns ``None`` and
    the stored status is left unchanged.
    """
    comment = store.add(
        conversation_id="conv_1a2b3c",
        path="api.py",
        body="Original",
        start_index=0,
        end_index=8,
    )

    result = store.update_comment(comment.id, "conv_4d5e6f", status="addressed")
    assert result is None, (
        f"Expected None updating a comment owned by another conversation, got {result!r}. "
        "update_comment must scope by conversation_id."
    )

    # The comment must still be draft when read by its real owner.
    owned = store.get(comment.id, "conv_1a2b3c")
    assert owned is not None and owned.status == "draft", (
        "A cross-conversation update_comment must not have mutated the comment."
    )


# ── delete ────────────────────────────────────────────────────────────────────


def test_delete_returns_comment_and_removes_it(store: SqlAlchemyCommentStore) -> None:
    """``delete`` returns the deleted Comment and removes it from the store."""
    comment = store.add(
        conversation_id="conv_del",
        path="delete_me.py",
        body="To be deleted",
        start_index=0,
        end_index=13,
    )

    deleted = store.delete(comment.id, "conv_del")

    # The deleted entity must be returned with correct fields.
    assert deleted is not None, "delete() must return the deleted Comment, not None"
    assert deleted.id == comment.id
    assert deleted.body == "To be deleted"

    # The comment must no longer appear in listings.
    remaining = store.list_for_conversation("conv_del")
    assert remaining == [], (
        f"Comment still visible after delete: {remaining}. "
        "The DELETE statement did not execute or did not commit."
    )


def test_delete_returns_none_for_missing(store: SqlAlchemyCommentStore) -> None:
    """``delete`` returns ``None`` when the comment id does not exist."""
    result = store.delete("no-such-comment-id", "conv_2b3c4d")

    assert result is None, f"Expected None for an unknown comment id, got {result!r}"


def test_delete_wrong_conversation_is_noop(store: SqlAlchemyCommentStore) -> None:
    """``delete`` scoped to another conversation does not remove the comment.

    Multi-user isolation: a comment created in conversation A must not be
    deletable via conversation B, even with the exact comment id. The call
    returns ``None`` and the comment remains readable by its real owner.
    """
    comment = store.add(
        conversation_id="conv_7a8b9c",
        path="keep.py",
        body="Must survive a cross-conversation delete",
        start_index=0,
        end_index=4,
    )

    result = store.delete(comment.id, "conv_0d1e2f")
    assert result is None, (
        f"Expected None deleting a comment owned by another conversation, got {result!r}. "
        "delete must scope by conversation_id."
    )

    # The comment must still exist for its real owner.
    owned = store.get(comment.id, "conv_7a8b9c")
    assert owned is not None and owned.id == comment.id, (
        "A cross-conversation delete must not have removed the comment."
    )


def test_delete_does_not_affect_other_comments(store: SqlAlchemyCommentStore) -> None:
    """Deleting one comment leaves other comments untouched."""
    c1 = store.add(
        conversation_id="conv_del_iso",
        path="f.py",
        body="Will survive",
        start_index=0,
        end_index=12,
    )
    c2 = store.add(
        conversation_id="conv_del_iso",
        path="f.py",
        body="Will be deleted",
        start_index=50,
        end_index=65,
    )

    store.delete(c2.id, "conv_del_iso")

    remaining = store.list_for_conversation("conv_del_iso")
    # Only c1 must remain.
    assert len(remaining) == 1, (
        f"Expected 1 remaining comment after deleting c2, got {len(remaining)}"
    )
    assert remaining[0].id == c1.id
    assert remaining[0].body == "Will survive"


# ── remove_conversation ───────────────────────────────────────────────────────


def test_remove_conversation_deletes_all_comments(store: SqlAlchemyCommentStore) -> None:
    """``remove_conversation`` removes every comment for the given conversation."""
    store.add(
        conversation_id="conv_rm",
        path="a.py",
        body="First",
        start_index=0,
        end_index=5,
    )
    store.add(
        conversation_id="conv_rm",
        path="b.py",
        body="Second",
        start_index=0,
        end_index=6,
    )

    store.remove_conversation("conv_rm")

    remaining = store.list_for_conversation("conv_rm")
    assert remaining == [], (
        f"Expected [] after remove_conversation, got {remaining}. "
        "remove_conversation did not delete all comments."
    )


def test_remove_conversation_does_not_affect_other_conversations(
    store: SqlAlchemyCommentStore,
) -> None:
    """``remove_conversation`` only removes comments for the specified conversation."""
    store.add(
        conversation_id="conv_keep",
        path="safe.py",
        body="Survives",
        start_index=0,
        end_index=8,
    )
    store.add(
        conversation_id="conv_gone",
        path="gone.py",
        body="Removed",
        start_index=0,
        end_index=7,
    )

    store.remove_conversation("conv_gone")

    kept = store.list_for_conversation("conv_keep")
    # conv_keep must still have its comment after conv_gone is purged.
    assert len(kept) == 1, (
        f"Expected conv_keep to still have 1 comment, got {len(kept)}. "
        "remove_conversation leaked into another conversation."
    )
    assert kept[0].body == "Survives"


def test_remove_conversation_is_noop_for_unknown_conversation(
    store: SqlAlchemyCommentStore,
) -> None:
    """``remove_conversation`` does not raise when no comments exist for the id."""
    # Should not raise — idempotent delete.
    store.remove_conversation("conv_nobody_here")


# ── updated_at & get_comments_fingerprints ──────────────────────────────────────


# One second in microseconds — updated_at is stored in epoch-µs while
# created_at stays epoch-seconds, so expectations scale by this factor.
_US = 1_000_000


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Deterministic write clock for the store under test.

    Patches the ``now_epoch_us`` reference the store module uses for
    all comment timestamps (``created_at`` is derived from the same
    read) so tests can advance time explicitly instead of sleeping.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: Mutable ``{"now": <epoch seconds>}`` dict; assign
        ``clock["now"]`` to move time forward.
    """
    state = {"now": 1_000}
    monkeypatch.setattr(
        "omnigent.stores.comment_store.sqlalchemy_store.now_epoch_us",
        lambda: state["now"] * _US,
    )
    return state


def test_add_sets_updated_at_equal_to_created_at(
    store: SqlAlchemyCommentStore, clock: dict[str, int]
) -> None:
    """A freshly created comment's ``updated_at`` equals ``created_at``."""
    comment = store.add(
        conversation_id="conv_fp",
        path="src/app.py",
        body="first",
        start_index=0,
        end_index=5,
    )
    # A never-edited comment's updated_at is its creation instant,
    # expressed in microseconds (created_at stays in seconds).
    assert comment.created_at == 1_000
    assert comment.updated_at == 1_000 * _US


def test_update_comment_bumps_updated_at_and_persists(
    store: SqlAlchemyCommentStore, clock: dict[str, int]
) -> None:
    """A body/status mutation moves ``updated_at`` to the write time."""
    comment = store.add(
        conversation_id="conv_fp",
        path="src/app.py",
        body="first",
        start_index=0,
        end_index=5,
    )
    clock["now"] = 2_000
    updated = store.update_comment(comment.id, "conv_fp", status="addressed")

    assert updated is not None
    # updated_at must move to the mutation time while created_at is
    # untouched — if it stayed at the creation instant the session
    # fingerprint would never change on edits and clients would miss
    # the mutation.
    assert updated.updated_at == 2_000 * _US
    assert updated.created_at == 1_000
    # The bump must be persisted, not just present on the returned entity.
    fetched = store.get(comment.id, "conv_fp")
    assert fetched is not None
    assert fetched.updated_at == 2_000 * _US


def test_update_comment_with_no_fields_does_not_bump_updated_at(
    store: SqlAlchemyCommentStore, clock: dict[str, int]
) -> None:
    """A no-op update (both fields ``None``) leaves ``updated_at`` alone."""
    comment = store.add(
        conversation_id="conv_fp",
        path="src/app.py",
        body="first",
        start_index=0,
        end_index=5,
    )
    clock["now"] = 2_000
    updated = store.update_comment(comment.id, "conv_fp")

    assert updated is not None
    # Nothing changed, so the fingerprint input must not move — a bump
    # here would push spurious "comments changed" frames to clients.
    assert updated.updated_at == 1_000 * _US


def test_get_comments_fingerprints_empty_input_returns_empty(
    store: SqlAlchemyCommentStore,
) -> None:
    """An empty id batch short-circuits to an empty map (no query)."""
    assert store.get_comments_fingerprints([]) == {}


def test_get_comments_fingerprints_omits_conversations_without_comments(
    store: SqlAlchemyCommentStore, clock: dict[str, int]
) -> None:
    """Conversations with no comments are absent from the result map."""
    store.add(
        conversation_id="conv_with",
        path="src/app.py",
        body="x",
        start_index=0,
        end_index=1,
    )
    result = store.get_comments_fingerprints(["conv_with", "conv_without"])
    # Absent (not a zero-count entry) is the contract — the route maps
    # absence to the comments_count=0 / comments_updated_at=None shape.
    assert set(result) == {"conv_with"}


def test_get_comments_fingerprints_batches_counts_and_max_updated_at(
    store: SqlAlchemyCommentStore, clock: dict[str, int]
) -> None:
    """One batched call returns exact per-conversation count + max."""
    store.add(conversation_id="conv_a", path="a.py", body="a1", start_index=0, end_index=1)
    clock["now"] = 1_500
    store.add(conversation_id="conv_a", path="a.py", body="a2", start_index=2, end_index=3)
    clock["now"] = 3_000
    store.add(conversation_id="conv_b", path="b.py", body="b1", start_index=0, end_index=1)

    result = store.get_comments_fingerprints(["conv_a", "conv_b"])

    # Exact values prove the aggregate is grouped per conversation: a
    # cross-conversation max would report 3_000 for conv_a.
    assert result["conv_a"] == CommentsFingerprint(count=2, last_updated_at=1_500 * _US)
    assert result["conv_b"] == CommentsFingerprint(count=1, last_updated_at=3_000 * _US)


def test_get_comments_fingerprints_reflects_edit(
    store: SqlAlchemyCommentStore, clock: dict[str, int]
) -> None:
    """An in-place mutation moves ``last_updated_at`` with count unchanged."""
    comment = store.add(
        conversation_id="conv_fp", path="a.py", body="x", start_index=0, end_index=1
    )
    before = store.get_comments_fingerprints(["conv_fp"])["conv_fp"]
    clock["now"] = 2_000
    store.update_comment(comment.id, "conv_fp", status="addressed")
    after = store.get_comments_fingerprints(["conv_fp"])["conv_fp"]

    # The edit is invisible to the count, so the timestamp alone must
    # carry it — this is the reason the updated_at column exists.
    assert before == CommentsFingerprint(count=1, last_updated_at=1_000 * _US)
    assert after == CommentsFingerprint(count=1, last_updated_at=2_000 * _US)


def test_get_comments_fingerprints_reflects_delete_of_older_comment(
    store: SqlAlchemyCommentStore, clock: dict[str, int]
) -> None:
    """Deleting a non-newest comment changes the count, not the max."""
    older = store.add(
        conversation_id="conv_fp", path="a.py", body="old", start_index=0, end_index=1
    )
    clock["now"] = 2_000
    store.add(conversation_id="conv_fp", path="a.py", body="new", start_index=2, end_index=3)

    store.delete(older.id, "conv_fp")
    after = store.get_comments_fingerprints(["conv_fp"])["conv_fp"]

    # max(updated_at) is blind to this delete (the surviving comment is
    # the newest) — the count drop is what makes the fingerprint move.
    # This is the reason the fingerprint carries a count at all.
    assert after == CommentsFingerprint(count=1, last_updated_at=2_000 * _US)
