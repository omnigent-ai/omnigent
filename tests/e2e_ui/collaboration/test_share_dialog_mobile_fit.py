"""UI: the Share dialog lays out inside its own boundary on a phone viewport.

The iOS/Android apps host this same SPA in a WebView, so the Share modal's
phone-width layout is a web contract. On a phone-sized viewport the dialog is
capped to the viewport width, but the add-grant row (user field + permission
level select + Grant button) must shrink with it: when the row keeps its
desktop min-content width, the level select and Grant button are pushed
through the dialog's right edge and clipped at the screen boundary.

Runs on the dedicated multi-user server (Share chrome is hidden on the
single-user shared server), as the admin identity, at an iPhone-class
viewport, opening Share the way a phone user does — via the header kebab.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, Locator, Page, ViewportSize, expect

from tests.e2e_ui.collaboration._multi_user_server import (
    ADMIN_EMAIL,
    MultiUserServer,
    spawn_multi_user_server,
)

# iPhone 16 Pro-class portrait viewport (402x874 CSS px @3x), the device class
# the overflow was reported on. The overflow also bites at 390x844.
_IPHONE_VIEWPORT: ViewportSize = {"width": 402, "height": 874}

_IOS_SAFARI_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)


@pytest.fixture(scope="module")
def multi_user_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MultiUserServer]:
    """A dedicated NON-single-user server (Share chrome enabled)."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_share_dialog_mobile")
    yield from spawn_multi_user_server(mock_llm_server_url, server_tmp)


def _mobile_admin_page(browser: Browser) -> Page:
    """An iPhone-profile page whose requests carry the admin identity."""
    # The conftest's OMNIGENT_E2E_RECORD_DIR hook only covers the async API,
    # so honor it here for this sync, manually-created context.
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    context = browser.new_context(
        viewport=_IPHONE_VIEWPORT,
        device_scale_factor=3,
        is_mobile=True,
        has_touch=True,
        user_agent=_IOS_SAFARI_UA,
        extra_http_headers={"X-Forwarded-Email": ADMIN_EMAIL},
        record_video_dir=record_dir,
    )
    return context.new_page()


def _open_share_dialog(page: Page) -> Locator:
    """Open the Share modal the way a phone user does: header kebab → Share."""
    kebab = page.get_by_test_id("header-conversation-actions")
    expect(kebab).to_be_visible(timeout=60_000)
    kebab.click()
    share_item = page.get_by_test_id("header-share-conversation")
    expect(share_item).to_be_visible()
    share_item.click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_text("Share this session")).to_be_visible()
    # The dialog mounts mid zoom-in animation; bounding boxes are only
    # trustworthy once its animations have finished.
    page.wait_for_function(
        "document.querySelector('[data-slot=\"dialog-content\"]')?.getAnimations().length === 0"
    )
    return dialog


def _right_edge(locator: Locator) -> float:
    box = locator.bounding_box()
    assert box is not None, f"no bounding box for {locator}"
    return box["x"] + box["width"]


def test_share_dialog_grant_controls_fit_on_phone_viewport(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """The add-grant row's controls stay inside the dialog on a phone."""
    page = _mobile_admin_page(browser)
    try:
        page.goto(f"{multi_user_server.public_url}/c/{multi_user_server.session_id}")
        dialog = _open_share_dialog(page)
        # Grants have loaded (the owner's own row renders) — the dialog is at
        # its final layout.
        expect(dialog.get_by_title(ADMIN_EMAIL)).to_be_visible(timeout=10_000)
        if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
            page.wait_for_timeout(1_500)

        dialog_box = dialog.bounding_box()
        assert dialog_box is not None
        dialog_right = dialog_box["x"] + dialog_box["width"]
        padding_right = float(
            dialog.evaluate("el => parseFloat(getComputedStyle(el).paddingRight)")
        )
        content_right = dialog_right - padding_right

        grant_button = dialog.get_by_role("button", name="Grant")
        level_select = dialog.get_by_role("combobox").filter(has_text="Read")
        user_field = dialog.get_by_placeholder("alice@example.com")
        expect(grant_button).to_be_visible()
        expect(level_select).to_be_visible()
        expect(user_field).to_be_visible()

        # The dialog itself must fit the viewport…
        assert dialog_box["x"] >= -0.5
        assert dialog_right <= _IPHONE_VIEWPORT["width"] + 0.5, (
            f"share dialog spills past the viewport: right edge {dialog_right:.1f}"
            f" vs viewport width {_IPHONE_VIEWPORT['width']}"
        )
        # …and the grant row's controls must fit inside the dialog's padding
        # box instead of being pushed through its right edge.
        for name, control_right in [
            ("permission level select", _right_edge(level_select)),
            ("Grant button", _right_edge(grant_button)),
        ]:
            assert control_right <= content_right + 1.0, (
                f"{name} overflows the share dialog: right edge"
                f" {control_right:.1f} vs dialog content right edge"
                f" {content_right:.1f} (dialog right {dialog_right:.1f})"
            )
    finally:
        page.context.close()
