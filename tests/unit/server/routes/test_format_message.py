"""Tests for :func:`omnigent.server.routes.comments._format_message`.

``_format_message`` is a pure function with non-trivial grouping and
sorting logic — it groups comments by file path (alphabetical order)
and sorts within each group by ``start_index`` ascending.  Each bullet
shows the ``anchor_content`` snippet (when available) plus the
``start_index``–``end_index`` character range.  These tests cover the
invariants directly so regressions in the sort or group logic surface
without needing a running HTTP server.
"""

from __future__ import annotations

from omnigent.entities import Comment
from omnigent.server.routes.comments import _format_message


def _make_comment(
    path: str,
    start_index: int,
    body: str,
    *,
    end_index: int = 0,
    anchor_content: str | None = None,
    conversation_id: str = "conv_test",
    status: str = "draft",
) -> Comment:
    """Build a :class:`Comment` for use in formatting tests.

    All fields other than ``path``, ``start_index``, and ``body`` default to
    sensible test values.

    :param path: File path for the comment.
    :param start_index: 0-based absolute character offset where the anchor begins.
    :param body: Comment text.
    :param end_index: 0-based absolute character offset where the anchor ends
        (default ``0``).
    :param anchor_content: Plain-text snapshot of the selected range (default
        ``None``).
    :param conversation_id: Owning conversation (default ``"conv_test"``).
    :param status: Comment status (default ``"draft"``).
    :returns: A :class:`Comment` with a fixed id and created_at.
    """
    return Comment(
        id="test-id",
        conversation_id=conversation_id,
        path=path,
        start_index=start_index,
        end_index=end_index if end_index >= start_index else start_index,
        body=body,
        status=status,
        created_at=1_000_000,
        updated_at=1_000_000,
        anchor_content=anchor_content,
    )


# ── header ────────────────────────────────────────────────────────────────────


def test_format_message_always_starts_with_header() -> None:
    """The formatted message always starts with the "Please address" header line.

    This header is what the e2e test ``test_comments_send_to_agent_with_empty_ids``
    asserts; it must be present even when no comments are provided.
    """
    result = _format_message([])

    assert result.startswith("Please address the following review comments."), (
        f"Expected header as first line, got: {result!r}"
    )


def test_format_message_empty_list_returns_header_only() -> None:
    """An empty comment list produces only the header — no trailing blank lines."""
    result = _format_message([])

    assert result == "Please address the following review comments.", (
        f"Expected single header line for empty input, got: {result!r}"
    )


# ── single file ───────────────────────────────────────────────────────────────


def test_format_message_single_comment_includes_path_anchor_and_body() -> None:
    """A single comment produces a block with the path, anchor snippet, and body."""
    comment = _make_comment(
        path="src/app.py",
        start_index=42,
        body="Add null check",
        anchor_content="some_function()",
    )

    result = _format_message([comment])

    assert "File: src/app.py" in result, f"Expected 'File: src/app.py' in output, got: {result!r}"
    assert "some_function()" in result, f"Expected anchor_content in output, got: {result!r}"
    assert "Add null check" in result, f"Expected comment body in output, got: {result!r}"


def test_format_message_falls_back_to_offset_when_no_anchor_content() -> None:
    """When anchor_content is None the location shows 'offset N'."""
    comment = _make_comment(path="src/app.py", start_index=100, body="Check this")

    result = _format_message([comment])

    assert "offset 100" in result, (
        f"Expected 'offset 100' fallback when anchor_content is None, got: {result!r}"
    )
    assert "Check this" in result


def test_format_message_single_file_multiple_comments_sorted_by_start_index() -> None:
    """Comments on the same file are emitted in ascending start_index order.

    start_index 5 must appear before start_index 200 even if the input list
    has start_index 200 first.
    """
    c200 = _make_comment(path="utils.py", start_index=200, body="High offset")
    c5 = _make_comment(path="utils.py", start_index=5, body="Low offset")

    result = _format_message([c200, c5])

    pos_low = result.index("Low offset")
    pos_high = result.index("High offset")

    assert pos_low < pos_high, (
        f"Expected start_index 5 comment before start_index 200 comment, "
        f"but 'Low offset' appears at pos {pos_low} and 'High offset' at {pos_high}. "
        "Comments are not sorted by start_index within a file."
    )


