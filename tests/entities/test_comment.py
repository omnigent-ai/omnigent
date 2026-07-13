"""Tests for comment entity dataclasses."""

from __future__ import annotations

from omnigent.entities.comment import Comment, CommentsFingerprint

# ── Comment ───────────────────────────────────────────


def test_comment_construction() -> None:
    comment = Comment(
        id="a1b2c3d4-0000-0000-0000-000000000000",
        conversation_id="conv_abc123",
        path="src/App.tsx",
        start_index=100,
        end_index=200,
        body="This needs a null check.",
        status="draft",
        created_at=1700000000,
        updated_at=1700000000_000000,
    )
    assert comment.id == "a1b2c3d4-0000-0000-0000-000000000000"
    assert comment.conversation_id == "conv_abc123"
    assert comment.path == "src/App.tsx"
    assert comment.start_index == 100
    assert comment.end_index == 200
    assert comment.body == "This needs a null check."
    assert comment.status == "draft"
    assert comment.anchor_content is None
    assert comment.created_by is None


def test_comment_with_optional_fields() -> None:
    comment = Comment(
        id="c1",
        conversation_id="conv_1",
        path="main.py",
        start_index=0,
        end_index=10,
        body="Fix this",
        status="addressed",
        created_at=1700000000,
        updated_at=1700000001_000000,
        anchor_content="old code here",
        created_by="alice@example.com",
    )
    assert comment.anchor_content == "old code here"
    assert comment.created_by == "alice@example.com"
    assert comment.status == "addressed"


def test_comment_is_mutable() -> None:
    comment = Comment(
        id="c1",
        conversation_id="conv_1",
        path="x.py",
        start_index=0,
        end_index=5,
        body="draft body",
        status="draft",
        created_at=1,
        updated_at=1,
    )
    comment.status = "addressed"
    comment.body = "updated body"
    assert comment.status == "addressed"
    assert comment.body == "updated body"


# ── CommentsFingerprint ───────────────────────────────


def test_comments_fingerprint() -> None:
    fp = CommentsFingerprint(count=5, last_updated_at=1700000001_000000)
    assert fp.count == 5
    assert fp.last_updated_at == 1700000001_000000


def test_comments_fingerprint_empty() -> None:
    """A conversation with zero comments still has a valid fingerprint."""
    fp = CommentsFingerprint(count=0, last_updated_at=0)
    assert fp.count == 0
