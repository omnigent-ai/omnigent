"""E2E: the composer must stay empty when a sent message actually reached the server.

Reported journey (mobile): send a message, background/reopen the app (or ride
a VPN blip), and the input field comes back pre-filled with the prompt that
was already sent and answered.

What backgrounding/VPN flapping does to the SPA is cut the network out from
under the in-flight send POST: the request reaches the server (the message is
persisted and the turn runs), but the client never sees the response. The
client treats that as a failed send and restores the text into the composer
(`failedSendDraft`), so the user ends up with a delivered, answered message
AND a composer pre-filled with the same prompt — priming a duplicate send.

This test drives that at a phone viewport: the send POST is forwarded to the
server (`route.fetch()`) and then aborted client-side (`route.abort()`), i.e.
delivered but unacknowledged. Once the delivered turn visibly renders (user
bubble + assistant reply arrive over the still-open stream), the composer must
be empty — on the buggy build it holds the already-sent prompt.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Route, ViewportSize, expect

from tests.e2e_ui.conftest import configure_mock_llm

# iPhone-12-class portrait viewport, matching tests/e2e_ui/mobile conventions.
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

_COMPOSER = 'textarea[aria-label="Message the agent"]'
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'

_PROMPT = "delivered-unacked-sentinel summarize the deploy status"
_REPLY = "deploy-status-summary-reply"


def _drop_ack_once(page: Page, session_id: str) -> list[int]:
    """Deliver the first matching send POST to the server, then abort it client-side.

    Simulates the app being backgrounded / VPN dropping right after the send
    request went out: the server processes the message, the client sees a
    network failure instead of the ack.

    :param page: Playwright page; the route is registered before the send.
    :param session_id: Session whose ``/events`` POST is intercepted.
    :returns: Single-element counter of dropped acks, for asserting the
        interception actually happened.
    """
    dropped = [0]

    def _handle(route: Route) -> None:
        if route.request.method != "POST" or _PROMPT not in (route.request.post_data or ""):
            route.continue_()
            return
        if dropped[0] > 0:
            route.continue_()
            return
        dropped[0] += 1
        route.fetch()
        route.abort("internetdisconnected")

    page.route(f"**/v1/sessions/{session_id}/events", _handle)
    return dropped


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_composer_stays_empty_after_delivered_but_unacked_send(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A delivered-but-unacknowledged send must not repopulate the composer."""
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _REPLY}],
        key="composer-after-unacked-send",
        match=_PROMPT,
    )

    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.locator(_COMPOSER)
    expect(composer).to_be_visible()

    dropped = _drop_ack_once(page, session_id)

    composer.fill(_PROMPT)
    page.get_by_role("button", name="Send", exact=True).click()

    # The message was delivered despite the client-side network failure: the
    # persisted user message and the assistant's reply arrive over the live
    # stream and render in the transcript.
    expect(page.locator(_USER_BUBBLE).filter(has_text=_PROMPT)).to_be_visible(timeout=30_000)
    expect(page.locator(_ASSISTANT_BUBBLE).filter(has_text=_REPLY)).to_be_visible(timeout=30_000)
    assert dropped[0] == 1, "the send POST was never intercepted; the fault was not injected"

    # The prompt was sent and answered, so the input field must be empty —
    # the buggy build restores the already-sent prompt into it.
    expect(composer).to_have_value("")
