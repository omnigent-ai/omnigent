"""E2E regression guard: a silent OAuth-401 on the stream must not blank a turn.

Bug: a turn is running on the host; the browser's session event stream
reconnects while the user's OAuth token has quietly expired, so the stream
open returns 401. The running turn's reply then never renders in the live
view AND no error is surfaced — the turn area stays blank while the agent's
answer is silently dropped. One reporter traced their own "empty response"
case to exactly this silent 401 token expiry.

Root cause the journey exercises: on a cold reconnect the store has no locally
initiated ``activeResponse`` yet, so the stream-401 handler's
``finalizeActive(set, "failed", "stream unavailable (401)", null)`` is a no-op
(``activeResponse === null && !responseIdOverride`` returns ``{}``). The pump
then settles ``status`` to idle and gives up without appending any visible
error block or re-fetching the transcript — so the user sees neither the reply
nor an error (chatStore.ts, the 401 branch of ``startStreamPump``).

Journey driven here, on the real web SPA against a live server + runner with
the agent's model pointed at the mock LLM:

1. open a running chat session and send a message; the agent starts a turn
   whose model call is gated open (in flight on the host)
2. the OAuth token expires: every session-stream (re)open now returns 401. A
   reload models the tab's stream reconnecting after the token went stale.
3. the pump hits the 401 and gives up
4. the host-side turn completes and commits the reply
5. observable failure: the turn area is blank — no assistant reply is rendered
   AND no error is surfaced (only a console warning). The committed reply is
   reachable, proving it was silently dropped from the live view rather than
   lost.

Regression assertion (FAILS on the unfixed build, passes once the 401 path
gives the user *something* — the reply via a re-fetch, or a visible error):
after the reply has committed on the host, the still-connected page must show
either the reply text or a visible error affordance, not a silent blank turn.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_ERROR_PILL = '[data-testid="error-pill"]'


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _wait_gate_pending(page: Page, mock_url: str) -> None:
    """Block until the mock LLM has a request parked on its gate.

    Proves the turn's model call is actually in flight on the host before we
    sever the browser's view of it, so the reply we later assert on is one the
    server is genuinely producing.
    """
    for _ in range(150):
        if httpx.get(f"{mock_url}/gate/pending", timeout=5).json().get("pending"):
            return
        page.wait_for_timeout(200)
    raise AssertionError("mock LLM gate never went pending — model call not in flight")


@pytest.mark.timeout(600)
def test_silent_stream_401_does_not_blank_the_turn(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A 401 on stream reconnect must not silently blank a completing turn."""
    base_url, session_id = seeded_session
    logs: list[str] = []
    page.on("console", lambda m: logs.append(f"[{m.type}] {m.text}"))

    uid = uuid.uuid4().hex[:6]
    token = f"silent401-{uid}"
    reply = f"REPLY-THAT-VANISHES-{uid}"
    # Gate the model call so the turn stays in flight on the host while we
    # 401 the browser's event stream; the reply commits only after we release.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": reply, "block": True}],
        key=f"silent401-{uid}",
        match=token,
    )

    armed = [False]

    def handle(route: Route) -> None:
        # Once the token has "expired", every session-stream open is 401'd.
        if armed[0]:
            route.fulfill(status=401, content_type="text/plain", body="Unauthorized")
            return
        route.continue_()

    page.route(f"**/v1/sessions/{session_id}/stream*", handle)

    # 1. Send a message; the agent starts a turn (model call gated in flight).
    page.goto(f"{base_url}/c/{session_id}")
    _send(page, f"Answer me. {token}")
    expect(page.locator(_WORKING)).to_be_visible(timeout=30_000)
    _wait_gate_pending(page, mock_llm_server_url)

    # 2. The OAuth token expires: reconnect the stream (reload) so its open
    #    hits the 401 path.
    armed[0] = True
    page.reload()
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

    # 3. Wait for the pump to reach the 401 give-up branch — the synchronization
    #    point that proves we exercised the reported code path.
    for _ in range(150):
        if any("giving up" in line for line in logs):
            break
        page.wait_for_timeout(100)
    assert any("giving up" in line for line in logs), (
        "stream never reached the 401 give-up branch; the journey did not "
        f"exercise the reported path. Console: {logs[-8:]}"
    )

    # 4. The host-side turn completes and commits the reply.
    released = httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5).json()
    assert released.get("released"), "mock LLM gate was not released"
    # Give the store ample time to render the reply IF it re-fetches / recovers.
    page.wait_for_timeout(6_000)

    # 5. Reproduction assertion: after the reply has committed on the host, a
    #    user staring at this (still-connected) page must see SOMETHING — the
    #    reply, or a visible error explaining the blank turn. On the unfixed
    #    build they see neither: the assistant area is blank and no error pill
    #    renders (only a swallowed console warning), so this assertion FAILS —
    #    that blank, silent turn IS the bug. A fix that appends a visible error
    #    block on the 401 path, or that re-fetches the committed reply, flips
    #    this green.
    assistant = page.locator(_ASSISTANT)
    reply_visible = reply in page.locator("body").inner_text()
    error_visible = page.locator(_ERROR_PILL).count() > 0
    assert reply_visible or error_visible, (
        "silent-401 regression: after the turn's reply committed on the host, "
        "the reconnected page shows neither the reply nor any visible error — "
        f"a blank, silent turn. assistant_bubbles={assistant.count()} "
        f"reply_visible={reply_visible} "
        f"error_pills={page.locator(_ERROR_PILL).count()}"
    )
