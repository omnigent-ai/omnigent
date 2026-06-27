"""E2E: commenting on a *rendered* HTML file (the bridge path).

HTML files open in the sandboxed preview iframe (opaque origin, no
``allow-same-origin``), so the host app cannot read the frame's selection
directly. ``HtmlCommentViewer`` injects a small bridge script into the frame
that relays selections over a private MessageChannel; the parent then drives
the same CommentsPanel + comment store used by Markdown and code. This test
pins that round-trip end to end:

  1. An ``.html`` file is seeded via the filesystem resources API (no agent
     run), with a sentence that appears exactly once — verbatim in both the
     rendered text and the raw source — so the stored offset is deterministic.
  2. The FileViewer opens the file in the preview iframe (the HTML default).
  3. The user selects that sentence *inside the sandboxed frame*; the bridge
     relays it and the floating "Add comment" button (portalled to the parent
     document) appears.
  4. Clicking it opens the CommentsPanel with the selection as the pending
     anchor; the user fills the body and saves.
  5. Via the REST API, the stored comment carries the selected sentence as its
     ``anchor_content`` at the offset matching the raw HTML source — so the
     agent (which edits the source) can locate it.

If this goes red, the regression is most likely in the bridge handshake or the
selection relay: iframe load → MessageChannel init → ``omni:selection`` →
parent offset resolution → ``onSetActiveSelection`` → add-comment POST.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

# The hello_world agent spec uses ``os_env.cwd: .``, so the runner writes seeded
# files into the server process's cwd — the repo root (this file is
# ``<repo>/tests/e2e_ui/comments/...``, so the repo root is ``parents[3]``).
_REPO_ROOT = Path(__file__).resolve().parents[3]

_HTML_PATH = "design_doc.html"

# A distinctive sentence that appears exactly once and is identical in the
# rendered text and the raw source (no inline markup inside it), so the rendered
# selection maps to a single, deterministic offset in the source.
_ANCHOR_SENTENCE = "uniqueanchortoken design review sentence"

_HTML_CONTENT = f"""\
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Design doc</title>
  </head>
  <body>
    <h1>Design Doc</h1>
    <p id="anchor">{_ANCHOR_SENTENCE}</p>
    <p>Some other unrelated prose for context.</p>
  </body>
</html>
"""


def _cleanup_session_workdir(session_id: str) -> None:
    shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


@pytest.fixture
def seeded_html(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str, str]]:
    """Seed the HTML doc and yield ``(base_url, session_id, path)``."""
    base_url, session_id = seeded_session
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_HTML_PATH}",
        json={"content": _HTML_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id, _HTML_PATH)
    finally:
        _cleanup_session_workdir(session_id)


def test_html_preview_add_comment(
    page: Page,
    seeded_html: tuple[str, str, str],
) -> None:
    """Select rendered HTML text, add a comment, and verify it persists."""
    base_url, session_id, file_path = seeded_html
    # Wide viewport so the toolbar renders inline and the panel has room.
    page.set_viewport_size({"width": 1600, "height": 900})
    # HTML files default to preview, so the iframe mounts directly.
    page.goto(f"{base_url}/c/{session_id}?file={_HTML_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    iframe_el = file_viewer.locator('iframe[title="HTML preview"]')
    expect(iframe_el).to_be_visible(timeout=10_000)

    # Reach into the sandboxed (opaque-origin) frame via CDP and select the
    # anchor sentence. select_text drives a programmatic selection, which the
    # bridge picks up via its debounced selectionchange listener.
    preview = file_viewer.frame_locator('iframe[title="HTML preview"]')
    expect(preview.locator("#anchor")).to_have_text(_ANCHOR_SENTENCE, timeout=10_000)
    preview.locator("#anchor").select_text()

    # The floating "Add comment" button is portalled to the PARENT document
    # (not inside the frame), so find it on the page.
    add_comment_btn = page.get_by_role("button", name="Add comment")
    expect(add_comment_btn).to_be_visible(timeout=10_000)
    add_comment_btn.click()

    # CommentsPanel opens alongside the preview.
    expect(file_viewer.locator("span.font-semibold", has_text="Comments")).to_be_visible()

    comment_body = "This sentence needs a citation."
    comment_textarea = file_viewer.locator("textarea[placeholder='Add a comment…']")
    expect(comment_textarea).to_be_visible()
    comment_textarea.fill(comment_body)
    file_viewer.get_by_role("button", name="Add Comment").click()

    # The comment card appears in the panel.
    expect(file_viewer).to_contain_text(comment_body)

    # Verify via the REST API that the comment persisted with the selected
    # sentence as its anchor at an offset matching the raw HTML source.
    comments_resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/comments?path={file_path}",
        timeout=10.0,
    )
    comments_resp.raise_for_status()
    comments = comments_resp.json()
    assert len(comments) == 1, f"Expected 1 comment, got {len(comments)}: {comments}"

    comment = comments[0]
    assert comment["body"] == comment_body
    assert comment["anchor_content"] == _ANCHOR_SENTENCE, (
        f"anchor_content {comment['anchor_content']!r} != selected sentence "
        f"{_ANCHOR_SENTENCE!r}"
    )
    raw_idx = _HTML_CONTENT.find(_ANCHOR_SENTENCE)
    assert raw_idx != -1, "fixture bug: anchor sentence missing from file content"
    assert comment["start_index"] == raw_idx, (
        f"stored start_index={comment['start_index']} does not match the raw "
        f"source position {raw_idx} of the anchor sentence"
    )
    assert comment["end_index"] == raw_idx + len(_ANCHOR_SENTENCE)
