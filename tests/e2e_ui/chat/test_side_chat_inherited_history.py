"""A side chat never renders its parent's transcript, even when the pane is left
while its first transcript page is still loading; a held request stands in for a
slow deployment. Staying on the pane is covered by ``test_side_chat_entrypoints``."""

from __future__ import annotations

import contextlib
import re
import time

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm, open_right_rail

from .test_side_chat_entrypoints import _ASSISTANT, _items, _send_parent

_USER = '[data-testid="message-bubble"][data-role="user"]'
_ITEMS_URL = re.compile(r"/v1/sessions/([^/]+)/items\?")


def test_side_chat_left_while_loading_hides_parent_transcript(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Leaving the pane mid-load must not surface the parent's turns, now or after a reload."""
    base_url, parent_id = seeded_session
    # Created after the session exists so a recorded run films only the journey.
    page: Page = request.getfixturevalue("page")
    marker = f"caveats-{parent_id[:8]}"
    parent_turns = [
        (f"main-{parent_id}-1: summarize what we found", f"Reply one {parent_id[:8]}."),
        (
            f"main-{parent_id}-2: list the important caveats",
            f"Important caveats {marker} (we flagged these at the time)\n\n"
            "- It's a proxy, not a true lineage join; the rows don't carry a parent id.\n"
            "- Because of that, the fan-out bucket's rate was inflated.",
        ),
    ]
    # Longer than the parent-turn tokens so the mock routes the side turn to its own queue.
    side_token = f"sidechat-question-{parent_id}"
    side_question = f"{side_token}: answer this side question"
    side_reply = f"Side answer {parent_id[:8]}."
    child_ids: list[str] = []
    held: list[Route] = []

    def track_fork(route: Route) -> None:
        response = route.fetch()
        if response.ok:
            child_ids.append(response.json()["id"])
        route.fulfill(response=response)

    def hold_first_child_page(route: Route) -> None:
        match = _ITEMS_URL.search(route.request.url)
        if not held and match is not None and match.group(1) in child_ids:
            held.append(route)
            return
        route.continue_()

    page.route(f"**/v1/sessions/{parent_id}/fork", track_fork)
    page.route(_ITEMS_URL, hold_first_child_page)
    try:
        page.goto(f"{base_url}/c/{parent_id}")
        for index, (question, reply) in enumerate(parent_turns, start=1):
            key = f"main-{parent_id}-{index}"
            configure_mock_llm(mock_llm_server_url, [{"text": reply}], key=key, match=key)
            _send_parent(page, question, reply.splitlines()[0])
        parent_items = _items(base_url, parent_id)
        assert str(parent_items).count(marker) == 1

        configure_mock_llm(
            mock_llm_server_url, [{"text": side_reply}], key=side_token, match=side_token
        )
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")
        rail.get_by_role("button", name="Open new", exact=True).click()
        page.get_by_role("menuitem", name="Side chat", exact=True).click()
        page.get_by_test_id("side-chat-input").fill(side_question)
        page.get_by_test_id("side-chat-send").click()

        deadline = time.monotonic() + 60
        while not held and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        assert held, "the side chat never requested its first transcript page"
        held_match = _ITEMS_URL.search(held[0].request.url)
        assert held_match is not None
        child_id = held_match.group(1)
        # Leave the pane while that page is in flight, then let it land off screen.
        rail.get_by_role("button", name="Open new", exact=True).click()
        page.get_by_role("menuitem", name="Side chat", exact=True).click()
        expect(rail.get_by_role("tab", name="Side chat 2", exact=True)).to_be_visible()
        with page.expect_response(lambda r: f"/v1/sessions/{child_id}/items" in r.url):
            held[0].continue_()
        # Wait for the OFF-SCREEN load to persist its boundary while Side chat 1
        # stays unmounted, so the later assertions exercise that path rather than
        # the on-screen capture.
        boundary_key = f"omnigent.sideChatInherited:{child_id}"
        deadline = time.monotonic() + 30
        while not page.evaluate("(k) => localStorage.getItem(k)", boundary_key):
            assert time.monotonic() < deadline, "off-screen load never persisted the boundary"
            page.wait_for_timeout(100)

        rail.get_by_role("tab", name="Side chat 1", exact=True).click()
        pane = page.locator(".side-chat-backdrop")
        expect(pane).to_be_visible()
        expect(pane.get_by_text(marker)).to_have_count(0)
        expect(pane.locator(_ASSISTANT).filter(has_text=side_reply)).to_be_visible(timeout=30_000)
        expect(pane.get_by_test_id("working-indicator")).to_have_count(0, timeout=30_000)
        expect(pane.get_by_text(marker)).to_have_count(0)
        expect(pane.locator(_USER)).to_have_count(1)
        expect(pane.locator(_USER).filter(has_text=side_question)).to_have_count(1)
        expect(pane.locator(_ASSISTANT)).to_have_count(1)
        assert _items(base_url, parent_id) == parent_items

        # The persisted boundary must survive a reload without hiding the side chat's own turn.
        page.reload()
        open_right_rail(page)
        page.get_by_role("tab", name="Side chat 1", exact=True).click()
        expect(pane.locator(_ASSISTANT).filter(has_text=side_reply)).to_be_visible(timeout=30_000)
        expect(pane.get_by_text(marker)).to_have_count(0)
        expect(pane.locator(_USER)).to_have_count(1)
        expect(pane.locator(_USER).filter(has_text=side_question)).to_have_count(1)
        expect(pane.locator(_ASSISTANT)).to_have_count(1)
    finally:
        # Release any still-held route so a mid-test failure can't hang teardown.
        for route in held:
            with contextlib.suppress(Exception):
                route.continue_()
        for child_id in child_ids:
            with contextlib.suppress(Exception):
                httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)
