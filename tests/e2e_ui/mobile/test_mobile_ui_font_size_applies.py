"""E2E: the Appearance interface font size must apply on mobile, not just save.

On a phone-sized viewport the Settings > Appearance "Interface font size"
stepper persists the chosen px value, but the visible UI never changes size.

The stepper (``UiFontSizeControl`` in ``pages/SettingsPage.tsx``) writes the
choice to ``localStorage["omnigent:ui-font-size"]`` and sets
``--desktop-ui-font-size`` on ``<html>`` (``lib/uiFontPreferences.ts``). In
``index.css`` the typography mapping that reads that variable is gated
``@media (width >= 48rem)``; without a fix the ``@media (width < 48rem)``
branch hard-codes ``--mobile-ui-font-size: 14px`` and never references the
preference, so on mobile the write is a dead store: the value round-trips
through Settings but the rendered text stays at 14px.

The journey below is the reporter's, driven at a phone viewport (the mobile
web UI; the native iOS/Android apps render this same SPA in a WebView, so the
same CSS branch governs them). The persistence assertions guard the half that
already worked; the "text actually grew" assertions are the regression guard
for the applied size.

No LLM turn is involved.
"""

from __future__ import annotations

from playwright.sync_api import Page, ViewportSize, expect

STORAGE_KEY = "omnigent:ui-font-size"
GROUP_NAME = "Interface font size"

# Phone-sized viewport: comfortably below the 48rem (768px) breakpoint where
# index.css switches to the mobile typography branch.
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}


def _open_appearance(page: Page, base_url: str) -> None:
    """Navigate to Settings > Appearance the way a phone user does.

    On mobile the settings nav is a full-screen sidebar overlay that is pinned
    open on entering ``/settings`` and sits over the section content, so the
    journey must tap the Appearance nav row (which closes the overlay) before
    the stepper is reachable — a direct ``goto`` alone leaves the overlay
    intercepting every click.
    """
    page.goto(f"{base_url}/settings/appearance")
    _dismiss_settings_nav_overlay(page)
    expect(page.get_by_role("group", name=GROUP_NAME, exact=True)).to_be_visible(timeout=30_000)


def _dismiss_settings_nav_overlay(page: Page) -> None:
    """Tap the Appearance nav row so the mobile settings overlay closes."""
    nav_item = page.get_by_test_id("settings-nav-appearance")
    expect(nav_item).to_be_visible(timeout=30_000)
    nav_item.click()


def _rendered_ui_text_px(page: Page) -> float:
    """The pixel size the ``text-ui`` typography token actually renders at.

    Measures the real, user-visible "Interface font size" settings label (a
    ``text-ui`` element) rather than reading the CSS variable's declared text,
    so the assertion is on what a user sees.
    """
    label = page.locator("span.text-ui", has_text=GROUP_NAME).first
    expect(label).to_be_visible()
    return label.evaluate("el => parseFloat(getComputedStyle(el).fontSize)")


def _stored_size(page: Page) -> str | None:
    """The persisted font-size preference, or None when unset."""
    return page.evaluate(f"() => window.localStorage.getItem('{STORAGE_KEY}')")


def test_mobile_ui_font_size_applies_not_just_saves(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """At a phone viewport, stepping the size up must visibly scale the UI.

    Reporter's journey: open Settings / Appearance on mobile, increase the UI
    font size significantly, leave and return — the value is saved, but the
    interface stays at the same small size. The saved-value checks guard the
    half that already worked; the rendered-size checks are the regression
    guard for the applied size.
    """
    base_url, _session_id = seeded_session
    page.set_viewport_size(_MOBILE_VIEWPORT)
    _open_appearance(page, base_url)

    value = page.get_by_test_id("ui-font-size-input")
    increase = page.get_by_test_id("ui-font-size-inc")

    # Fresh context: the default 13px choice; mobile renders text-ui at its
    # own base size. Capture that rendered baseline before touching anything.
    expect(value).to_have_value("13")
    baseline_px = _rendered_ui_text_px(page)

    # Step to the 18px maximum — a "significant" increase per the report.
    for _ in range(8):
        if increase.is_disabled():
            break
        increase.click()
    expect(value).to_have_value("18")

    # Persistence works (and is NOT the bug): the choice is stored.
    assert _stored_size(page) == "18", "the stepped size was not persisted"

    # The bug: the visible UI must actually grow. On the broken build the
    # mobile typography branch never reads the preference, so the rendered
    # size stays at the 14px mobile base.
    applied_px = _rendered_ui_text_px(page)
    assert applied_px > baseline_px, (
        f"UI font size was saved (18px) but not applied on mobile: text-ui text "
        f"still renders at {applied_px}px (baseline {baseline_px}px)"
    )

    # Leave Settings and return (the reporter's step 4-6): the value is still
    # saved AND still applied after a reload at the phone viewport. The reload
    # re-pins the settings nav overlay, so dismiss it again first.
    page.reload()
    _dismiss_settings_nav_overlay(page)
    expect(page.get_by_role("group", name=GROUP_NAME, exact=True)).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("ui-font-size-input")).to_have_value("18")
    assert _stored_size(page) == "18"
    reload_px = _rendered_ui_text_px(page)
    assert reload_px > baseline_px, (
        f"UI font size survived the reload in Settings but the interface "
        f"reverted to {reload_px}px (baseline {baseline_px}px)"
    )
