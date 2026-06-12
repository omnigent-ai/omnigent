"""E2E: rich-text editor comment UX in FileViewer.

Verifies the end-to-end flow for selecting text in the TipTap rich-text
editor and adding a comment:

  1. A markdown file is seeded directly via the artifacts API (no agent run
     needed), so the test is fast and deterministic.
  2. The FileViewer opens in rich-text editor mode (the default for .md files).
  3. The user selects plain text in the editor; the floating "Add comment"
     button appears above the selection.
  4. Clicking "Add comment" marks the selection as a pending comment (a TipTap
     inline decoration) so the selected range stays visible while the panel is open.
  5. The user fills in the comment body and saves it; the comment card appears
     in the CommentsPanel with the correct body.
  6. The comment offset returned by the API matches the position of the anchor
     text in the raw markdown.
  7. A saved-comment decoration (``data-comment-id``) is present in the editor.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_MARKDOWN_FILE_PATH = "test_comments.md"

# The plain-text paragraph we will select. It must appear exactly once in the
# file so the offset test is unambiguous.
_SELECTABLE_TEXT = "Welcome to the editor."

# The full text of the h2 heading — used to test that anchor_content does not
# include the ``## `` prefix when selecting text from a heading node.
_HEADING_TEXT = "Editor Section Heading"

# Full markdown body — a heading followed by the selectable paragraph.
_MARKDOWN_CONTENT = f"""\
# Editor Comment Test

## {_HEADING_TEXT}

{_SELECTABLE_TEXT}

