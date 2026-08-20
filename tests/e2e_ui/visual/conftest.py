"""Shared fixtures for the visual-snapshot suite (one committed baseline per page).

Every page snapshot is a pure function of the committed SPA bundle plus a fixed
set of ``page.route`` stubs, captured at a fixed viewport in a pinned renderer
(see ``README.md``). These fixtures hold the parts every page shares -- the
deterministic viewport/palette, a JSON-route helper, and the pre-capture settle
-- so each ``test_*_snapshot.py`` only has to declare its page-specific stubs.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, Route

# Same fixed viewport for every page so baselines are comparable and stable.
_VIEWPORT = {"width": 1280, "height": 800}


@pytest.fixture
def snapshot_page(page: Page) -> Page:
    """A page pinned to a fixed viewport and light palette, ready for stubbing.

    Both are set before navigation so the SPA reads them on boot. The light
    scheme pins the whole palette regardless of the runner's
    ``prefers-color-scheme`` default.

    :param page: pytest-playwright page (fresh context per test).
    :returns: The same page, configured for a deterministic capture.
    """
    page.set_viewport_size(_VIEWPORT)
    page.emulate_media(color_scheme="light")
    return page


@pytest.fixture
def fulfill_json():
    """Return a helper that answers a route with a 200 JSON body.

    :returns: ``fulfill(route, payload)`` -- serializes *payload* and fulfills.
    """

    def _fulfill(route: Route, payload: object) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    return _fulfill


@pytest.fixture
def settle_for_snapshot():
    """Return a helper that settles time-dependent rendering before capture.

    Waits for web fonts (so glyph metrics don't shift mid-capture) and kills the
    blinking caret. The fonts expression must *return* the Promise so Playwright's
    sync API awaits it -- an arrow function calling ``.then()`` returns undefined
    and never waits.

    :returns: ``settle(page)`` -- call once the page's content has painted.
    """

    def _settle(page: Page) -> None:
        page.evaluate("document.fonts.ready")
        page.add_style_tag(content="* { caret-color: transparent !important; }")

    return _settle
