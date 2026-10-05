"""E2E: the composer stays empty once a sent message has reached the server.

Backgrounding the app or a VPN drop cuts the network under the in-flight send
POST: the server persists the message and answers, but the client never sees the
acknowledgement and must not hand the prompt back to the composer. Both journeys
run at a phone profile (desktop Chromium standing in for the native WebView).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm

_PHONE = pytest.mark.browser_context_args(
    viewport={"width": 390, "height": 844},
    is_mobile=True,
    has_touch=True,
)

_COMPOSER = 'textarea[aria-label="Message the agent"]'
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'

_UNACKED_PROMPT = "delivered-unacked-sentinel summarize the deploy status"
_UNACKED_REPLY = "deploy-status-summary-reply"
_ACKED_PROMPT = "acked-sentinel list the open incidents"
_ACKED_REPLY = "open-incident-list-reply"

_TURN_TIMEOUT_MS = 30_000


def _drop_ack_once(page: Page, session_id: str, prompt: str) -> list[int]:
    """Deliver the first send POST carrying ``prompt`` to the server, then abort it client-side.

    :returns: Single-element dropped-ack counter, so the test can assert the fault was injected.
    """
    dropped = [0]

    def _handle(route: Route) -> None:
        if (
            dropped[0] > 0
            or route.request.method != "POST"
            or prompt not in (route.request.post_data or "")
        ):
            route.continue_()
            return
        dropped[0] += 1
        route.fetch()
        route.abort("internetdisconnected")

    page.route(f"**/v1/sessions/{session_id}/events", _handle)
    return dropped


def _open_session(page: Page, base_url: str, session_id: str) -> Locator:
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.locator(_COMPOSER)
    expect(composer).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    return composer


def _send(page: Page, composer: Locator, prompt: str) -> None:
    composer.tap()
    composer.fill(prompt)
    page.get_by_role("button", name="Send", exact=True).tap()


def _expect_turn_rendered(page: Page, prompt: str, reply: str) -> None:
    expect(page.locator(_USER_BUBBLE).filter(has_text=prompt)).to_be_visible(
        timeout=_TURN_TIMEOUT_MS
    )
    expect(page.locator(_ASSISTANT_BUBBLE).filter(has_text=reply)).to_be_visible(
        timeout=_TURN_TIMEOUT_MS
    )


def _reopen(page: Page, prompt: str) -> Locator:
    page.reload()
    composer = page.locator(_COMPOSER)
    expect(composer).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    expect(page.locator(_USER_BUBBLE).filter(has_text=prompt)).to_be_visible(
        timeout=_TURN_TIMEOUT_MS
    )
    return composer


@_PHONE
def test_composer_stays_empty_after_delivered_but_unacked_send(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A delivered-but-unacknowledged send must not repopulate the composer."""
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _UNACKED_REPLY}],
        key="composer-after-unacked-send",
        match=_UNACKED_PROMPT,
    )

    composer = _open_session(page, base_url, session_id)
    dropped = _drop_ack_once(page, session_id, _UNACKED_PROMPT)
    _send(page, composer, _UNACKED_PROMPT)

    # The message was delivered despite the client-side network failure: the
    # persisted user message and the reply arrive over the live stream.
    _expect_turn_rendered(page, _UNACKED_PROMPT, _UNACKED_REPLY)
    assert dropped[0] == 1, (
        "the send POST was never intercepted; the network drop was not injected"
    )

    # The prompt was sent and answered, so the input field must be empty.
    expect(composer).to_have_value("")

    composer = _reopen(page, _UNACKED_PROMPT)
    expect(composer).to_have_value("")


@_PHONE
def test_composer_stays_empty_after_reopen_following_acked_send(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Reopening after a normally acknowledged send must not restore the sent prompt."""
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _ACKED_REPLY}],
        key="composer-after-acked-send",
        match=_ACKED_PROMPT,
    )

    composer = _open_session(page, base_url, session_id)
    _send(page, composer, _ACKED_PROMPT)
    _expect_turn_rendered(page, _ACKED_PROMPT, _ACKED_REPLY)
    expect(composer).to_have_value("")

    composer = _reopen(page, _ACKED_PROMPT)
    expect(composer).to_have_value("")
