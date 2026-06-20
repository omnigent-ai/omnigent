"""E2E: adding a comment on a NON-markdown file (the Monaco code path).

The existing comment e2e coverage all runs through markdown files, which
render in the TipTap rich-text editor (see ``test_markdown_editor_comments``).
Non-markdown files take an entirely different surface — they render in the
Monaco editor, whose comment affordances live in ``useMonacoCommentLayer``
rather than the TipTap extension. This test pins that the Monaco path also
supports adding comments end to end:

  1. A ``.py`` file is seeded directly via the filesystem resources API (no
     agent run needed), so the test is fast and deterministic.
  2. The FileViewer opens the file in the Monaco editor (the default for any
     non-markdown, non-preview file).
  3. The user selects a word in the editor (a double-click word-select, which
     fires Monaco's ``onDidChangeCursorSelection``); the floating
     "Add comment" button appears.
  4. Clicking it opens the CommentsPanel with the selection as the pending
     anchor.
  5. The user fills in the comment body and saves it; the comment card appears
     in the CommentsPanel with the correct body.
  6. Via the REST API, the stored comment carries the selected word as its
     ``anchor_content`` at an offset that matches the raw file content.

If this goes red, the likely regression is in the Monaco comment layer:
selection → floating button → ``onSetActiveSelection`` → add-comment POST.
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

_PY_FILE_PATH = "comment_target.py"

# A distinctive identifier that appears exactly once so the double-click
# word-select is unambiguous and the stored offset is deterministic.
_ANCHOR_WORD = "uniqueanchortoken"

# Python source — the anchor word lives in a trailing comment so it sits on its
# own token and double-clicking selects the whole identifier.
_PY_CONTENT = f"""\
def greet(name):
    message = "hello " + name  # {_ANCHOR_WORD}
    print(message)
    return message
"""


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_python_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed a ``.py`` file into the session and yield (base_url, session_id, path).

    The file is written through
    ``PUT /v1/sessions/{id}/resources/environments/default/filesystem/{path}``
    so it shows up in the FileViewer without requiring an agent run.

    :param seeded_session: Base fixture providing a runner-bound
        ``(base_url, session_id)`` pair.
    :returns: ``(base_url, session_id, file_path)``.
    """
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_PY_FILE_PATH}"
    )
    resp = httpx.put(
        file_url,
        json={"content": _PY_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    yield (base_url, session_id, _PY_FILE_PATH)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_monaco_non_markdown_add_comment(
    page: Page,
    seeded_python_session: tuple[str, str, str],
) -> None:
    """Select a word in the Monaco editor, add a comment, and verify it persists."""
    base_url, session_id, file_path = seeded_python_session
    page.goto(f"{base_url}/c/{session_id}")
    # The rail defaults open but is remembered per session; ensure it is open so
    # the changed-files panel (and its file-open button) are reachable.
    open_right_rail(page)

    # The changed-file row renders two buttons carrying the filename: the
    # file-open button (visible text) and an icon-only Download button
    # (aria-label "Download <name>"). Filter to the open button by its visible
    # text so the locator stays single-element under strict mode.
    file_button = page.get_by_role("button", name=re.compile(re.escape(_PY_FILE_PATH))).filter(
        has_text=_PY_FILE_PATH
    )
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    # Non-markdown files render in Monaco (lazy-loaded ~MBs + worker), so the
    # editor surface can take a moment to mount. Wait for the rendered lines.
    editor = file_viewer.locator(".monaco-editor")
    expect(editor).to_be_visible(timeout=20_000)
    expect(file_viewer.locator(".view-lines")).to_contain_text(_ANCHOR_WORD, timeout=10_000)

    # Double-click the anchor word: Monaco selects the whole identifier and
    # fires onDidChangeCursorSelection, which surfaces the floating button.
    file_viewer.get_by_text(_ANCHOR_WORD).first.dblclick()

    # The floating "Add comment" button is portalled to document.body. It runs
    # its action on mousedown (Playwright's click fires mousedown first), so a
    # plain click opens the CommentsPanel with the selection as pending anchor.
    add_comment_btn = page.get_by_role("button", name=re.compile("Add comment", re.IGNORECASE))
    expect(add_comment_btn).to_be_visible()
    add_comment_btn.click()

    # CommentsPanel opens alongside the editor.
    expect(file_viewer.locator("span.font-semibold", has_text="Comments")).to_be_visible()

    comment_body = "This is a comment on a non-markdown (Python) file."
    comment_textarea = file_viewer.locator("textarea[placeholder='Add a comment…']")
    expect(comment_textarea).to_be_visible()
    comment_textarea.fill(comment_body)
    file_viewer.get_by_role("button", name="Add Comment").click()

    # The comment card appears in the CommentsPanel.
    expect(file_viewer).to_contain_text(comment_body)

    # Verify via the REST API that the comment was persisted with the selected
    # word as its anchor at an offset matching the raw file content.
    comments_resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/comments?path={file_path}",
        timeout=10.0,
    )
    comments_resp.raise_for_status()
    comments = comments_resp.json()
    assert len(comments) == 1, f"Expected 1 comment, got {len(comments)}: {comments}"

    comment = comments[0]
    assert comment["body"] == comment_body
    assert comment["anchor_content"] == _ANCHOR_WORD, (
        f"anchor_content {comment['anchor_content']!r} != selected word {_ANCHOR_WORD!r}"
    )
    raw_idx = _PY_CONTENT.find(_ANCHOR_WORD)
    assert raw_idx != -1, "fixture bug: anchor word missing from file content"
    assert comment["start_index"] == raw_idx, (
        f"stored start_index={comment['start_index']} does not match the raw "
        f"file position {raw_idx} of {_ANCHOR_WORD!r}"
    )
