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

# The rendered text of a paragraph whose SOURCE wraps the words across multiple
# indented lines. The browser collapses that whitespace when rendering, so the
# rendered selection ("... one single line") is NOT a verbatim substring of the
# source — it exercises the whitespace-tolerant anchor matching on both sides.
_WRAPPED_RENDERED = (
    "uniquewrapped this rendered sentence spans several source lines yet reads as one single line"
)
_WRAPPED_SOURCE = (
    "uniquewrapped this rendered sentence\n"
    "      spans several source lines\n"
    "      yet reads as one single line"
)

# A phrase that appears twice — once as a title, once in body prose — so a
# comment on the title must highlight ONLY the title, not both copies.
_REPEATED_PHRASE = "uniquerepeated Aurora Sync"

# A sentence far down the document (after tall filler) so activating its comment
# must scroll the iframe to bring it into view.
_DEEP_SENTENCE = "uniquedeeptoken sentence near the bottom of the document"

_FILLER = "\n".join(
    f"    <p>Filler paragraph {i} providing vertical space.</p>" for i in range(60)
)

_HTML_CONTENT = f"""\
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Design doc</title>
  </head>
  <body>
    <h1>Design Doc</h1>
    <h2 id="repeated-title">{_REPEATED_PHRASE}</h2>
    <p id="anchor">{_ANCHOR_SENTENCE}</p>
    <p id="wrapped">{_WRAPPED_SOURCE}</p>
    <p>Body prose mentioning <span id="repeated-body">{_REPEATED_PHRASE}</span> again.</p>
{_FILLER}
    <p id="deep">{_DEEP_SENTENCE}</p>
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
        f"anchor_content {comment['anchor_content']!r} != selected sentence {_ANCHOR_SENTENCE!r}"
    )
    raw_idx = _HTML_CONTENT.find(_ANCHOR_SENTENCE)
    assert raw_idx != -1, "fixture bug: anchor sentence missing from file content"
    assert comment["start_index"] == raw_idx, (
        f"stored start_index={comment['start_index']} does not match the raw "
        f"source position {raw_idx} of the anchor sentence"
    )
    assert comment["end_index"] == raw_idx + len(_ANCHOR_SENTENCE)


def _open_preview(page: Page, base_url: str, session_id: str):
    """Navigate to the HTML file and return the (file_viewer, preview) locators."""
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{base_url}/c/{session_id}?file={_HTML_PATH}")
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    expect(file_viewer.locator('iframe[title="HTML preview"]')).to_be_visible(timeout=10_000)
    return file_viewer, file_viewer.frame_locator('iframe[title="HTML preview"]')


def _add_comment_on(file_viewer, page: Page, locator_id: str, body: str) -> None:
    """Select the element with ``locator_id`` in the frame and add ``body``."""
    preview = file_viewer.frame_locator('iframe[title="HTML preview"]')
    preview.locator(f"#{locator_id}").select_text()
    add_btn = page.get_by_role("button", name="Add comment")
    expect(add_btn).to_be_visible(timeout=10_000)
    add_btn.click()
    textarea = file_viewer.locator("textarea[placeholder='Add a comment…']")
    expect(textarea).to_be_visible()
    textarea.fill(body)
    file_viewer.get_by_role("button", name="Add Comment").click()
    expect(file_viewer).to_contain_text(body)


def test_wrapped_anchor_matches_across_source_lines(
    page: Page,
    seeded_html: tuple[str, str, str],
) -> None:
    """A selection whose source wraps across lines still anchors + highlights.

    The rendered text collapses the source's newlines/indentation, so the
    selection is not a verbatim source substring. This pins the whitespace-
    tolerant matching: the stored comment must anchor to the wrapped source span,
    and the bridge must paint a Custom Highlight over the rendered range (the
    matcher inside the frame would otherwise find nothing and leave it unpainted).
    """
    base_url, session_id, file_path = seeded_html
    file_viewer, preview = _open_preview(page, base_url, session_id)

    expect(preview.locator("#wrapped")).to_have_text(_WRAPPED_RENDERED, timeout=10_000)
    _add_comment_on(file_viewer, page, "wrapped", "wrapped-anchor comment")

    # The stored comment anchors to the wrapped span in the RAW source.
    comments = _get_comments(base_url, session_id, file_path)
    assert len(comments) == 1, comments
    raw_idx = _HTML_CONTENT.find(_WRAPPED_SOURCE)
    assert raw_idx != -1, "fixture bug: wrapped source span missing from file"
    assert comments[0]["start_index"] == raw_idx
    assert comments[0]["end_index"] == raw_idx + len(_WRAPPED_SOURCE)

    # The bridge painted the highlight over the rendered range (Custom Highlight
    # API is available in the Chromium harness).
    assert _highlight_count(preview) >= 1, "expected the wrapped anchor to be highlighted"


def test_repeated_anchor_highlights_only_commented_occurrence(
    page: Page,
    seeded_html: tuple[str, str, str],
) -> None:
    """A comment on repeated anchor text highlights only its own occurrence.

    ``_REPEATED_PHRASE`` appears twice — as a title and again in body prose. The
    bridge highlights by text match, so without the occurrence disambiguation it
    would light up both copies. Commenting on the title must paint exactly one
    highlight (the title's), matching the offset the parent stored.
    """
    base_url, session_id, file_path = seeded_html
    file_viewer, preview = _open_preview(page, base_url, session_id)

    expect(preview.locator("#repeated-title")).to_have_text(_REPEATED_PHRASE, timeout=10_000)
    _add_comment_on(file_viewer, page, "repeated-title", "title comment")

    # The comment anchored to the FIRST occurrence (the title) in the source.
    comments = _get_comments(base_url, session_id, file_path)
    assert len(comments) == 1, comments
    title_idx = _HTML_CONTENT.find(_REPEATED_PHRASE)
    assert comments[0]["start_index"] == title_idx

    # Exactly one range is highlighted, even though the phrase appears twice.
    assert _HTML_CONTENT.count(_REPEATED_PHRASE) == 2, "fixture bug: phrase should appear twice"
    assert _highlight_count(preview) == 1, "only the commented occurrence should be highlighted"


def test_repeated_anchor_second_occurrence_anchors_to_its_own_offset(
    page: Page,
    seeded_html: tuple[str, str, str],
) -> None:
    """Selecting the SECOND copy of repeated text anchors to the second offset.

    The inbound path: the bridge reports which occurrence was selected, and the
    parent resolves that occurrence's source offset. Without it, both copies
    resolve to the first match — so commenting on the body copy would be stored
    at the title's offset (and clicking it would open the title's comment).
    """
    base_url, session_id, file_path = seeded_html
    file_viewer, preview = _open_preview(page, base_url, session_id)

    expect(preview.locator("#repeated-body")).to_have_text(_REPEATED_PHRASE, timeout=10_000)
    _add_comment_on(file_viewer, page, "repeated-body", "body comment")

    # The stored comment must anchor to the SECOND occurrence in the source, not
    # the title's first one.
    comments = _get_comments(base_url, session_id, file_path)
    assert len(comments) == 1, comments
    title_idx = _HTML_CONTENT.find(_REPEATED_PHRASE)
    body_idx = _HTML_CONTENT.find(_REPEATED_PHRASE, title_idx + 1)
    assert body_idx != -1, "fixture bug: phrase should appear twice"
    assert comments[0]["start_index"] == body_idx, (
        f"comment anchored to {comments[0]['start_index']} (title is {title_idx}); "
        f"expected the body occurrence at {body_idx}"
    )


def test_native_selection_cleared_after_comment_saved(
    page: Page,
    seeded_html: tuple[str, str, str],
) -> None:
    """After saving a comment, the native selection is dropped inside the frame.

    The browser's ``::selection`` paints over the (lower-priority) Custom
    Highlight, so a lingering selection would keep the just-commented range grey
    instead of yellow until the user clicked elsewhere. The bridge clears it once
    a saved comment covers the selection.
    """
    base_url, session_id, _ = seeded_html
    file_viewer, preview = _open_preview(page, base_url, session_id)

    expect(preview.locator("#anchor")).to_have_text(_ANCHOR_SENTENCE, timeout=10_000)
    _add_comment_on(file_viewer, page, "anchor", "clears selection")

    # The selection inside the frame must be collapsed after save.
    selection_text = preview.locator("body").evaluate("() => window.getSelection().toString()")
    assert selection_text == "", f"native selection should be cleared, got {selection_text!r}"
    # And the highlight must be present (it was masked by grey before the clear).
    assert _highlight_count(preview) >= 1


def test_clicking_comment_scrolls_highlight_into_view(
    page: Page,
    seeded_html: tuple[str, str, str],
) -> None:
    """Activating a comment scrolls the sandboxed frame to its highlight."""
    base_url, session_id, file_path = seeded_html

    # Seed the deep comment via the REST API rather than the UI: the floating
    # "Add comment" button tracks the selection rect, so commenting on a
    # below-the-fold sentence would place the button off-screen. The scroll
    # behavior under test is driven by clicking the card, not by how it was made.
    raw_idx = _HTML_CONTENT.find(_DEEP_SENTENCE)
    assert raw_idx != -1, "fixture bug: deep sentence missing from file content"
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/comments",
        json={
            "path": file_path,
            "body": "deep comment",
            "start_index": raw_idx,
            "end_index": raw_idx + len(_DEEP_SENTENCE),
            "anchor_content": _DEEP_SENTENCE,
        },
        timeout=10.0,
    ).raise_for_status()

    file_viewer, preview = _open_preview(page, base_url, session_id)
    # The frame starts at the top; the deep highlight is far below the fold.
    assert preview.locator("body").evaluate("() => window.pageYOffset") == 0

    # Open the comments panel via the toolbar (the seeded comment isn't shown
    # until the panel is open).
    file_viewer.get_by_role("button", name="Show comments").click()

    # Clicking the comment card in the panel activates it → the bridge scrolls.
    file_viewer.get_by_text("deep comment").click()
    page.wait_for_timeout(800)  # smooth scroll settle
    scrolled = preview.locator("body").evaluate("() => window.pageYOffset")
    assert scrolled > 0, "activating the comment should scroll the frame to the highlight"


def test_selecting_highlight_scrolls_comment_panel_to_its_card(
    page: Page,
    seeded_html: tuple[str, str, str],
) -> None:
    """Selecting a highlighted range in the file reveals its card in the panel.

    The reverse direction of the scroll test: with many comments the panel list
    scrolls, so clicking a highlight near the bottom of the doc must scroll the
    panel to that comment's card (it would otherwise stay out of view).
    """
    base_url, session_id, file_path = seeded_html

    # Seed a comment on each filler paragraph so the panel list is long, plus one
    # on the deep sentence — the target we'll select in the file.
    def _post(anchor: str, body: str) -> None:
        idx = _HTML_CONTENT.find(anchor)
        assert idx != -1, f"fixture bug: {anchor!r} missing"
        httpx.post(
            f"{base_url}/v1/sessions/{session_id}/comments",
            json={
                "path": file_path,
                "body": body,
                "start_index": idx,
                "end_index": idx + len(anchor),
                "anchor_content": anchor,
            },
            timeout=10.0,
        ).raise_for_status()

    for i in range(30):
        _post(f"Filler paragraph {i} providing vertical space.", f"filler comment {i}")
    _post(_DEEP_SENTENCE, "deep card")

    file_viewer, preview = _open_preview(page, base_url, session_id)
    file_viewer.get_by_role("button", name="Show comments").click()

    # Select the deep sentence in the file (scroll it into the frame's view first
    # so its rendered range is laid out, then select it).
    preview.locator("#deep").scroll_into_view_if_needed()
    preview.locator("#deep").select_text()

    # The deep comment's card must scroll into the panel's visible viewport.
    deep_card = file_viewer.get_by_text("deep card")
    expect(deep_card).to_be_in_viewport(timeout=5_000)


def _get_comments(base_url: str, session_id: str, file_path: str) -> list[dict]:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/comments?path={file_path}",
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def _highlight_count(preview) -> int:
    """Number of ranges the bridge registered under the base comment highlight."""
    return preview.locator("body").evaluate(
        "() => { const h = (typeof CSS !== 'undefined' && CSS.highlights)"
        " ? CSS.highlights.get('omni-comment') : null; return h ? h.size : 0; }"
    )
