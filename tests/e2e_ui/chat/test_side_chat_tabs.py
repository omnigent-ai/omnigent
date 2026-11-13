"""Side-chat tab names, pinning, reordering and density in a real browser.

The Workspace rail's side-chat tabs are arranged locally in the browser. These
journeys cover what jsdom cannot: dnd-kit drag on real pointer events, Radix
right-click menus, and the strip's visible layout as tabs accumulate. Setup
runs before the page is created so a recording starts at the first navigation.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm, open_right_rail

_TAB_SELECTOR = "[data-side-chat-tab] [role='tab']"

# Record at the viewport's size so the strip's small tab labels stay legible.
pytestmark = pytest.mark.browser_context_args(record_video_size={"width": 1280, "height": 720})


@pytest.fixture
def side_chat_forks(seeded_session: tuple[str, str]) -> Iterator[list[str]]:
    """Delete forks a test recorded, after its page and recording have closed."""
    base_url, _session_id = seeded_session
    child_ids: list[str] = []
    yield child_ids
    for child_id in child_ids:
        httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)


def _track_forks(page: Page, session_id: str, child_ids: list[str]) -> None:
    def track_fork(route: Route) -> None:
        response = route.fetch()
        if response.ok:
            child_ids.append(response.json()["id"])
        route.fulfill(response=response)

    page.route(f"**/v1/sessions/{session_id}/fork", track_fork)


def _hold(page: Page, milliseconds: int) -> None:
    """When recording, hold the frame so each step is readable in the clip."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(milliseconds)


def _open_session(request: pytest.FixtureRequest, base_url: str, session_id: str) -> Page:
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible()
    open_right_rail(page)
    return page


def _rail(page: Page) -> Locator:
    return page.get_by_role("complementary", name="Workspace")


def _open_side_chat(page: Page, expected_name: str) -> None:
    _rail(page).get_by_role("button", name="Open new", exact=True).click()
    page.get_by_role("menuitem", name="Side chat", exact=True).click()
    expect(_rail(page).get_by_role("tab", name=expected_name, exact=True)).to_be_visible()
    _hold(page, 400)


def _side_chat_tab(page: Page, name: str) -> Locator:
    return _rail(page).get_by_role("tab", name=name, exact=True)


def _expect_side_chat_order(page: Page, names: list[str]) -> None:
    tabs = _rail(page).locator(_TAB_SELECTOR)
    expect(tabs).to_have_count(len(names))
    for index, name in enumerate(names):
        expect(tabs.nth(index)).to_have_attribute("aria-label", name)


def _choose_from_tab_menu(page: Page, tab_name: str, item: str) -> None:
    _side_chat_tab(page, tab_name).click(button="right")
    menu_item = page.get_by_role("menuitem", name=item, exact=True)
    expect(menu_item).to_be_visible()
    _hold(page, 700)
    menu_item.click()


def test_closing_a_side_chat_keeps_the_other_names(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    side_chat_forks: list[str],
    mock_llm_server_url: str,
) -> None:
    """Closing Side chat 1 leaves Side chat 2 and 3 named as before."""
    base_url, session_id = seeded_session
    question = f"stable-names-{session_id}: what is in this repo?"
    reply = "This side chat answered from the mock model."
    configure_mock_llm(mock_llm_server_url, [{"text": reply}], match=question)

    page = _open_session(request, base_url, session_id)
    _track_forks(page, session_id, side_chat_forks)
    for number in (1, 2, 3):
        _open_side_chat(page, f"Side chat {number}")
    page.get_by_test_id("side-chat-input").fill(question)
    page.get_by_test_id("side-chat-send").click()
    expect(page.locator(".side-chat-backdrop").get_by_text(reply, exact=True)).to_be_visible(
        timeout=30_000
    )
    _expect_side_chat_order(page, ["Side chat 1", "Side chat 2", "Side chat 3"])
    _hold(page, 1_500)

    _rail(page).get_by_role("button", name="Close Side chat 1", exact=True).click()
    _expect_side_chat_order(page, ["Side chat 2", "Side chat 3"])
    _hold(page, 1_500)

    _open_side_chat(page, "Side chat 4")
    _expect_side_chat_order(page, ["Side chat 2", "Side chat 3", "Side chat 4"])
    _hold(page, 2_500)


