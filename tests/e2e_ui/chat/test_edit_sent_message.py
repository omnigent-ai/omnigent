"""UI journeys: Edit on a sent message copies it into the composer.

The bubble's Edit button hands its text and attachments to the composer of
the same pane; the original bubble and history stay. Sending is an ordinary
new message, steered like any other when the agent is mid-turn. Component
tests cover the input matrix (pending, shared, read-only, failed downloads);
these drive the real server, runner and file store through the browser.
"""

from __future__ import annotations

import base64
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
_IMAGE_NAME = "chart.png"

# 240x180 PNG (same fixture as test_fork_preserves_image_attachment.py).
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAPAAAAC0CAIAAAAl/ja/AAABaUlEQVR42u3SQREAMAjAsDFdyEEdKvHAk0sk"
    "9BpZ/eCKLwGGBkODocHQGBoMDYYGQ4OhMTQYGgwNhgZDY2gwNBgaDA2GxtBgaDA0GBoMjaHB0GBoMDQYGkOD"
    "ocHQYGgwNIYGQ4OhwdAYGgwNhgZDg6ExNBgaDA2GBkNjaDA0GBoMDYbG0GBoMDQYGgyNocHQYGgwNBgaQ4Oh"
    "wdBgaDA0hgZDg6HB0GBoDA2GBkODoTE0GBoMDYYGQ2NoMDQYGgwNhsbQYGgwNBgaDI2hwdBgaDA0GBpDg6HB"
    "0GBoMDSGBkODocHQYGgMDYYGQ4OhMTQYGgwNhgZDY2gwNBgaDA2GxtBgaDA0GBoMjaHB0GBoMDQYGkODocHQ"
    "YGgwNIYGQ4OhwdBgaAwNhgZDg6HB0BgaDA2GBkNjaDA0GBoMDYbG0GBoMDQYGgyNocHQYGgwNBgaQ4OhwdBg"
    "aDA0hgZDg6HB0GBoDA2GBkPD3gAlVgKygGocMwAAAABJRU5ErkJggg=="
)


def _user_bubble(page: Page | Locator, text: str) -> Locator:
    return page.locator(_USER).filter(has_text=text)


def _click_edit(bubble: Locator) -> None:
    """Hover the bubble so its action row shows, then click its Edit button."""
    bubble.hover()
    edit = bubble.get_by_role("button", name="Edit", exact=True)
    expect(edit).to_be_visible()
    edit.click()


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label(_COMPOSER_LABEL)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _wait_idle(page: Page) -> None:
    expect(page.get_by_test_id("working-indicator")).to_have_count(0, timeout=60_000)


def _pause(page: Page) -> None:
    """Hold the frame briefly so the recorded state is readable."""
    page.wait_for_timeout(2500)


def test_edit_sent_message_with_image_and_send_revision(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """Edit copies text and image into the composer; the revision sends as a new turn."""
    base_url, session_id = seeded_session
    original = "edit-e2e-a1 Summarize the trend in this chart"
    revised = "edit-e2e-a2 Summarize the trend in this chart in one sentence"
    configure_mock_llm(
        mock_llm_server_url, [{"text": "The chart rises steadily."}], match="edit-e2e-a1"
    )
    configure_mock_llm(
        mock_llm_server_url, [{"text": "It climbs every month."}], match="edit-e2e-a2"
    )
    png = tmp_path / _IMAGE_NAME
    png.write_bytes(base64.b64decode(_PNG_B64))

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)

    page.locator('input[type="file"][accept*="image/"]').set_input_files(str(png))
    remove_chip = page.get_by_role("button", name=f"Remove {_IMAGE_NAME}")
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
    expect(remove_chip).to_be_visible(timeout=10_000)
    expect(page.locator(_USER)).to_have_count(1)
    _pause(page)

    composer.fill(revised)
    _pause(page)
    page.get_by_role("button", name="Send", exact=True).click()
    second = _user_bubble(page, revised)
    expect(second.locator("img").first).to_be_visible(timeout=30_000)
    expect(page.locator(_ASSISTANT, has_text="It climbs every month.")).to_be_visible(
        timeout=60_000
    )
    _wait_idle(page)
    # The original message is still in the transcript, unchanged.
    expect(page.locator(_USER)).to_have_count(2)
    expect(page.locator(_USER).first).to_contain_text(original)
    expect(composer).to_have_value("")
    _pause(page)


def test_edit_asks_before_replacing_a_draft(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """With a draft in the composer, Edit confirms; Keep keeps it, Replace swaps it."""
    base_url, session_id = seeded_session
    sent = "edit-e2e-b1 List three risks for the launch"
    draft = "An unfinished follow-up I don't want to lose"
    configure_mock_llm(
        mock_llm_server_url, [{"text": "Scope, schedule and staffing."}], match="edit-e2e-b1"
    )

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)
    _send(page, sent)
    expect(page.locator(_ASSISTANT, has_text="Scope, schedule and staffing.")).to_be_visible(
        timeout=60_000
    )
    _wait_idle(page)

    composer.fill(draft)
    _pause(page)
    bubble = _user_bubble(page, sent)
    dialog = page.get_by_role("dialog", name="Replace your draft?")

    _click_edit(bubble)
    expect(dialog).to_be_visible()
    _pause(page)
    dialog.get_by_role("button", name="Keep draft").click()
    expect(dialog).to_have_count(0)
    expect(composer).to_have_value(draft)
    _pause(page)

    _click_edit(bubble)
    expect(dialog).to_be_visible()
    _pause(page)
    dialog.get_by_role("button", name="Replace draft").click()
    expect(dialog).to_have_count(0)
    expect(composer).to_have_value(sent)
    expect(page.locator(_USER)).to_have_count(1)
    _pause(page)


def test_edit_while_agent_runs_does_not_stop_it_and_send_steers(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Edit mid-turn leaves the turn running; sending the revision steers it."""
    base_url, session_id = seeded_session
    original = "edit-e2e-c1 Draft the release announcement"
    revised = "edit-e2e-c2 Draft the release announcement for customers"
    # Title generation sends the same text and can take a gate of its own.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Here is an internal announcement.", "block": True}] * 2,
        match="edit-e2e-c1",
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Here is the customer announcement."}],
        match="edit-e2e-c2",
    )

    page: Page = request.getfixturevalue("page")
    page.add_init_script("localStorage.setItem('omnigent:background-session-titles', 'off')")
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
        for _ in range(2):
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
    question = f"edit-e2e-d1-{session_id} Why might this test be flaky?"
    configure_mock_llm(
        mock_llm_server_url, [{"text": "It races the network request."}], match="edit-e2e-d1"
    )

    page: Page = request.getfixturevalue("page")
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
        _pause(page)
    finally:
        if not page.is_closed():
            unroute()
        for child_id in child_ids:
            httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)
