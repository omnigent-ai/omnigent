"""E2E: right-clicking a previewed image must keep the native copy affordance.

Users copy a previewed PNG by right-clicking it. On the desktop shell that
menu is Electron-owned (covered by
``web/electron/e2e/desktop_image_copy_context_menu.e2e.js``). On the plain
browser surface the "Copy image" item lives in the browser's native context
menu, which no page-level driver can inspect; Chrome offers it iff

* the topmost element at the right-click point is the ``<img>`` itself (an
  overlay would swallow the image hit-test), and
* no page handler default-prevents the ``contextmenu`` event.

This test right-clicks the previewed image for real and asserts exactly those
two user-visible preconditions, so it fails if the SPA ever suppresses or
covers the file preview's native right-click menu (e.g. a zoom overlay that
eats the hit-test).

Seeded via the filesystem PUT endpoint, which can only carry text — hence an
SVG image fixture (see test_image_rendering.py); the ``<ImageViewer>`` under
test renders every image type through the same bare blob-backed ``<img>``.
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

_IMAGE_FILE_PATH = "preview-image.svg"

# A minimal valid image with explicit dimensions so the rendered <img> has a
# non-zero natural size once decoded (same shape as test_image_rendering.py).
_SVG_CONTENT = """\
<svg xmlns="http://www.w3.org/2000/svg" width="480" height="320" viewBox="0 0 480 320">
  <rect width="480" height="320" fill="#4f46e5"/>
  <circle cx="240" cy="160" r="96" fill="#ffffff"/>
</svg>
"""

# Records what the page observes for a real right-click on the image:
#  - capture phase (window): the event fired and what its target was;
#  - bubble phase (window, runs after all page handlers): whether any handler
#    default-prevented it — the one thing that suppresses the native menu.
_CONTEXTMENU_PROBE = """\
() => {
  window.__imageMenuProbe = { seen: false, prevented: null, targetTag: null, targetAlt: null };
  window.addEventListener('contextmenu', (e) => {
    window.__imageMenuProbe.seen = true;
    const t = e.target;
    window.__imageMenuProbe.targetTag = t && t.tagName;
    window.__imageMenuProbe.targetAlt = t && t.getAttribute && t.getAttribute('alt');
  }, true);
  window.addEventListener('contextmenu', (e) => {
    window.__imageMenuProbe.prevented = e.defaultPrevented;
  }, false);
}
"""

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_image_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed the image file and yield (base_url, session_id, path).

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


def test_preview_image_right_click_keeps_native_copy_menu(
    page: Page,
    seeded_image_session: tuple[str, str, str],
) -> None:
    """A real right-click on the previewed image must reach the bare <img>
    un-prevented, so the browser's native "Copy image" menu stays available."""
    base_url, session_id, _file_path = seeded_image_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")

    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(_IMAGE_FILE_PATH)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    # Two FileViewer instances mount with the same test id (mobile push-panel
    # and the desktop rail) — match the visible one directly.
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    img = file_viewer.locator(f'img[alt="{_IMAGE_FILE_PATH}"]')
    expect(img).to_be_visible(timeout=10_000)
    page.wait_for_function(
        "(el) => el.complete && el.naturalWidth > 0",
        arg=img.element_handle(),
        timeout=10_000,
    )

    # The element a right-click at the image's center actually hits must be
    # the <img> itself: the browser builds its context menu from that hit
    # test, and only an image target yields the "Copy image" item.
    topmost = page.evaluate(
        """
        (img) => {
          const r = img.getBoundingClientRect();
          const el = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
          return {
            tag: el && el.tagName,
            alt: el && el.getAttribute && el.getAttribute('alt'),
            cursor: el ? getComputedStyle(el).cursor : null,
          };
        }
        """,
        img.element_handle(),
    )
    # Breadcrumb for triage: the cursor here is the zoom affordance a user
    # sees instead of any copy affordance.
    print(f"image-preview probe: topmost element at image center = {topmost}")
    assert topmost["tag"] == "IMG", (
        f"right-click at the previewed image's center hits <{topmost['tag']}> "
        f"instead of the <img> — an overlay is swallowing the image hit-test, "
        f"so the native context menu loses its 'Copy image' item"
    )
    assert topmost["alt"] == _IMAGE_FILE_PATH

    # Dispatch a REAL right-click and verify no page handler suppressed the
    # native menu (preventDefault is the only page-side kill switch for it).
    page.evaluate(_CONTEXTMENU_PROBE)
    img.click(button="right")
    observed = page.evaluate("() => window.__imageMenuProbe")
    print(f"image-preview probe: contextmenu observation = {observed}")
    assert observed["seen"], "the right-click never produced a contextmenu event"
    assert observed["targetTag"] == "IMG" and observed["targetAlt"] == _IMAGE_FILE_PATH, (
        f"the contextmenu event targeted {observed['targetTag']} "
        f"(alt={observed['targetAlt']!r}), not the previewed <img>"
    )
    assert observed["prevented"] is False, (
        "a page handler default-prevented the contextmenu event on the "
        "previewed image, suppressing the browser's native 'Copy image' menu"
    )
