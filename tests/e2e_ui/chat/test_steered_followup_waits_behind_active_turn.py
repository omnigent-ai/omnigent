"""E2E: a follow-up pushed forward mid-turn must not read as the active request.

While the first turn is still running, the queued strip's "Send now" POSTs the
follow-up. The runner buffers it behind the active turn, but the SPA renders it
as a normal sent bubble with the Working… indicator beneath it, so the UI
reports the follow-up as the request being processed while the first request
is still executing.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

import httpx
import pytest
from playwright.sync_api import Page, expect

_COMPOSER_LABEL = "Message the agent"
_FIRST_PROMPT = "Inspect the workspace and tell me what is in it."
_FOLLOW_UP = "sentinel-steer-followup-7291 push this prompt forward"
_FINAL_REPLY = "Workspace inspected."
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_QUEUED_STRIP = '[data-testid="composer-queued-strip"]'
_QUEUE_STATE_RE = r"queued|waiting|pending"


def _wait_for(page: Page, predicate: Callable[[], bool], *, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _gate_pending(mock_url: str) -> bool:
    resp = httpx.get(f"{mock_url}/gate/pending", timeout=5.0)
    resp.raise_for_status()
    return bool(resp.json()["pending"])


def _send(page: Page, text: str) -> None:
    page.get_by_label(_COMPOSER_LABEL).fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _follow_up_presented_as_active(page: Page) -> bool:
    """Whether the follow-up reads as the request being processed.

    True when it is the last user bubble, the Working… indicator follows it in
    the transcript, and neither the bubble nor the queued strip marks it as
    still waiting.
    """
    return bool(
        page.evaluate(
            """([bubbleSel, workingSel, stripSel, text, stateRe]) => {
              const bubbles = [...document.querySelectorAll(bubbleSel)];
              const last = bubbles[bubbles.length - 1];
              if (!last || !last.innerText.includes(text)) return false;
              const working = document.querySelector(workingSel);
              if (!working) return false;
              const follows = Boolean(
                last.compareDocumentPosition(working) & Node.DOCUMENT_POSITION_FOLLOWING,
              );
              const strip = document.querySelector(stripSel);
              const stillQueued = strip !== null && strip.innerText.includes(text);
              const marked = new RegExp(stateRe, "i").test(last.innerText);
              return follows && !stillQueued && !marked;
            }""",
            [_USER_BUBBLE, _WORKING, _QUEUED_STRIP, _FOLLOW_UP, _QUEUE_STATE_RE],
        )
    )


def _working_sits_above_follow_up(page: Page) -> bool:
    """Whether exactly one Working… indicator is visible above the follow-up.

    The fix keeps the shimmer with the active turn, so while the first request
    runs there is exactly one visible indicator and it precedes the steered
    follow-up. Visibility is checked explicitly: a hidden or vanished indicator
    (count 0) must not read as success.
    """
    return bool(
        page.evaluate(
            """([workingSel, bubbleSel, followUp]) => {
              const visible = (el) => el.checkVisibility() && el.getClientRects().length > 0;
              const workings = [...document.querySelectorAll(workingSel)].filter(visible);
              if (workings.length !== 1) return false;
              const followUpBubble = [...document.querySelectorAll(bubbleSel)].find((b) =>
                b.innerText.includes(followUp),
              );
              if (!followUpBubble) return false;
              return Boolean(
                workings[0].compareDocumentPosition(followUpBubble) &
                  Node.DOCUMENT_POSITION_FOLLOWING,
              );
            }""",
            [_WORKING, _USER_BUBBLE, _FOLLOW_UP],
        )
    )


def test_steered_followup_is_not_presented_as_active_while_first_turn_runs(
    request: pytest.FixtureRequest,
    paused_mid_turn_session: tuple[str, str, str],
    output_path: str,
) -> None:
    base_url, session_id, mock_url = paused_mid_turn_session
    print(f"product_session_id={session_id}")
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)

    _send(page, _FIRST_PROMPT)
    # The first turn has run its `ls` tool and is now blocked on its next model call.
    _wait_for(page, lambda: _gate_pending(mock_url))
    expect(page.locator(_WORKING)).to_be_visible()

    _send(page, _FOLLOW_UP)
    strip = page.locator(_QUEUED_STRIP)
    expect(strip).to_contain_text(_FOLLOW_UP)
    page.get_by_role("button", name="Send queued message now").click()

    follow_up = page.locator(_USER_BUBBLE).filter(has_text=_FOLLOW_UP)
    expect(follow_up).to_be_visible(timeout=10_000)
    expect(strip).to_have_count(0)
    page.wait_for_timeout(3_000)
    assert _gate_pending(mock_url), "the first request must still be executing here"
    presented_as_active = _follow_up_presented_as_active(page)
    working_above_follow_up = _working_sits_above_follow_up(page)
    follow_up_item_id = follow_up.get_attribute("data-message-id")
    page.screenshot(path=os.path.join(output_path, "follow-up-while-first-turn-runs.png"))

    httpx.post(f"{mock_url}/gate/release", timeout=5.0).raise_for_status()
    first_reply = page.locator(_ASSISTANT_BUBBLE).filter(has_text=_FINAL_REPLY)
    expect(first_reply.first).to_be_visible(timeout=60_000)
    page.wait_for_timeout(2_000)
    page.screenshot(path=os.path.join(output_path, "first-reply-after-release.png"))
    expect(page.locator(_WORKING)).to_be_hidden(timeout=60_000)
    page.wait_for_timeout(1_500)
    print(
        f"follow_up_item_id={follow_up_item_id} presented_as_active={presented_as_active} "
        f"working_above_follow_up={working_above_follow_up}"
    )

    assert not presented_as_active, (
        "the follow-up was shown as the request being processed (committed bubble "
        f"{follow_up_item_id!r} followed by the Working… indicator, no queued marker) "
        "while the first request was still executing"
    )
    assert working_above_follow_up, (
        "while the first request runs, exactly one Working… indicator must stay "
        "visible with the active turn, above the steered follow-up — not beneath it "
        "and not gone"
    )
