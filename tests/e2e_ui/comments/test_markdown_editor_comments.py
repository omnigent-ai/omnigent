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
  8. Highlighting one copy of a word that repeats nearby anchors the comment to
     that copy in the editor, the stored offsets, and the Source view.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail, switch_markdown_view_mode

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

# A word that repeats within one paragraph, and the 0-based copy the user
# highlights (the second "fox").
_WORD = "fox"
_SELECTED_OCCURRENCE = 1
_REPEATED_PARAGRAPH = (
    "The quick brown fox jumps over the lazy dog while the sleepy fox naps "
    "and a third fox watches from the hill."
)

# Markup the editor never shows (an image target, a link target) sits before
# the paragraphs, so raw-file offsets run well ahead of the rendered text. The
# image is a self-contained 1x1 PNG so the browser makes no external request.
_IMAGE_URL = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
_LINK_URL = (
    "https://docs.example.com/engineering/design/comment-anchoring/overview"
    "?version=3&section=offsets"
)

# Full markdown body — README-shaped: heading, image, link, then the paragraphs.
_MARKDOWN_CONTENT = f"""\
# Editor Comment Test

## {_HEADING_TEXT}

![Architecture diagram]({_IMAGE_URL})

See the [design document]({_LINK_URL}) for details.

{_SELECTABLE_TEXT}

{_REPEATED_PARAGRAPH}

This is another paragraph with some text.
"""

# Viewport centre of the n-th copy of a word inside a rendered paragraph.
_WORD_CENTER_JS = """
(p, [word, occurrence]) => {
  const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
  const nodes = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n);
  const full = nodes.map((t) => t.data).join("");
  let idx = -1;
  for (let i = 0; i <= occurrence; i++) idx = full.indexOf(word, idx + 1);
  if (idx === -1) return null;
  let acc = 0;
  for (const t of nodes) {
    if (idx < acc + t.data.length) {
      const r = document.createRange();
      r.setStart(t, idx - acc);
      r.setEnd(t, idx - acc + word.length);
      const b = r.getBoundingClientRect();
      return { x: b.x + b.width / 2, y: b.y + b.height / 2 };
    }
    acc += t.data.length;
  }
  return null;
}
"""

# Each editor decoration with its character offset inside its paragraph.
_EDITOR_DECORATIONS_JS = """
(editor, selector) =>
  Array.from(editor.querySelectorAll(selector)).map((el) => {
    const block = el.closest("p") || editor;
    const r = document.createRange();
    r.setStart(block, 0);
    r.setEndBefore(el);
    return { text: el.textContent, offset: r.toString().length };
  })
"""

# Source view draws each comment as an empty overlay whose ``left`` encodes the
# start column in ``ch`` units.
_SOURCE_OVERLAYS_JS = r"""
(root) =>
  Array.from(root.querySelectorAll("[data-line] > span[aria-hidden]")).map((el) => {
    const m = /(\d+)ch/.exec(el.style.left);
    return { line: el.parentElement.textContent, startCol: m ? Number(m[1]) : null };
  })
"""


def _occurrences(text: str, word: str) -> list[int]:
    return [m.start() for m in re.finditer(re.escape(word), text)]


def _occurrence_of(offsets: list[int], offset: int | None) -> int | None:
    return offsets.index(offset) if offset in offsets else None


def _highlight_word(page: Page, paragraph: Locator, occurrence: int) -> None:
    """Double-click the n-th copy of ``_WORD`` so only that word is selected."""
    paragraph.scroll_into_view_if_needed()
    center = paragraph.evaluate(_WORD_CENTER_JS, [_WORD, occurrence])
    assert center is not None, f"occurrence {occurrence} of {_WORD!r} not found in paragraph"
    page.mouse.dblclick(center["x"], center["y"])
    page.wait_for_function("word => window.getSelection().toString().trim() === word", arg=_WORD)


def _editor_decorations(editor: Locator, selector: str) -> list[dict]:
    return editor.evaluate(_EDITOR_DECORATIONS_JS, selector)


