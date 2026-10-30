"""Tests for :class:`omnigent.server.routes.comments.AddCommentRequest` validation.

``AddCommentRequest`` has a ``model_validator`` that rejects semantically
invalid range field combinations at the HTTP boundary before they reach the
store.  These tests cover each rejection branch and the valid happy-path so
that any relaxation or tightening of the validator surfaces immediately.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnigent.server.routes.comments import AddCommentRequest


def _valid_kwargs(**overrides: object) -> dict:
    """Return a dict of valid ``AddCommentRequest`` kwargs, with optional overrides.

    :param overrides: Field values to substitute in the base valid payload.
    :returns: Keyword-argument dict suitable for ``AddCommentRequest(**...)``.
    """
    base: dict = {
        "path": "src/app.py",
        "body": "Fix this",
        "start_index": 0,
        "end_index": 10,
    }
    base.update(overrides)
    return base


# ── happy path ────────────────────────────────────────────────────────────────


def test_add_comment_request_valid() -> None:
    """A comment with valid range fields constructs without error."""
    req = AddCommentRequest(**_valid_kwargs())

    assert req.start_index == 0
    assert req.end_index == 10


def test_add_comment_request_valid_zero_length_selection() -> None:
    """A zero-length selection (start_index == end_index) is valid (cursor position)."""
    req = AddCommentRequest(**_valid_kwargs(start_index=5, end_index=5))

    assert req.start_index == 5
    assert req.end_index == 5


def test_add_comment_request_valid_anchor_content_optional() -> None:
    """anchor_content defaults to None and can be supplied."""
    req_no_anchor = AddCommentRequest(**_valid_kwargs())
    assert req_no_anchor.anchor_content is None

    req_with_anchor = AddCommentRequest(**_valid_kwargs(anchor_content="selected text"))
    assert req_with_anchor.anchor_content == "selected text"


# ── start_index validation ────────────────────────────────────────────────────


@pytest.mark.parametrize("start_index", [-1, -100])
def test_add_comment_request_rejects_negative_start_index(start_index: int) -> None:
    """start_index must be >= 0; negative values are rejected.

    :param start_index: An invalid (negative) start_index value.
    """
    with pytest.raises(ValidationError, match="start_index must be >= 0"):
        AddCommentRequest(**_valid_kwargs(start_index=start_index, end_index=0))


# ── end_index validation ──────────────────────────────────────────────────────


def test_add_comment_request_rejects_end_index_before_start_index() -> None:
    """end_index must be >= start_index; a smaller end_index is rejected."""
    with pytest.raises(ValidationError, match="end_index must be >= start_index"):
        AddCommentRequest(**_valid_kwargs(start_index=10, end_index=5))


@pytest.mark.parametrize("side,line,start", [("LEFT", 9, 7), ("RIGHT", 1, None)])
def test_pr_comment_anchor(side: str, line: int, start: int | None) -> None:
    """PR coordinates survive validation independently of file offsets."""
    request = AddCommentRequest(
        **_valid_kwargs(
            review_anchor={
                "pr_url": "https://github.com/acme/app/pull/1",
                "side": side,
                "line": line,
                "start_line": start,
                "head_sha": "a" * 40,
                "base_sha": "b" * 40,
            }
        )
    )
    assert request.review_anchor is not None
    assert request.review_anchor.side == side
    assert request.review_anchor.line == line


@pytest.mark.parametrize(
    "override",
    [
        {"side": "OTHER"},
        {"line": 0},
        {"start_line": 10},
        {"head_sha": ""},
        {"base_sha": "not-a-sha"},
        {"pr_url": "x" * 4097},
    ],
)
def test_invalid_pr_comment_anchor(override: dict) -> None:
    """Reject unusable coordinates and oversized metadata before persistence."""
    anchor = {
        "pr_url": "https://github.com/acme/app/pull/1",
        "side": "LEFT",
        "line": 9,
        "start_line": 7,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
    }
    with pytest.raises(ValidationError):
        AddCommentRequest(**_valid_kwargs(review_anchor={**anchor, **override}))
