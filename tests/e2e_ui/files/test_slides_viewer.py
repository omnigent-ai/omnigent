"""E2E: ``*.slides.html`` opens in the slide-deck viewer and steps through slides.

Seeds a 3-slide deck via the filesystem PUT endpoint (no agent run), opens it in
the file viewer, and checks that the deck renders in the same sandboxed srcdoc
iframe as the HTML preview, shows one section at a time, and that the
next/previous controls, keyboard keys, and Source toggle work.

Playwright drives the browser via CDP, so it can read into the sandboxed
(opaque-origin) iframe to confirm which section is visible.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

# Seeded files land in the server's cwd, the repo root (see test_html_preview.py).
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Must stay in sync with ``HTML_PREVIEW_SANDBOX`` in web/src/shell/codeViewerHelpers.ts.
_EXPECTED_SANDBOX = (
    "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms allow-modals"
)

_DECK_PATH = "talk.slides.html"

_DECK_CONTENT = """\
<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8" /><title>Deck fixture</title></head>
  <body>
    <section id="s1"><h1>Slide one</h1></section>
    <section id="s2"><h1>Slide two</h1></section>
    <section id="s3"><h1>Slide three</h1></section>
  </body>
</html>
"""


@pytest.fixture
def seeded_deck(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed the slide deck and yield ``(base_url, session_id)``."""
    base_url, session_id = seeded_session
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_DECK_PATH}",
        json={"content": _DECK_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def test_slides_viewer_steps_through_deck(page: Page, seeded_deck: tuple[str, str]) -> None:
    """A 3-slide deck renders one section at a time and the controls step through it."""
    base_url, session_id = seeded_deck
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{base_url}/c/{session_id}?file={_DECK_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    iframe_el = file_viewer.locator('iframe[title="Slide deck"]')
    expect(iframe_el).to_be_visible(timeout=10_000)
    expect(iframe_el).to_have_attribute("sandbox", _EXPECTED_SANDBOX)
    # The plain HTML preview must not also mount for a deck.
    expect(file_viewer.locator('iframe[title="HTML preview"]')).to_have_count(0)

    deck = file_viewer.frame_locator('iframe[title="Slide deck"]')
    counter = file_viewer.get_by_text("1 / 3")
    expect(counter).to_be_visible()
    expect(deck.locator("#s1")).to_be_visible()
    expect(deck.locator("#s2")).to_be_hidden()

    next_btn = file_viewer.get_by_role("button", name="Next slide")
    prev_btn = file_viewer.get_by_role("button", name="Previous slide")
    expect(prev_btn).to_be_disabled()

    next_btn.click()
    expect(file_viewer.get_by_text("2 / 3")).to_be_visible()
    expect(deck.locator("#s2")).to_be_visible()
    expect(deck.locator("#s1")).to_be_hidden()

    next_btn.click()
    expect(file_viewer.get_by_text("3 / 3")).to_be_visible()
    expect(deck.locator("#s3")).to_be_visible()
    expect(next_btn).to_be_disabled()

    # Keyboard navigation while the viewer is focused.
    file_viewer.get_by_role("region", name="Slide deck").press("ArrowLeft")
    expect(file_viewer.get_by_text("2 / 3")).to_be_visible()
    expect(deck.locator("#s2")).to_be_visible()

    # Source returns to the existing code view.
    file_viewer.get_by_role("button", name="View deck source").click()
    expect(iframe_el).to_have_count(0)


_SAMPLE_KIT = _REPO_ROOT / "examples" / "design-kits" / "sample"

# The deck sets its own base colors; the kit must still win.
_OFF_BRAND_DECK = """\
<!DOCTYPE html>
<html>
  <head><style>section{background:#000;color:#fff;font-family:monospace}</style></head>
  <body>
    <section id="s1" class="layout-two-col"><h1>Off brand</h1><p>a</p><p>b</p></section>
  </body>
</html>
"""


def test_slides_viewer_applies_design_kit(page: Page, seeded_session: tuple[str, str]) -> None:
    """The sample kit in ``.omnigent/design-kit/`` brands a deck that ignores it."""
    base_url, session_id = seeded_session
    fs = f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem"
    files = {f".omnigent/design-kit/{p.name}": p.read_text() for p in _SAMPLE_KIT.iterdir()}
    files[_DECK_PATH] = _OFF_BRAND_DECK
    try:
        for path, content in files.items():
            httpx.put(
                f"{fs}/{path}", json={"content": content, "encoding": "utf-8"}, timeout=10.0
            ).raise_for_status()

        page.set_viewport_size({"width": 1600, "height": 900})
        page.goto(f"{base_url}/c/{session_id}?file={_DECK_PATH}")
        file_viewer = page.locator('[data-testid="file-viewer"]:visible')
        expect(file_viewer.get_by_title("Design kit: Sample Kit")).to_be_visible(timeout=10_000)

        slide = file_viewer.frame_locator('iframe[title="Slide deck"]').locator("#s1")
        expect(slide).to_be_visible()
        expect(slide).to_have_css("background-color", "rgb(248, 250, 252)")
        expect(slide).to_have_css("color", "rgb(15, 23, 42)")
        expect(slide).to_have_css("display", "grid")
        expect(slide.locator("h1")).to_have_css("font-family", "Georgia, serif")
        assert (
            slide.evaluate("el => getComputedStyle(el).getPropertyValue('--kit-primary').trim()")
            == "#3b2fc9"
        )
        logo = slide.evaluate("el => getComputedStyle(el, '::after').backgroundImage")
        assert logo.startswith('url("data:image/svg+xml;base64,')
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)
