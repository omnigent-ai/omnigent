"""E2E: a transient 404 on stream-open self-heals instead of failing the session.

A reverse proxy in front of the server serves 404 for the stream route for the
~10-60s a backend container takes to restart (upgrade, config change, re-seed
bounce). Before this fix the client treated any 404 on stream-open as
permanent and flipped the session to "failed", requiring a manual page reload
to recover. This test simulates a restart window by 404-ing the stream open a
few times before letting it through, and asserts the turn still completes
with no failure banner -- proving the client retried through the transient
window instead of giving up on the first 404.
"""

from __future__ import annotations

from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Ask the agent anything…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# Well under chatStore.ts's MAX_TRANSIENT_404_RETRIES (10) -- proves the loop
# recovers comfortably within its budget, not just barely under the cap.
_TRANSIENT_404_COUNT = 3


def _fail_stream_then_recover(page: Page, session_id: str) -> list[int]:
    """404 the session's first few stream-open attempts, then let them through.

    :param page: Playwright page, registered before navigation.
    :param session_id: Session whose ``/stream`` route is intercepted.
    :returns: A single-element list tracking the intercepted-attempt count,
        mutable so the caller can assert on it once the page has settled.
    """
    attempts = [0]

    def _handle(route: Route) -> None:
        if urlparse(route.request.url).path != f"/v1/sessions/{session_id}/stream":
            route.continue_()
            return
        attempts[0] += 1
        if attempts[0] <= _TRANSIENT_404_COUNT:
            route.fulfill(status=404, content_type="text/plain", body="Not Found")
            return
        route.continue_()

    page.route(f"**/v1/sessions/{session_id}/stream*", _handle)
    return attempts


def test_transient_stream_404_recovers_without_manual_reload(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A few 404s on stream-open must not flip the session to "failed".

    Regression test for the bug where a reverse-proxy 404 during a backend
    restart was treated identically to a permanently-gone conversation.
    """
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "the stream is back"}],
        key="stream-404-recover",
        match="Say hello",
    )

    attempts = _fail_stream_then_recover(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()

    # Let the retry loop run its course BEFORE sending anything: the old
    # code gave up and flipped to "failed" on the very first 404, so
    # `attempts` would freeze at 1 forever. Waiting here (rather than
    # sending a message immediately) also avoids racing the turn's reply
    # against the still-reconnecting stream -- this is a first-ever
    # connect, not a reconnect, so the client has no backlog replay for
    # events emitted before it's actually listening.
    #
    # Poll via page.wait_for_timeout(), NOT time.sleep(): Playwright's sync
    # API dispatches route/console callbacks cooperatively on the same
    # thread, so a bare time.sleep() here would starve our own route
    # handler and never let `attempts` advance.
    for _ in range(100):
        if attempts[0] > _TRANSIENT_404_COUNT:
            break
        page.wait_for_timeout(100)
    assert attempts[0] > _TRANSIENT_404_COUNT, (
        f"stream never recovered from the transient 404s within 10s "
        f"(attempts={attempts[0]}) -- the old code gives up on the first 404"
    )

    # No failed-session banner anywhere on the page.
    expect(page.get_by_text("stream unavailable", exact=False)).to_have_count(0)

    # The connection is healthy again -- prove the chat is still fully usable,
    # not just that the retry counter moved.
    composer.fill("Say hello")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=15_000)
