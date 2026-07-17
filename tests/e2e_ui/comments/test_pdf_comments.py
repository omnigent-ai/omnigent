"""E2E: commenting on a rendered PDF file (the PdfViewer text-layer path).

PDFs render inline via react-pdf/pdf.js with a selectable text layer. Unlike
code/markdown/HTML, anchors are page-relative geometry encoded in
``anchor_content`` (``__pdf__{...}``), and highlights are browser-only overlay
divs (``.pdf-comment``). This test pins the full round-trip:

  1. A minimal ``.pdf`` is seeded via the filesystem resources API (no agent
     run), with a short phrase pdf.js renders into the text layer.
  2. The FileViewer opens the PDF in ``PdfViewer``.
  3. The user selects that phrase in the text layer; the floating "Add comment"
     button (portalled to the parent document) appears.
  4. Clicking it opens the CommentsPanel with the selection as the pending
     anchor and paints a pending highlight overlay (``.pdf-comment-active``).
  5. The user fills the body and saves; the comment card appears and a saved
     highlight overlay (``.pdf-comment``) remains on the page.
  6. Via the REST API, the stored comment carries a PDF geometry anchor whose
     decoded text matches the selection.

If this goes red, the regression is most likely in PdfViewer's selection capture,
floating-button portal, highlight overlay positioning, or PDF anchor encoding.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_PDF_FILE_PATH = "sample.pdf"

# Rendered by the fixture PDF into pdf.js's text layer — kept ASCII so the file
# survives the filesystem PUT endpoint's UTF-8 text path.
_ANCHOR_TEXT = "Hello PDF"

_PDF_CONTENT = """\
%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]
/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj
4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
5 0 obj<</Length 44>>stream
BT /F1 24 Tf 72 700 Td (Hello PDF) Tj ET
endstream endobj
trailer<</Root 1 0 R>>
%%EOF
"""

_PDF_ANCHOR_PREFIX = "__pdf__"


def _select_pdf_text(page: Page, file_viewer, text: str) -> None:
    """Drag-select ``text`` in the pdf.js text layer.

    ``Locator.select_text()`` does not reliably fire the ``mouseup`` handler
    on ``PdfViewer``'s scroll container in headless Chromium, so drive a real
    drag across the rendered glyph bounds instead.
    """
    layer = file_viewer.locator(".pdf-viewer .textLayer")
    expect(layer).to_be_visible(timeout=30_000)
    target = layer.get_by_text(text, exact=True)
    expect(target).to_be_visible()
    target.scroll_into_view_if_needed()
    box = target.bounding_box()
    assert box is not None, f"{text!r} has no bounding box"
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + 1, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] - 1, y, steps=6)
    page.mouse.up()


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_pdf_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed the PDF file and yield ``(base_url, session_id, path)``."""
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_PDF_FILE_PATH}"
    )
    resp = httpx.put(
        file_url,
        json={"content": _PDF_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    yield (base_url, session_id, _PDF_FILE_PATH)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_pdf_text_selection_add_comment_and_highlight(
    page: Page,
    seeded_pdf_session: tuple[str, str, str],
) -> None:
    """Select PDF text, add a comment, and verify the highlight overlay persists."""
    base_url, session_id, file_path = seeded_pdf_session
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{base_url}/c/{session_id}?file={_PDF_FILE_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    # Wait for pdf.js to render the canvas and text layer.
    expect(file_viewer.locator("canvas").first).to_be_visible(timeout=30_000)
    expect(file_viewer.locator(".pdf-viewer").get_by_text(_ANCHOR_TEXT)).to_be_visible(
        timeout=30_000
    )
    _select_pdf_text(page, file_viewer, _ANCHOR_TEXT)

    add_comment_btn = page.get_by_role("button", name=re.compile("Add comment", re.IGNORECASE))
    expect(add_comment_btn).to_be_visible(timeout=10_000)
    add_comment_btn.click()

    expect(file_viewer.locator("span.font-semibold", has_text="Comments")).to_be_visible()

    # Pending selection paints an active overlay before the comment is saved.
    pending_highlight = file_viewer.locator(".pdf-viewer .pdf-comment.pdf-comment-active")
    expect(pending_highlight.first).to_be_visible()
    pending_box = pending_highlight.first.bounding_box()
    assert pending_box is not None, "pending PDF comment highlight has no bounding box"
    assert pending_box["width"] > 0 and pending_box["height"] > 0

    comment_body = "Needs a citation in the PDF."
    comment_textarea = file_viewer.locator("textarea[placeholder='Add a comment…']")
    expect(comment_textarea).to_be_visible()
    comment_textarea.fill(comment_body)
    file_viewer.get_by_role("button", name="Add Comment").click()

    expect(file_viewer).to_contain_text(comment_body)

    # Saved comments keep a non-active overlay on the page.
    saved_highlight = file_viewer.locator(".pdf-viewer .pdf-comment")
    expect(saved_highlight.first).to_be_visible()
    saved_box = saved_highlight.first.bounding_box()
    assert saved_box is not None, "saved PDF comment highlight has no bounding box"
    assert saved_box["width"] > 0 and saved_box["height"] > 0

    comments_resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/comments?path={file_path}",
        timeout=10.0,
    )
    comments_resp.raise_for_status()
    comments = comments_resp.json()
    assert len(comments) == 1, f"Expected 1 comment, got {len(comments)}: {comments}"

    comment = comments[0]
    assert comment["body"] == comment_body
    anchor_content = comment["anchor_content"]
    assert anchor_content is not None
    assert anchor_content.startswith(_PDF_ANCHOR_PREFIX), (
        f"expected PDF geometry anchor, got {anchor_content!r}"
    )
    payload = json.loads(anchor_content[len(_PDF_ANCHOR_PREFIX) :])
    assert payload["text"] == _ANCHOR_TEXT, (
        f"decoded anchor text {payload['text']!r} != selected text {_ANCHOR_TEXT!r}"
    )
    assert payload["page"] == 1
    assert len(payload["rects"]) >= 1
    assert comment["end_index"] > comment["start_index"]
