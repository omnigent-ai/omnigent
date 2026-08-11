"""E2E: operator branding from the server config reshapes the web UI.

Branding is delivered entirely through the unauthenticated ``GET /v1/info``
probe the SPA fetches at boot (see ``web/src/lib/capabilities.ts`` and
``web/src/lib/branding.ts``): a ``branding`` block carries the app name, the
landing hero heading, per-variant logo URLs, and the ``powered_by`` flag. Each
test stubs ``/v1/info`` (and the logo route) so the assertions don't depend on
a server-side config file, then loads the landing composer ("/") and checks the
rendered result.

Covered: custom app name (browser-tab title + sidebar wordmark), custom hero
heading, an explicit empty heading that hides the hero line instead of falling
back to the default, the custom in-app logo and favicon, and the "Powered by
Omnigent" credit — which appears only when branding is set.
"""

from __future__ import annotations

import base64
import re

from playwright.sync_api import Page, expect

_LOGO_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

_LOGOS = {
    "main": "/v1/branding/logo/main",
    "loading": "/v1/branding/logo/loading",
    "favicon": "/v1/branding/logo/favicon",
}


def _stub_info(page: Page, branding: object) -> None:
    """Serve ``/v1/info`` carrying only ``branding``; the SPA defaults the rest.

    ``resolveServerInfo`` reads every other field defensively, so a payload with
    just ``branding`` yields a stock, accounts-off server.
    """
    page.route("**/v1/info", lambda route: route.fulfill(json={"branding": branding}))


def _stub_logos(page: Page) -> None:
    """Serve any ``/v1/branding/logo/<variant>`` request as a small PNG."""
    page.route(
        "**/v1/branding/logo/**",
        lambda route: route.fulfill(status=200, content_type="image/png", body=_LOGO_PNG),
    )


def _open_landing(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/")
    expect(page.get_by_test_id("new-chat-landing")).to_be_visible(timeout=30_000)


def test_custom_branding_reshapes_the_landing(page: Page, seeded_session: tuple[str, str]) -> None:
    """A full branding block sets the name, heading, logo, favicon, and credit."""
    base_url, _ = seeded_session
    _stub_logos(page)
    _stub_info(
        page,
        {"app_name": "Acme Agent", "heading": "Acme Corp", "logos": _LOGOS, "powered_by": True},
    )
    _open_landing(page, base_url)

    # App name → browser-tab title and sidebar wordmark.
    expect(page).to_have_title(re.compile(r"Acme Agent"))
    expect(page.get_by_role("link", name="Acme Agent")).to_be_visible()

    # Custom hero heading replaces the built-in default.
    expect(page.get_by_role("heading", name="Acme Corp")).to_be_visible()
    expect(page.get_by_text("What should we build?")).to_have_count(0)

    # Custom logo renders as an <img> (alt = app name); the favicon points at
    # the branding route.
    expect(page.locator('img[alt="Acme Agent"]').first).to_be_visible()
    expect(page.locator('link[rel="icon"]')).to_have_attribute(
        "href", re.compile(r"/v1/branding/logo/favicon")
    )

    # The credit shows because branding is set.
    expect(page.get_by_test_id("powered-by-omnigent")).to_be_visible()


def test_empty_heading_hides_the_hero_line(page: Page, seeded_session: tuple[str, str]) -> None:
    """An explicit empty heading hides the hero line rather than defaulting."""
    base_url, _ = seeded_session
    _stub_logos(page)
    _stub_info(
        page,
        {"app_name": "Acme Agent", "heading": "", "logos": _LOGOS, "powered_by": True},
    )
    _open_landing(page, base_url)

    # Neither the custom nor the built-in default heading appears.
    expect(page.get_by_text("What should we build?")).to_have_count(0)
    # The landing did load branded (credit present), so this is a real hide.
    expect(page.get_by_test_id("powered-by-omnigent")).to_be_visible()


def test_powered_by_false_hides_the_credit(page: Page, seeded_session: tuple[str, str]) -> None:
    """A branded deployment can explicitly hide the Omnigent attribution."""
    base_url, _ = seeded_session
    _stub_logos(page)
    _stub_info(
        page,
        {"app_name": "Acme Agent", "heading": "Acme Corp", "logos": _LOGOS, "powered_by": False},
    )
    _open_landing(page, base_url)

    expect(page.get_by_role("heading", name="Acme Corp")).to_be_visible()
    expect(page.get_by_test_id("powered-by-omnigent")).to_have_count(0)


def test_unbranded_keeps_defaults_and_hides_credit(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """With no branding, the UI keeps its defaults and shows no credit."""
    base_url, _ = seeded_session
    _stub_info(page, None)
    _open_landing(page, base_url)

    expect(page.get_by_role("heading", name="What should we build?")).to_be_visible()
    expect(page.get_by_role("link", name="Omnigent")).to_be_visible()
    expect(page.get_by_test_id("powered-by-omnigent")).to_have_count(0)
