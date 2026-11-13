"""Browser e2e for "Open in new window": a session popped out into its own window.

Unit tests cover the menu wiring and the layout marker. This drives the real
browser boundary they can't: ``window.open`` creating a separate popup page that
loads the SPA, keeps the desktop layout at its smaller size, and chats with the
session while the original window stays where it was.
"""

from __future__ import annotations

import os
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, seed_committed_turn
from tests.e2e_ui.sessions._sidebar_helpers import patch_session

# Unique markers so assertions can't match UI chrome or another test's transcript.
_HISTORY_MARKER = "quince-popout-history"
_POPUP_MARKER = "quince-popout-reply"

# A desktop-sized opener; Chromium sizes the popup from its window features (960x820).
_DESKTOP_WINDOW = {"width": 1280, "height": 800}
_DESKTOP_CONTEXT = pytest.mark.browser_context_args(
    viewport=_DESKTOP_WINDOW, record_video_size=_DESKTOP_WINDOW
)


def _hold_for_viewer(page: Page, milliseconds: int) -> None:
    """Leave a recorded demo time to show each step; unrecorded runs don't wait."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(milliseconds)


def _expect_session_popup(popup: Page, session_id: str, title: str) -> None:
    """The popup shows only the session, in the desktop layout with the sidebar collapsed."""
    expect(popup).to_have_url(re.compile(rf"/c/{re.escape(session_id)}\?window=session$"))
    expect(popup.get_by_test_id("header-title")).to_have_text(title, timeout=30_000)
    expect(popup.locator("html")).to_have_attribute("data-session-window", "true")
    expect(popup.locator("html")).to_have_attribute("data-desktop-app", "true")
    assert popup.evaluate("[window.innerWidth, window.innerHeight]") == [960, 820]
    expect(popup.get_by_role("button", name="Open sidebar", exact=True)).to_be_visible()


@_DESKTOP_CONTEXT
def test_header_menu_pops_the_session_out_and_chats_there(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Header ⋯ → Open in new window opens a popup that chats; the original stays put."""
    base_url, session_id = seeded_session
    title = "Quarterly planning"
    patch_session(base_url, session_id, title=title)
    seed_committed_turn(
        session_id,
        prompt=f"Summarize the plan. {_HISTORY_MARKER}",
        reply="The plan has three milestones.",
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Replying from the separate window."}],
        match=_POPUP_MARKER,
    )
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_test_id("header-title")).to_have_text(title, timeout=30_000)
    expect(page.get_by_role("button", name="Close sidebar", exact=True)).to_be_visible()
    _hold_for_viewer(page, 800)

    page.get_by_test_id("header-conversation-actions").click()
    open_item = page.get_by_test_id("header-open-session-window")
    expect(open_item).to_have_text("Open in new window")
    _hold_for_viewer(page, 800)
    with page.context.expect_page() as popup_info:
        open_item.click()
    popup = popup_info.value
    _expect_session_popup(popup, session_id, title)
    expect(popup.get_by_text(_HISTORY_MARKER)).to_be_visible(timeout=30_000)

    composer = popup.get_by_placeholder("Send a message…")
    composer.fill(f"Send a short reply. {_POPUP_MARKER}")
    popup.get_by_role("button", name="Send", exact=True).click()
    assistant = popup.locator('[data-testid="message-bubble"][data-role="assistant"]').filter(
        has_text="Replying from the separate window."
    )
    expect(assistant).to_be_visible(timeout=60_000)
    _hold_for_viewer(popup, 1_500)

    popup.get_by_role("button", name="Open sidebar", exact=True).click()
    expect(popup.get_by_role("button", name="Close sidebar", exact=True)).to_be_visible()
    _hold_for_viewer(popup, 1_500)

    # The original window didn't navigate, and choosing it again brings the same
    # popup forward without reloading it (a reload would drop this page global).
    expect(page).to_have_url(re.compile(rf"/c/{re.escape(session_id)}$"))
    popup.evaluate("window.__popOutStillLoaded = true")
    page.get_by_test_id("header-conversation-actions").click()
    page.get_by_test_id("header-open-session-window").click()
    expect(page.get_by_role("menu")).to_have_count(0)
    page.wait_for_timeout(1_000)  # custom-lint: disable=sleep-then-assert -- no new popup
    assert len(page.context.pages) == 2
    assert popup.evaluate("window.__popOutStillLoaded === true")
    expect(assistant).to_be_visible()
