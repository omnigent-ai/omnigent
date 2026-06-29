"""UI e2e: the app ships a PWA manifest (#8).

Asserts the served HTML links the web manifest — the entry point that makes the
app installable and is the precondition for the service worker + Web Push
subscribe flow (whose crypto/endpoints are unit + e2e covered elsewhere).
"""

from __future__ import annotations

from playwright.sync_api import Page, expect


def test_pwa_manifest_is_linked(page: Page, live_server: str) -> None:
    page.goto(f"{live_server}/")
    manifest = page.locator('link[rel="manifest"]')
    expect(manifest).to_have_count(1)
    assert "manifest.webmanifest" in (manifest.get_attribute("href") or "")