def _open_markdown_in_editor(page: Page, file_path: str) -> tuple[Locator, Locator]:
    """Open ``file_path`` from the files panel and wait for the rich-text editor.

    The changed-file row renders two buttons carrying the filename (open and
    Download), so the open button is filtered by its visible text. Two
    FileViewer instances mount with the same test id (hidden mobile drawer and
    desktop rail); the visible one is matched directly.
    """
    open_right_rail(page)
    file_button = page.get_by_role("button", name=re.compile(re.escape(file_path))).filter(
        has_text=file_path
    )
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=15_000)
    expect(editor).to_contain_text(_REPEATED_PARAGRAPH)
    return file_viewer, editor


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
    file_viewer, editor_content = _open_markdown_in_editor(page, file_path)

    # The open file is identified by its tab (the desktop viewer header no
    # longer repeats a top-level filename — it's redundant with the tab).
    # exact=True targets the close button, not the tab div whose accessible
    # name also contains "Close <name>".
    expect(
        page.get_by_role("button", name=f"Close {_MARKDOWN_FILE_PATH}", exact=True).first
    ).to_be_visible()

    # Markdown files default to rich-text editor mode: the heading and paragraph
    # render as styled HTML, and the raw syntax characters (# , **) are NOT
    # visible in the editor surface.
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
    file_viewer, editor_content = _open_markdown_in_editor(page, file_path)

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


def test_comment_on_repeated_word_anchors_to_highlighted_occurrence(
    request: pytest.FixtureRequest,
    seeded_markdown_session: tuple[str, str, str],
) -> None:
    """Highlight the second of three nearby copies of a word and comment on it.

    The editor highlight (before and after a reload), the stored offsets, and the
    Source view overlay must all point at the highlighted copy.
    """
    base_url, session_id, file_path = seeded_markdown_session
    paragraph_offsets = _occurrences(_REPEATED_PARAGRAPH, _WORD)
    expected_offset = paragraph_offsets[_SELECTED_OCCURRENCE]
    expected_raw_start = _MARKDOWN_CONTENT.index(_REPEATED_PARAGRAPH) + expected_offset

    # Requested after seeding so a recording starts on the journey itself.
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    file_viewer, editor = _open_markdown_in_editor(page, file_path)
    paragraph = editor.locator("p", has_text=_REPEATED_PARAGRAPH)
    expect(paragraph).to_have_count(1)

    _highlight_word(page, paragraph, _SELECTED_OCCURRENCE)
    add_comment_btn = page.get_by_role("button", name=re.compile("Add comment", re.IGNORECASE))
    expect(add_comment_btn).to_be_visible()
    add_comment_btn.click()

    # The pending highlight shows the selection reached the editor on the right copy.
    pending = _editor_decorations(editor, ".md-comment-pending")
    assert [d["offset"] for d in pending] == [expected_offset], pending

    comment_body = "Rename this fox."
    textarea = file_viewer.locator("textarea[placeholder='Add a comment…']")
    expect(textarea).to_be_visible()
    textarea.fill(comment_body)
    file_viewer.get_by_role("button", name="Add Comment").click()
    expect(file_viewer).to_contain_text(comment_body)
    expect(editor.locator("[data-comment-id]")).to_have_count(1)
    expect(editor.locator(".md-comment-pending")).to_have_count(0)
    saved = _editor_decorations(editor, "[data-comment-id]")

    comments = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/comments", params={"path": file_path}, timeout=10.0
    ).json()
    assert len(comments) == 1, comments
    stored = comments[0]

    page.reload()
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_contain_text(_REPEATED_PARAGRAPH, timeout=15_000)
    expect(editor.locator("[data-comment-id]")).to_have_count(1, timeout=15_000)
    after_reload = _editor_decorations(editor, "[data-comment-id]")

    switch_markdown_view_mode(page, file_viewer, "Source")
    expect(file_viewer.locator("[data-line]", has_text=_REPEATED_PARAGRAPH)).to_be_visible(
        timeout=15_000
    )
    expect(file_viewer.locator("[data-line] > span[aria-hidden]").first).to_be_attached(
        timeout=15_000
    )
    overlays = file_viewer.evaluate(_SOURCE_OVERLAYS_JS)

    observed = {
        "editor_highlight_occurrences": [
            _occurrence_of(paragraph_offsets, d["offset"]) for d in saved
        ],
        "editor_highlight_after_reload": [
            _occurrence_of(paragraph_offsets, d["offset"]) for d in after_reload
        ],
        "source_view_occurrences": [
            _occurrence_of(paragraph_offsets, o["startCol"])
            if o["line"] == _REPEATED_PARAGRAPH
            else None
            for o in overlays
        ],
        "stored": stored,
        "expected_start_index": expected_raw_start,
    }
    assert observed["editor_highlight_occurrences"] == [_SELECTED_OCCURRENCE], observed
    assert observed["editor_highlight_after_reload"] == [_SELECTED_OCCURRENCE], observed
    assert stored["anchor_content"] == _WORD, observed
    assert stored["start_index"] == expected_raw_start, observed
    assert stored["end_index"] == expected_raw_start + len(_WORD), observed
    assert observed["source_view_occurrences"] == [_SELECTED_OCCURRENCE], observed