def test_pinned_side_chat_stays_at_the_left(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    """Pinning a side chat moves it to the start of the strip and keeps it there."""
    base_url, session_id = seeded_session
    page = _open_session(request, base_url, session_id)
    for number in (1, 2, 3):
        _open_side_chat(page, f"Side chat {number}")
    _hold(page, 800)

    _choose_from_tab_menu(page, "Side chat 3", "Pin tab")
    _expect_side_chat_order(page, ["Side chat 3", "Side chat 1", "Side chat 2"])
    expect(_rail(page).get_by_role("button", name="Unpin Side chat 3", exact=True)).to_be_visible()
    _hold(page, 1_500)

    # A newly opened side chat joins the unpinned tabs; the pinned one stays first.
    _open_side_chat(page, "Side chat 4")
    _expect_side_chat_order(page, ["Side chat 3", "Side chat 1", "Side chat 2", "Side chat 4"])
    _hold(page, 1_500)

    page.reload()
    open_right_rail(page)
    _expect_side_chat_order(page, ["Side chat 3", "Side chat 1", "Side chat 2", "Side chat 4"])
    expect(_rail(page).get_by_role("button", name="Unpin Side chat 3", exact=True)).to_be_visible()
    _hold(page, 2_500)


def test_side_chat_tabs_reorder_by_drag_and_menu(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    """Dragging a tab, or Move left/right from its menu, changes the strip order."""
    base_url, session_id = seeded_session
    page = _open_session(request, base_url, session_id)
    for number in (1, 2, 3):
        _open_side_chat(page, f"Side chat {number}")
    _hold(page, 800)

    source = _side_chat_tab(page, "Side chat 3").bounding_box()
    target = _side_chat_tab(page, "Side chat 1").bounding_box()
    assert source is not None and target is not None
    page.mouse.move(source["x"] + source["width"] / 2, source["y"] + source["height"] / 2)
    page.mouse.down()
    page.mouse.move(
        target["x"] + target["width"] / 4, target["y"] + target["height"] / 2, steps=25
    )
    _hold(page, 400)
    page.mouse.up()
    _expect_side_chat_order(page, ["Side chat 3", "Side chat 1", "Side chat 2"])
    _hold(page, 1_500)

    _choose_from_tab_menu(page, "Side chat 1", "Move right")
    _expect_side_chat_order(page, ["Side chat 3", "Side chat 2", "Side chat 1"])
    _hold(page, 1_500)

    _side_chat_tab(page, "Side chat 3").click(button="right")
    expect(page.get_by_role("menuitem", name="Move left", exact=True)).to_be_disabled()
    _hold(page, 1_500)
    page.keyboard.press("Escape")

    page.reload()
    open_right_rail(page)
    _expect_side_chat_order(page, ["Side chat 3", "Side chat 2", "Side chat 1"])
    _hold(page, 2_500)


def test_side_chat_tabs_compact_as_they_accumulate(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    """From four side chats tabs show numbers; from seven only the active tab has Close."""
    base_url, session_id = seeded_session
    page = _open_session(request, base_url, session_id)
    tabs = _rail(page).locator("[data-side-chat-tab]")

    for number in (1, 2, 3):
        _open_side_chat(page, f"Side chat {number}")
    expect(_side_chat_tab(page, "Side chat 1")).to_have_text("Side chat 1")
    _hold(page, 1_200)

    _open_side_chat(page, "Side chat 4")
    expect(tabs.first).to_have_attribute("data-density", "compact")
    expect(_side_chat_tab(page, "Side chat 1")).to_have_text("1")
    expect(_rail(page).get_by_role("button", name="Close Side chat 1", exact=True)).to_be_visible()
    _hold(page, 1_500)

    for number in (5, 6, 7):
        _open_side_chat(page, f"Side chat {number}")
    expect(tabs.first).to_have_attribute("data-density", "dense")
    expect(_rail(page).get_by_role("button", name="Close Side chat 1", exact=True)).to_have_count(
        0
    )
    expect(_rail(page).get_by_role("button", name="Close Side chat 7", exact=True)).to_be_visible()
    _hold(page, 1_500)

    _open_side_chat(page, "Side chat 8")
    expect(_rail(page).locator(_TAB_SELECTOR)).to_have_count(8)
    _hold(page, 2_500)