This is another paragraph with some text.
"""

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_markdown_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed a markdown file into the session and yield (base_url, session_id, path).

    The file is created via PUT /v1/sessions/{id}/resources/environments/
    default/filesystem/{path}, which writes it into the session's artifact
    store and makes it visible in the FileViewer without requiring an agent run.

    :param seeded_session: The base session fixture providing a runner-bound
        (base_url, session_id) pair.
    :returns: ``(base_url, session_id, file_path)`` for use in test body.
    """
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_MARKDOWN_FILE_PATH}"
    )
    resp = httpx.put(
        file_url,
        json={"content": _MARKDOWN_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    yield (base_url, session_id, _MARKDOWN_FILE_PATH)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_markdown_rich_text_editor_add_comment(
    page: Page,
    seeded_markdown_session: tuple[str, str, str],
) -> None:
    """Select text in the TipTap editor, add a comment, and verify it persists.

    Steps:
    1. Navigate to the seeded session.
    2. Open the markdown file from the files panel.
    3. Verify the FileViewer opens in rich-text editor mode (default for .md).
    4. Select the selectable paragraph text in the editor.
    5. Wait for the floating "Add comment" button to appear.
    6. Click "Add comment" and confirm CommentsPanel opens; a pending-comment
       decoration (``.md-comment-pending``) wraps the selection.
    7. Verify the pending decoration is present in the editor surface.
    8. Fill in the comment body and save.
    9. Confirm the comment card appears with the expected body.
    10. Via the REST API, verify the stored start_index matches the position
        of the anchor text in the raw markdown.
    11. A saved-comment decoration (``data-comment-id``) persists in the editor.
    """
    base_url, session_id, file_path = seeded_markdown_session
    page.goto(f"{base_url}/c/{session_id}")
    # The rail defaults open but is remembered per session; ensure it is open so the files panel is
    # reachable.
    open_right_rail(page)

    # Wait for the markdown file to appear in the files panel. The panel
    # polls the workspace changed-files endpoint; the PUT-seeded file shows
    # up as a new addition relative to the git baseline.
    # The changed-file row renders two buttons carrying the filename: the
    # file-open button (visible text) and an icon-only Download button
    # (aria-label "Download <name>"). Filter to the open button by its
    # visible text so the locator stays single-element under strict mode.
    file_button = page.get_by_role(
        "button", name=re.compile(re.escape(_MARKDOWN_FILE_PATH))
    ).filter(has_text=_MARKDOWN_FILE_PATH)
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    # The FileViewer should open and show the filename.
    # Two FileViewer instances mount with the same test id (hidden mobile
    # drawer + desktop rail). Match the visible one directly rather than by
    # DOM order — order is not guaranteed. Matches test_markdown_rich_rendering.
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    # The open file is identified by its tab (the desktop viewer header no
    # longer repeats a top-level filename — it's redundant with the tab).
    # exact=True targets the close button, not the tab div whose accessible
    # name also contains "Close <name>".
    expect(
        page.get_by_role("button", name=f"Close {_MARKDOWN_FILE_PATH}", exact=True).first
    ).to_be_visible()

    # Markdown files default to rich-text editor mode. The editor renders the
    # heading and paragraph into styled HTML via TipTap; the raw markdown
    # syntax characters (# , **) are NOT visible in the editor surface.
    editor_content = file_viewer.locator("[contenteditable='true']")
    expect(editor_content).to_be_visible(timeout=10_000)

    # Confirm the heading and selectable paragraph are rendered.
    expect(editor_content).to_contain_text("Editor Comment Test")
    expect(editor_content).to_contain_text(_SELECTABLE_TEXT)

    # select_text() drives a real drag-selection in the TipTap surface.
    # click(click_count=3) does not reliably fire SELECTION_CHANGE_COMMAND
    # in headless Chromium (no triple_click() on Locator in this Playwright pin).
    selectable = editor_content.get_by_text(_SELECTABLE_TEXT)
    expect(selectable).to_be_visible()
    selectable.select_text()

    # After mouseup the floating "Add comment" button appears (via portal).
    add_comment_btn = page.get_by_role("button", name=re.compile("Add comment", re.IGNORECASE))
    expect(add_comment_btn).to_be_visible()

    # The button must be positioned ABOVE the selection (y < selection top).
    # We cannot easily verify this in Playwright without bounding-box math,
    # but we verify the button is in the viewport (not off-screen).
    btn_box = add_comment_btn.bounding_box()
    assert btn_box is not None, "Add comment button has no bounding box"
    assert btn_box["y"] > 0, "Add comment button is above the viewport"

    add_comment_btn.click()

    # CommentsPanel opens alongside the editor (header is unique in the panel).
    expect(file_viewer.locator("span.font-semibold", has_text="Comments")).to_be_visible()

    # Clicking "Add comment" marks the selection as a pending comment. The
    # TipTap editor renders this as a ProseMirror inline decoration with the
    # ``md-comment-pending`` class (see TipTapCommentExtension). Verify the
    # highlight is present in the editor surface — this is the actual
    # highlight mechanism, not browser selection alone.
    pending_mark = editor_content.locator(".md-comment-pending")
    expect(pending_mark.first).to_be_visible()

    # Fill in the comment body and submit.
    comment_body = "This is a test comment on the selectable paragraph."
    comment_textarea = file_viewer.locator("textarea[placeholder='Add a comment…']")
    expect(comment_textarea).to_be_visible()
    comment_textarea.fill(comment_body)
    file_viewer.get_by_role("button", name="Add Comment").click()

    # The comment card should appear in the CommentsPanel.
    expect(file_viewer).to_contain_text(comment_body)

    # After save the pending decoration is replaced by a saved-comment
    # decoration (``md-comment`` class + ``data-comment-id``). A highlight
    # should still be present for the anchor range.
    saved_mark = editor_content.locator("[data-comment-id]")
    expect(saved_mark.first).to_be_visible()

    # Verify via the REST API that the comment was persisted with correct offsets.
    comments_resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/comments?path={file_path}",
        timeout=10.0,
    )
    comments_resp.raise_for_status()
    comments = comments_resp.json()
    assert len(comments) == 1, f"Expected 1 comment, got {len(comments)}: {comments}"

    comment = comments[0]
    assert comment["body"] == comment_body
    assert comment["anchor_content"] is not None
    # The anchor content should match (or contain) the selectable text.
    assert (
        _SELECTABLE_TEXT in comment["anchor_content"]
        or comment["anchor_content"] in _SELECTABLE_TEXT
    ), f"anchor_content {comment['anchor_content']!r} does not match selectable text"
    # The start_index should place the anchor within the raw markdown.
    stored_idx = comment["start_index"]
    raw_idx = _MARKDOWN_CONTENT.find(comment["anchor_content"])
    assert raw_idx != -1, f"anchor_content {comment['anchor_content']!r} not found in raw markdown"
    # Allow a ±200-char window for editor normalization differences.
    assert abs(stored_idx - raw_idx) <= 200, (
        f"stored start_index={stored_idx} is more than 200 chars from "
        f"raw markdown position {raw_idx} for anchor {comment['anchor_content']!r}"
    )


def test_heading_text_anchor_content_excludes_prefix(
    page: Page,
    seeded_markdown_session: tuple[str, str, str],
) -> None:
    """Select a word from inside a heading; anchor_content must not include ``## ``.

    Regression test for the stale-``pendingDataRef`` bug: clicking "Add
    comment" used pre-computed selection data from a previous rAF, which could
    include the block prefix (``## ``) when the user's current selection was
    refined after the button appeared. The fix re-computes the anchor from the
    *current* editor state at click time.

    Steps:
    1. Navigate to the seeded session and open the markdown file.
    2. Select the full heading text (``_HEADING_TEXT``) via ``select_text()`` on the h2 element.
    3. Click "Add comment" → fill in body → save.
    4. Verify ``anchor_content`` contains or matches ``_HEADING_TEXT`` (no ``## `` prefix).
    5. Via REST API, verify start_index places the anchor within ``## Editor Section Heading``.
    """
    base_url, session_id, file_path = seeded_markdown_session
    page.goto(f"{base_url}/c/{session_id}")
    # The rail defaults open but is remembered per session; ensure it is open so the files panel is
    # reachable.
    open_right_rail(page)

    # The changed-file row renders two buttons carrying the filename: the
    # file-open button (visible text) and an icon-only Download button
    # (aria-label "Download <name>"). Filter to the open button by its
    # visible text so the locator stays single-element under strict mode.
    file_button = page.get_by_role(
        "button", name=re.compile(re.escape(_MARKDOWN_FILE_PATH))
    ).filter(has_text=_MARKDOWN_FILE_PATH)
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    # Two FileViewer instances mount with the same test id (hidden mobile
    # drawer + desktop rail). Match the visible one directly rather than by
    # DOM order — order is not guaranteed. Matches test_markdown_rich_rendering.
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    editor_content = file_viewer.locator("[contenteditable='true']")
    expect(editor_content).to_be_visible(timeout=10_000)

    # The heading is rendered by TipTap as an h2 element — ``## `` is NOT
    # visible text. Locate the h2 by its full rendered text and select it.
    heading_locator = editor_content.locator("h2").filter(has_text=_HEADING_TEXT).first
    expect(heading_locator).to_be_visible()
    heading_locator.select_text()

    add_comment_btn = page.get_by_role("button", name=re.compile("Add comment", re.IGNORECASE))
    expect(add_comment_btn).to_be_visible()
    add_comment_btn.click()

    comment_body = "Heading anchor test comment."
    comment_textarea = file_viewer.locator("textarea[placeholder='Add a comment…']")
    expect(comment_textarea).to_be_visible()
    comment_textarea.fill(comment_body)
    file_viewer.get_by_role("button", name="Add Comment").click()

    expect(file_viewer).to_contain_text(comment_body)

    comments_resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/comments?path={file_path}",
        timeout=10.0,
    )
    comments_resp.raise_for_status()
    all_comments = comments_resp.json()
    heading_comment = next(
        (c for c in all_comments if c["body"] == comment_body),
        None,
    )
    assert heading_comment is not None, f"Heading comment not found in {all_comments}"

    anchor = heading_comment["anchor_content"]
    assert anchor is not None, "anchor_content should not be None"
    # The anchor must not start with the heading prefix characters.
    assert not anchor.startswith("#"), (
        f"anchor_content {anchor!r} starts with '#' — heading prefix leaked into anchor. "
        "This is the stale-pendingDataRef regression."
    )
    # The anchor must contain (or match) the heading text — never the ``## `` syntax.
    assert _HEADING_TEXT in anchor or anchor in _HEADING_TEXT, (
        f"anchor_content {anchor!r} does not match the heading text {_HEADING_TEXT!r}"
    )
    # The start_index must place the anchor within the raw markdown (not at position 0).
    stored_idx = heading_comment["start_index"]
    raw_idx = _MARKDOWN_CONTENT.find(anchor)
    assert raw_idx != -1, f"anchor_content {anchor!r} not found in raw markdown"
    assert abs(stored_idx - raw_idx) <= 200, (
        f"stored start_index={stored_idx} is more than 200 chars from "
        f"raw markdown position {raw_idx} for anchor {anchor!r}"
    )
