"""UI journeys: Edit on a sent message copies it into the composer.

The bubble's Edit button copies its text (not its attachments) into the
composer of the same pane; the original bubble and history stay. Sending is
an ordinary new message, sent mid-turn like any other while the agent works.
Component tests cover the input matrix (draft confirmation, pending, other
authors, read-only); these drive the real server and runner through the browser.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests._helpers.polling import poll_until
from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER_LABEL = "Message the agent"
_USER = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_IMAGE = Path(__file__).resolve().parents[2] / "resources" / "test_image.png"
# Background titles would send the turn's text to the mock model and take its reply.
_TITLES_OFF = "localStorage.setItem('omnigent:background-session-titles', 'off')"


def _user_bubble(page: Page | Locator, text: str) -> Locator:
    return page.locator(_USER).filter(has_text=text)


def _click_edit(bubble: Locator) -> None:
    """Hover the bubble so its action row shows, then click its Edit button."""
    bubble.hover()
    edit = bubble.get_by_role("button", name="Edit and resend", exact=True)
    expect(edit).to_be_visible()
    edit.click()


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label(_COMPOSER_LABEL)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _wait_idle(page: Page) -> None:
    expect(page.get_by_test_id("working-indicator")).to_have_count(0, timeout=60_000)


def _pause(page: Page) -> None:
    """When recording, hold the frame briefly so the recorded state is readable."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(2500)


def test_edit_sent_message_copies_text_and_sends_revision(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Edit copies the text, not the image; the revision sends as a new turn."""
    base_url, session_id = seeded_session
    original = "edit-e2e-a1 Summarize the trend in this chart"
    revised = "edit-e2e-a2 Summarize the trend in this chart in one sentence"
    configure_mock_llm(
        mock_llm_server_url, [{"text": "The chart rises steadily."}], match="edit-e2e-a1"
    )
    configure_mock_llm(
        mock_llm_server_url, [{"text": "It climbs every month."}], match="edit-e2e-a2"
    )

    page: Page = request.getfixturevalue("page")
    page.add_init_script(_TITLES_OFF)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)

    page.locator('input[type="file"][accept*="image/"]').set_input_files(str(_IMAGE))
    remove_chip = page.get_by_role("button", name=f"Remove {_IMAGE.name}")
    expect(remove_chip).to_be_visible(timeout=10_000)
    _send(page, original)
    first = _user_bubble(page, original)
    expect(first.locator("img").first).to_be_visible(timeout=30_000)
    expect(page.locator(_ASSISTANT, has_text="The chart rises steadily.")).to_be_visible(
        timeout=60_000
    )
    _wait_idle(page)
    _pause(page)

    _click_edit(first)
    expect(composer).to_have_value(original)
    expect(composer).to_be_focused()
    # The agent already has the image from the original message.
    expect(remove_chip).to_have_count(0)
    expect(page.locator(_USER)).to_have_count(1)
    _pause(page)

    composer.fill(revised)
    _pause(page)
    page.get_by_role("button", name="Send", exact=True).click()
    second = _user_bubble(page, revised)
    expect(second).to_be_visible(timeout=30_000)
    expect(second.locator("img")).to_have_count(0)
    expect(page.locator(_ASSISTANT, has_text="It climbs every month.")).to_be_visible(
        timeout=60_000
    )
    _wait_idle(page)
    # The original message is still in the transcript, unchanged.
    expect(page.locator(_USER)).to_have_count(2)
    expect(page.locator(_USER).first).to_contain_text(original)
    expect(first.locator("img").first).to_be_visible()
    expect(composer).to_have_value("")
    _pause(page)


def test_edit_while_agent_runs_does_not_stop_it_and_sends_mid_turn(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Edit mid-turn leaves the turn running, and the revision sends while it runs.

    What the runtime does with a mid-turn message is covered by test_always_steer.
    """
    base_url, session_id = seeded_session
    original = "edit-e2e-c1 Draft the release announcement"
    revised = "edit-e2e-c2 Draft the release announcement for customers"
    # Titles are off, so turn c1 is the only request that can match.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Here is an internal announcement.", "block": True}],
        match="edit-e2e-c1",
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Here is the customer announcement."}],
        match="edit-e2e-c2",
    )

    page: Page = request.getfixturevalue("page")
    page.add_init_script(_TITLES_OFF)
    try:
        page.goto(f"{base_url}/c/{session_id}")
        composer = page.get_by_label(_COMPOSER_LABEL)
        expect(composer).to_be_visible(timeout=30_000)
        _send(page, original)
        poll_until(
            lambda: httpx.get(f"{mock_llm_server_url}/gate/pending", timeout=5.0).json()[
                "pending"
            ],
            timeout_s=30.0,
            interval_s=0.25,
        )
        working = page.get_by_test_id("working-indicator")
        expect(working).to_be_visible(timeout=30_000)
        _pause(page)

        _click_edit(_user_bubble(page, original))
        expect(composer).to_have_value(original)
        # Edit doesn't stop the turn.
        expect(working).to_be_visible()
        _pause(page)

        composer.fill(revised)
        page.get_by_role("button", name="Send", exact=True).click()
        expect(_user_bubble(page, revised)).to_be_visible(timeout=30_000)
        expect(composer).to_have_value("")
        expect(working).to_be_visible()
        _pause(page)
    finally:
        httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0)

    expect(page.locator(_ASSISTANT, has_text="Here is the customer announcement.")).to_be_visible(
        timeout=60_000
    )
    _wait_idle(page)
    expect(page.locator(_USER)).to_have_count(2)
    _pause(page)


