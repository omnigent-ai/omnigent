"""Side chat on sessions whose forks the deployment refuses.

A generic side chat forks its parent server-side, so a session whose Fork action
is unsupported cannot start one. The managed-sandbox marker is set on a real
session through the product API; the fork refusal itself is answered at the
browser boundary because the core server forks every session.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm, open_right_rail

FORK_UNSUPPORTED = "Forking this sandbox session is not supported yet."
SIDE_CHAT_UNSUPPORTED = "Side chat forks this sandbox session, which is not supported yet."
FORK_REFUSED = "Session forks are disabled for managed sandbox sessions on this deployment."
COMPOSER_PLACEHOLDER = "Send a message…"
ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'


@pytest.fixture
def managed_session(seeded_session: tuple[str, str]) -> tuple[str, str]:
    """The seeded runner-bound session, marked the way a managed deployment marks it."""
    base_url, session_id = seeded_session
    response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"labels": {"omnigent.host_type": "managed"}},
        timeout=10.0,
    )
    response.raise_for_status()
    return seeded_session


def refuse_forks(page: Page, session_id: str, forks: list[dict[str, object]]) -> None:
    """Answer the parent's fork POST like a deployment that disallows managed forks."""

    def refuse(route: Route) -> None:
        forks.append(route.request.post_data_json)
        route.fulfill(
            status=400,
            content_type="application/json",
            body=json.dumps({"error": {"code": "invalid_input", "message": FORK_REFUSED}}),
        )

    page.route(f"**/v1/sessions/{session_id}/fork", refuse)


def close_menu(page: Page) -> None:
    # A tooltip's dismissable layer stays mounted while it animates out and eats the
    # next Escape, so wait for it before closing the menu itself.
    page.keyboard.press("Escape")
    expect(page.get_by_role("tooltip")).to_have_count(0)
    if page.get_by_role("menu").count() > 0:
        page.keyboard.press("Escape")
    expect(page.get_by_role("menu")).to_have_count(0)


def expect_fork_action_unsupported(page: Page) -> None:
    page.get_by_test_id("header-conversation-actions").click()
    fork = page.get_by_test_id("header-fork-conversation")
    expect(fork).to_be_disabled()
    fork.hover()
    expect(page.get_by_role("tooltip").filter(has_text=FORK_UNSUPPORTED)).to_be_visible()
    close_menu(page)


def _expect_offer_disabled(page: Page, item: Locator) -> None:
    """A listed side-chat entry stays in the menu but disabled, naming the reason on hover."""
    expect(item).to_be_visible()
    expect(item).to_be_disabled()
    item.hover()
    expect(page.get_by_role("tooltip").filter(has_text=SIDE_CHAT_UNSUPPORTED)).to_be_visible()


def test_side_chat_entry_points_follow_fork_restriction(
    request: pytest.FixtureRequest, managed_session: tuple[str, str]
) -> None:
    """Listed side-chat entries stay disabled with a reason; the slash suggestion is dropped."""
    base_url, session_id = managed_session
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(COMPOSER_PLACEHOLDER)
    expect(composer).to_be_visible()
    expect_fork_action_unsupported(page)

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new", exact=True).click()
    expect(page.get_by_role("menu")).to_be_visible()
    _expect_offer_disabled(page, page.get_by_role("menuitem", name="Side chat", exact=True))
    close_menu(page)

    page.get_by_test_id("composer-attach").click()
    expect(page.get_by_role("menu")).to_be_visible()
    _expect_offer_disabled(page, page.get_by_role("menuitem", name="Start a new side chat"))
    close_menu(page)

    composer.fill("/")
    # /help is ungated, so it proves the command menu opened; /side is dropped from
    # the command map when side chat is unavailable, so its absence is a withheld entry.
    expect(page.get_by_test_id("slash-menu-item-help")).to_be_visible()
    expect(page.get_by_test_id("slash-menu-item-side")).to_have_count(0)


def test_restricted_session_withholds_ask_in_side_chat(
    request: pytest.FixtureRequest,
    managed_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Selecting assistant text on a fork-restricted session omits Ask in side chat."""
    base_url, session_id = managed_session
    token = f"restricted-popup-{session_id}"
    question = f"{token}: say something quotable."
    reply = f"{token}: here is a quotable reply."
    configure_mock_llm(mock_llm_server_url, [{"text": reply}], key=token, match=token)
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(COMPOSER_PLACEHOLDER)
    expect(composer).to_be_visible()
    composer.fill(question)
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator(ASSISTANT_BUBBLE).filter(has_text=reply)
    expect(assistant).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("working-indicator")).to_have_count(0, timeout=30_000)

    assistant.get_by_text(reply).select_text()
    # Reply always survives, so its popup button proves the selection action bar rendered;
    # Ask in side chat is withheld because this session's forks are unsupported.
    expect(page.get_by_role("button", name=re.compile(r"^Reply"))).to_be_visible()
    expect(page.get_by_role("button", name="Ask in side chat", exact=True)).to_have_count(0)


def test_refused_side_chat_fork_explains_refusal(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    """A fork the server refuses leaves the question in place and shows the server's reason."""
    base_url, session_id = seeded_session
    forks: list[dict[str, object]] = []
    page: Page = request.getfixturevalue("page")
    refuse_forks(page, session_id, forks)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(COMPOSER_PLACEHOLDER)).to_be_visible()

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new", exact=True).click()
    page.get_by_role("menuitem", name="Side chat", exact=True).click()
    expect(rail.get_by_role("tab", name="Side chat 1", exact=True)).to_be_visible()
    question = f"refused-side-{session_id}: can this session fork?"
    page.get_by_test_id("side-chat-input").fill(question)
    page.get_by_test_id("side-chat-send").click()

    expect(page.get_by_text("Couldn't start a side chat for this session.")).to_be_visible()
    assert forks == [{"title": "Side chat", "side_chat": True}]
    expect(page.get_by_test_id("side-chat-input")).to_have_value(question)
    expect(page.get_by_text(FORK_REFUSED)).to_be_visible()