# ── multiple files ────────────────────────────────────────────────────────────


def test_format_message_multiple_files_sorted_alphabetically() -> None:
    """Files are emitted in alphabetical order regardless of input order.

    ``zoo.py`` must appear after ``alpha.py`` even when it is first in the list.
    """
    c_zoo = _make_comment(path="zoo.py", start_index=1, body="Zoo comment")
    c_alpha = _make_comment(path="alpha.py", start_index=1, body="Alpha comment")

    result = _format_message([c_zoo, c_alpha])

    pos_alpha = result.index("alpha.py")
    pos_zoo = result.index("zoo.py")

    assert pos_alpha < pos_zoo, (
        f"Expected 'alpha.py' before 'zoo.py' (alphabetical order), "
        f"but alpha.py appears at pos {pos_alpha} and zoo.py at {pos_zoo}. "
        "Files are not sorted alphabetically."
    )


def test_format_message_each_file_gets_its_own_section() -> None:
    """Every distinct file in the input gets a separate 'File:' section."""
    c1 = _make_comment(path="a.py", start_index=1, body="Comment on a")
    c2 = _make_comment(path="b.py", start_index=1, body="Comment on b")

    result = _format_message([c1, c2])

    assert "File: a.py" in result, "Missing 'File: a.py' section header"
    assert "File: b.py" in result, "Missing 'File: b.py' section header"
    assert "Comment on a" in result
    assert "Comment on b" in result


def test_format_message_comments_not_mixed_across_files() -> None:
    """A comment on file A must not appear under the file-B section."""
    c_a = _make_comment(path="a.py", start_index=3, body="Only in A")
    c_b = _make_comment(path="b.py", start_index=7, body="Only in B")

    result = _format_message([c_a, c_b])
    lines = result.splitlines()

    idx_file_a = next(i for i, ln in enumerate(lines) if "File: a.py" in ln)
    idx_file_b = next(i for i, ln in enumerate(lines) if "File: b.py" in ln)
    idx_body_a = next(i for i, ln in enumerate(lines) if "Only in A" in ln)
    idx_body_b = next(i for i, ln in enumerate(lines) if "Only in B" in ln)

    assert idx_file_a < idx_body_a < idx_file_b, (
        f"'Only in A' should appear between 'File: a.py' and 'File: b.py', "
        f"but line indices are: file_a={idx_file_a}, body_a={idx_body_a}, file_b={idx_file_b}"
    )
    assert idx_file_b < idx_body_b, (
        f"'Only in B' should appear after 'File: b.py', "
        f"but file_b={idx_file_b}, body_b={idx_body_b}"
    )


# ── bullet format ─────────────────────────────────────────────────────────────


def test_format_message_uses_anchor_content_as_bullet_prefix() -> None:
    """When anchor_content is set it appears quoted before the offset range."""
    comment = _make_comment(
        path="f.py",
        start_index=0,
        end_index=8,
        body="Fix the import",
        anchor_content="import os",
    )

    result = _format_message([comment])

    assert '• "import os" (offset 0–8): Fix the import' in result, (
        f"Expected '• \"import os\" (offset 0–8): Fix the import' in output, got: {result!r}"
    )


def test_format_message_anchor_content_is_stripped() -> None:
    """Whitespace around anchor_content is stripped in the bullet prefix."""
    comment = _make_comment(
        path="f.py",
        start_index=0,
        body="Check indentation",
        anchor_content="  indented line  ",
    )

    result = _format_message([comment])

    assert '"indented line"' in result, (
        f"Expected stripped anchor_content in bullet, got: {result!r}"
    )