def _track_side_chat_forks(page: Page, session_id: str) -> tuple[list[str], Callable[[], None]]:
    """Record side-chat forks of *session_id*; returns the ids and an unroute callback."""
    child_ids: list[str] = []
    pattern = f"**/v1/sessions/{session_id}/fork"

    def track_fork(route: Route) -> None:
        response = route.fetch()
        if response.ok:
            child_ids.append(response.json()["id"])
        route.fulfill(response=response)

    page.route(pattern, track_fork)
    return child_ids, lambda: page.unroute(pattern, track_fork)


def test_edit_in_side_chat_fills_the_side_chat_composer(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Edit on a side chat's message fills that side chat's composer, not the main one."""
    base_url, session_id = seeded_session
    marker = f"edit-e2e-d1-{session_id}"
    question = f"{marker} Why might this test be flaky?"
    configure_mock_llm(
        mock_llm_server_url, [{"text": "It races the network request."}], match=marker
    )

    page: Page = request.getfixturevalue("page")
    page.add_init_script(_TITLES_OFF)
    child_ids, unroute = _track_side_chat_forks(page, session_id)
    try:
        page.goto(f"{base_url}/c/{session_id}")
        main_composer = page.get_by_label(_COMPOSER_LABEL)
        expect(main_composer).to_be_visible(timeout=30_000)

        page.get_by_test_id("composer-attach").click()
        page.get_by_role("menuitem", name="Start a new side chat").click()
        side_input = page.get_by_test_id("side-chat-input")
        expect(side_input).to_be_enabled(timeout=30_000)
        side_input.fill(question)
        page.get_by_test_id("side-chat-send").click()
        pane = page.locator(".side-chat-backdrop")
        expect(pane.get_by_text("It races the network request.", exact=True)).to_be_visible(
            timeout=30_000
        )
        expect(pane.get_by_test_id("working-indicator")).to_have_count(0, timeout=30_000)
        expect(side_input).to_have_value("", timeout=30_000)
        _pause(page)

        _click_edit(_user_bubble(pane, question))
        expect(side_input).to_have_value(question)
        expect(side_input).to_be_focused()
        expect(main_composer).to_have_value("")
        _pause(page)
    finally:
        if not page.is_closed():
            unroute()
        for child_id in child_ids:
            httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)
