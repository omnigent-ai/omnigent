"""E2E: PDF files render inline through the FileViewer's <PdfViewer>.

A file the server classifies as ``application/pdf`` (via
``mimetypes.guess_type`` → ``content_type``) must render inline via react-pdf
(pdf.js) — a scrollable page column with a page-count + zoom toolbar — not the
"Preview not available for binary files." placeholder.

The new toolbar behaviour is also asserted here (it is user-facing and specific
to PDFs): the diff (Δ) button is hidden because a rendered PDF has no text
surface to diff against. Comments are supported via the pdf.js text layer.
percentage doubles as the reset control. A web link inside the PDF opens in a
new page while the app stays on its session; where popups are withheld, the
same click follows the link in place. The linked site is a routed stub.

The fixture seeds a minimal but valid PDF. It is pure ASCII, so it round-trips
through the filesystem PUT endpoint's text-only path (``str.encode(encoding)``)
— the same constraint that makes ``test_image_rendering`` use an SVG. Seeded via
the filesystem PUT endpoint (no agent run).
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_PDF_FILE_PATH = "sample.pdf"

_PDF_LINK_URL = "https://review.external.example/pulls/1"

# A minimal, valid single-page PDF drawing the text "Hello PDF" and an "Open pull
# request" URI link annotation. Kept ASCII so it survives the filesystem PUT
# endpoint's UTF-8 text path (binary/base64 can't be seeded there). pdf.js
# renders the text into the selectable text layer, which is what the test
# asserts on.
_PDF_CONTENT = f"""\
%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]
/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R/Annots[6 0 R]>>endobj
4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
5 0 obj<</Length 90>>stream
BT /F1 24 Tf 72 700 Td (Hello PDF) Tj ET
BT /F1 18 Tf 72 650 Td (Open pull request) Tj ET
endstream endobj
6 0 obj<</Type/Annot/Subtype/Link/Rect[66 640 240 672]/Border[0 0 0]
/A<</S/URI/URI({_PDF_LINK_URL})>>>>endobj
trailer<</Root 1 0 R>>
%%EOF
"""


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_pdf_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed the PDF file and yield (base_url, session_id, path).

    :param seeded_session: Runner-bound (base_url, session_id) pair.
    :returns: ``(base_url, session_id, file_path)`` for the test body.
    """
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


def _fulfill_linked_page(route: Route) -> None:
    route.fulfill(
        status=200,
        content_type="text/html",
        body="<title>Pull request</title><h1>Pull request</h1>",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pdf_preview_toolbar_zoom_and_web_link(
    seeded_pdf_session: tuple[str, str, str],
    page: Page,
) -> None:
    """Render a PDF, check its toolbar and zoom, then follow its web link."""
    base_url, session_id, _file_path = seeded_pdf_session
    page.context.route("https://review.external.example/**", _fulfill_linked_page)
    page.goto(f"{base_url}/c/{session_id}?view=explore")

    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(_PDF_FILE_PATH)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    # Two FileViewer instances mount with the same test id (mobile push-panel,
    # md:hidden, and the desktop rail). Match the visible one directly.
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    # react-pdf mounts a <canvas> per page and a selectable text layer. Wait for
    # the page to render, then assert the text layer carries the document text —
    # proof pdf.js actually parsed and rendered the file (not the placeholder).
    expect(file_viewer.locator("canvas").first).to_be_visible(timeout=30_000)
    expect(file_viewer.get_by_text("Hello PDF")).to_be_visible(timeout=30_000)

    # The binary placeholder must NOT be shown.
    expect(file_viewer.get_by_text("Preview not available")).to_have_count(0)

    # The toolbar reports the page count.
    expect(file_viewer.get_by_text(re.compile(r"^\d+ pages?$"))).to_be_visible()

    # PDFs render through PdfViewer — no diff surface, but comments are supported.
    expect(file_viewer.get_by_role("button", name="Show diff")).to_have_count(0)
    expect(file_viewer.get_by_role("button", name="Show comments")).to_be_visible()

    reset = file_viewer.get_by_role("button", name="Reset zoom")
    # At 100% the reset control shows "100%" and is disabled (nothing to reset).
    expect(reset).to_have_text("100%")
    expect(reset).to_be_disabled()

    # Zoom in twice → 150%; the reset control becomes enabled.
    zoom_in = file_viewer.get_by_role("button", name="Zoom in")
    zoom_in.click()
    zoom_in.click()
    expect(reset).to_have_text("150%")
    expect(reset).to_be_enabled()

    # Clicking the percentage resets to 100% and disables itself again.
    reset.click()
    expect(reset).to_have_text("100%")
    expect(reset).to_be_disabled()

    pdf_link = file_viewer.locator(f'.annotationLayer a[href="{_PDF_LINK_URL}"]')
    expect(pdf_link).to_be_attached(timeout=10_000)
    with page.context.expect_page(timeout=10_000) as new_page_info:
        pdf_link.click()
    new_page = new_page_info.value
    new_page.wait_for_load_state()
    expect(new_page).to_have_url(_PDF_LINK_URL)
    expect(new_page.get_by_role("heading", name="Pull request")).to_be_visible()

    expect(page).to_have_url(re.compile(rf"/c/{session_id}"))
    expect(file_viewer.get_by_text("Hello PDF")).to_be_visible()

    # An embedding host pane or a popup blocker withholds the new tab; the click
    # must still follow the link instead of being silently swallowed.
    page.evaluate("window.open = () => null")
    pdf_link.click()
    expect(page).to_have_url(_PDF_LINK_URL)
