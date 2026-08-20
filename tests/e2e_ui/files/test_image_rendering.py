"""E2E: image files render through the FileViewer's <ImageViewer>.

A file the server classifies as an image (via ``mimetypes.guess_type`` →
``content_type``) must render as an ``<img>`` fed by a blob URL, not as
syntax-highlighted source or the binary placeholder.

We use an SVG fixture rather than a raster image because the filesystem PUT
endpoint can only seed text (``str.encode(encoding)`` — base64 is not a text
codec), and SVG is valid UTF-8 that round-trips through that path. It also
exercises the security-relevant case: SVG is rendered only through a blob URL
(``<img src=blob:...>``), never inlined into the DOM, so any embedded script
cannot execute. Seeded via the filesystem PUT endpoint (no agent run).
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

_IMAGE_FILE_PATH = "diagram.svg"

# A minimal but valid SVG with explicit dimensions so the rendered <img> has a
# non-zero natural size once the browser decodes it.
_SVG_CONTENT = """\
<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80" viewBox="0 0 120 80">
  <rect width="120" height="80" fill="#4f46e5"/>
  <circle cx="60" cy="40" r="24" fill="#ffffff"/>
</svg>
"""


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_image_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed the SVG file and yield (base_url, session_id, path).

    :param seeded_session: Runner-bound (base_url, session_id) pair.
    :returns: ``(base_url, session_id, file_path)`` for the test body.
    """
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_IMAGE_FILE_PATH}"
    )
    resp = httpx.put(
        file_url,
        json={"content": _SVG_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    yield (base_url, session_id, _IMAGE_FILE_PATH)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_image_file_renders_as_img(
    page: Page,
    seeded_image_session: tuple[str, str, str],
) -> None:
    """An image file renders as a blob-backed <img>, not source or placeholder."""
    base_url, session_id, _file_path = seeded_image_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")

    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(_IMAGE_FILE_PATH)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    # Two FileViewer instances mount with the same test id (mobile push-panel,
    # md:hidden, and the desktop rail). Match the visible one directly.
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    # The image renders as an <img> tagged with the filename as its alt text.
    img = file_viewer.locator(f'img[alt="{_IMAGE_FILE_PATH}"]')
    expect(img).to_be_visible(timeout=10_000)

    # The blob actually decoded as an image — i.e. the onError fallback did NOT
    # fire. A loaded <img> reports complete=true and naturalWidth>0.
    page.wait_for_function(
        "(el) => el.complete && el.naturalWidth > 0",
        arg=img.element_handle(),
        timeout=10_000,
    )

    # It rendered through the blob URL, never as inline SVG markup nor as the
    # binary placeholder / source text.
    expect(img).to_have_attribute("src", re.compile(r"^blob:"))
    expect(file_viewer.get_by_text("Unable to render image")).to_have_count(0)
    expect(file_viewer.locator("[contenteditable='true']")).to_have_count(0)


def test_image_click_opens_zoom_lightbox(
    page: Page,
    seeded_image_session: tuple[str, str, str],
) -> None:
    """Clicking a previewed image opens the shared full-screen zoom lightbox."""
    base_url, session_id, _file_path = seeded_image_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")

    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(_IMAGE_FILE_PATH)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    img = file_viewer.locator(f'img[alt="{_IMAGE_FILE_PATH}"]')
    expect(img).to_be_visible(timeout=10_000)

    # No lightbox until the user clicks the inline image.
    expect(page.get_by_role("dialog")).to_have_count(0)

    img.click()

    # The Radix dialog now hosts the shared ZoomViewer with its zoom toolbar.
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible(timeout=10_000)
    expect(dialog.get_by_role("button", name="Zoom in")).to_be_visible()
    expect(dialog.get_by_role("button", name="Zoom out")).to_be_visible()

    # The same SVG is shown in the lightbox, still via its blob URL.
    expect(dialog.locator(f'img[alt="{_IMAGE_FILE_PATH}"]')).to_have_attribute(
        "src", re.compile(r"^blob:")
    )

    # Escape dismisses the lightbox (Radix Dialog default).
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
