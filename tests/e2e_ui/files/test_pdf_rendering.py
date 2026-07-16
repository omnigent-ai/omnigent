"""E2E: PDF files render inline through the FileViewer's <PdfViewer>.

A file the server classifies as ``application/pdf`` (via
``mimetypes.guess_type`` → ``content_type``) must render inline via react-pdf
(pdf.js) — a scrollable page column with a page-count + zoom toolbar — not the
"Preview not available for binary files." placeholder.

The new toolbar behaviour is also asserted here (it is user-facing and specific
to PDFs): the diff (Δ) and comments buttons are hidden because a rendered PDF
has no text surface to diff against or anchor comments to, and the zoom
percentage doubles as the reset control.

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
from playwright.sync_api import Page, expect

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_PDF_FILE_PATH = "sample.pdf"

# A minimal, valid single-page PDF drawing the text "Hello PDF". Kept ASCII so
# it survives the filesystem PUT endpoint's UTF-8 text path (binary/base64 can't
# be seeded there). pdf.js renders the text into the selectable text layer,
# which is what the test asserts on.
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pdf_file_renders_inline(
    page: Page,
    seeded_pdf_session: tuple[str, str, str],
) -> None:
    """A PDF renders inline via react-pdf, not the binary placeholder."""
    base_url, session_id, _file_path = seeded_pdf_session
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


def test_pdf_toolbar_hides_diff_and_comments(
    page: Page,
    seeded_pdf_session: tuple[str, str, str],
) -> None:
    """The diff and comments toggles are hidden for PDFs (no text surface)."""
    base_url, session_id, _file_path = seeded_pdf_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")

    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(_PDF_FILE_PATH)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    # Wait until the PDF has actually rendered so the toolbar is in its final state.
    expect(file_viewer.locator("canvas").first).to_be_visible(timeout=30_000)

    # PDFs render through PdfViewer, which has no text/selection surface — so the
    # diff (Δ) and comments toggles are suppressed. (Both appear for text files.)
    expect(file_viewer.get_by_role("button", name="Show diff")).to_have_count(0)
    expect(file_viewer.get_by_role("button", name="Show comments")).to_have_count(0)


def test_pdf_zoom_percentage_resets(
    page: Page,
    seeded_pdf_session: tuple[str, str, str],
) -> None:
    """Zooming in enables the percentage-as-reset control; clicking it returns to 100%."""
    base_url, session_id, _file_path = seeded_pdf_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")

    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(_PDF_FILE_PATH)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    expect(file_viewer.locator("canvas").first).to_be_visible(timeout=30_000)

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
