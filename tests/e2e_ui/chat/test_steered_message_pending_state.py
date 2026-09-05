"""E2E: a steered message must show an intermediate state until consumed.

Bug this guards against: a follow-up steered into a running turn ("Send now" on the queued
strip, or the always-steer preference) immediately renders in the
transcript as a full-strength user bubble — visually identical to a
message the harness already consumed — even while the agent loop is
still parked mid-turn and demonstrably has NOT consumed it. There is no
intermediate "sent, but the harness hasn't picked it up yet" state; for
reference, Cursor renders such not-yet-consumed messages grayed out.

The user-observable journey this test drives:

1. open a session and send a message that starts a long-running turn
   (the mock LLM blocks the turn's second model call on a gate, so the
   turn stays open — running, un-consumable — for as long as the test
   needs);
2. while the turn is running, type a follow-up and send it — it parks
   in the queued strip;
3. click "Send now" (steer) on the queued row;
4. the steered message appears in the transcript as a normal user
   bubble, indistinguishable from the already-consumed first message —
   no grayed-out / pending affordance — even though the harness is
   still blocked inside the model call and cannot have consumed it.

The failing assertion accepts EITHER contract a fix may choose, so it
does not over-pin the implementation:

- a semantic pending hook on the steered bubble (a ``data-pending``
  attribute, or a pending/unconsumed-indicator descendant testid or
  aria-label), or
- any visual distinction (opacity / filter / color / background) between
  the unconsumed steered bubble and a committed, consumed bubble.

Note the assertion runs while the gate is verifiably still held, so a
client-only cosmetic fix that keys off the *POST* lifecycle but still
trusts the server's premature ``session.input.consumed`` (published at
POST time on this path, before the loop ever drains its inbox) will not
turn it green — the intermediate state must reflect actual harness
consumption end to end.

After the gate releases and the turn drains (the loop consumes the
steered input), the steered bubble must settle to the normal committed
presentation — exactly one bubble per message, no lingering pending
affordance.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
from playwright.sync_api import Locator, Page, expect

_COMPOSER_LABEL = "Message the agent"
_STEER_ROW_LABEL = "Send queued message now"
_QUEUED_STRIP_TESTID = "composer-queued-strip"

# Unique sentinels so each user bubble is unambiguously locatable and the
# model has no reason to echo them verbatim into an assistant bubble.
_FIRST_TURN_MSG = "steer-pending first turn opener sentinel-a17e"
_STEERED_MSG = "steer-pending steered follow-up sentinel-c93b"


def _wait_for(page: Page, predicate: Callable[[], bool], *, timeout_s: float = 30.0) -> None:
    """Poll ``predicate`` on the Playwright loop until true or timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _send(page: Page, text: str) -> None:
    """Type ``text`` into the composer and click Send."""
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _gate_pending(mock_url: str) -> bool:
    """Whether the mock LLM currently holds a model call open on its gate."""
    return bool(httpx.get(f"{mock_url}/gate/pending", timeout=5.0).json()["pending"])


def _release_gate(mock_url: str) -> None:
    response = httpx.post(f"{mock_url}/gate/release", timeout=5.0)
    response.raise_for_status()
    assert response.json()["released"] is True


def _session_status(base_url: str, session_id: str) -> str:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return str(response.json().get("status", ""))


def _user_bubble(page: Page, text: str) -> Locator:
    """Locator for the transcript user-message bubble carrying ``text``."""
    return page.locator('[data-testid="message-bubble"][data-role="user"]').filter(has_text=text)


def _presentation(bubble: Locator) -> dict[str, Any]:
    """Visual signature of a user bubble: wrapper + painted bubble surface.

    Captures the properties a "grayed out" treatment would touch —
    opacity, filter, text color, background — on the bubble wrapper and
    on its first descendant with a painted background (the rounded
    bubble surface). Deliberately narrow so incidental differences
    (layout, text length) cannot register as a distinction.
    """
    return bubble.evaluate(
        """
        (el) => {
          const sig = (n) => {
            const cs = getComputedStyle(n);
            return {
              opacity: cs.opacity,
              filter: cs.filter,
              color: cs.color,
              backgroundColor: cs.backgroundColor,
            };
          };
          let surface = null;
          for (const child of el.querySelectorAll("div")) {
            const bg = getComputedStyle(child).backgroundColor;
            if (bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent") {
              surface = child;
              break;
            }
          }
          return { wrapper: sig(el), surface: surface ? sig(surface) : null };
        }
        """
    )


def _has_pending_affordance(bubble: Locator) -> bool:
    """Whether the bubble carries any semantic not-yet-consumed marker."""
    if bubble.count() != 1:
        return False
    if bubble.get_attribute("data-pending") is not None:
        return True
    hooks = bubble.locator(
        '[data-testid*="pending"], [data-testid*="unconsumed"], '
        '[aria-label*="pending" i], [aria-label*="sending" i], '
        '[aria-label*="not yet" i], [aria-label*="delivered" i]'
    )
    return hooks.count() > 0


def test_steered_message_shows_intermediate_state_until_harness_consumes_it(
    page: Page,
    paused_mid_turn_session: tuple[str, str, str],
) -> None:
    """A steered mid-turn follow-up must be distinguishable until consumed."""
    base_url, session_id, mock_url = paused_mid_turn_session

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)

    # 1. Start a turn. The mock LLM answers the first model call with a
    #    tool call, then BLOCKS the second model call on its gate — the
    #    turn is now held open mid-step, so nothing the loop does can
    #    consume a steered input until the gate releases.
    _send(page, _FIRST_TURN_MSG)
    expect(_user_bubble(page, _FIRST_TURN_MSG)).to_be_visible(timeout=15_000)
    _wait_for(page, lambda: _gate_pending(mock_url), timeout_s=60.0)

    # 2. Mid-turn follow-up parks in the queued strip (the default,
    #    not-yet-sent state — this strip is NOT the intermediate state
    #    under test; the message hasn't been POSTed yet).
    _send(page, _STEERED_MSG)
    strip = page.get_by_test_id(_QUEUED_STRIP_TESTID)
    expect(strip).to_contain_text(_STEERED_MSG)

    # 3. Steer it into the running turn.
    page.get_by_label(_STEER_ROW_LABEL).click()
    expect(strip).to_have_count(0)

    steered = _user_bubble(page, _STEERED_MSG)
    expect(steered).to_be_visible(timeout=15_000)

    # Park the mouse away from both bubbles so hover styling cannot
    # contaminate the visual signatures.
    page.mouse.move(5, 5)
    # Give any pending affordance a moment to render/settle before
    # sampling — also what a viewer of the recording needs to see the
    # steered bubble sitting at full strength while the turn still runs.
    page.wait_for_timeout(1_500)

    # The gate is still held: the agent loop is parked inside the second
    # model call, so it cannot have consumed the steered input yet.
    assert _gate_pending(mock_url), "test precondition lost: the mock gate released early"

    committed = _user_bubble(page, _FIRST_TURN_MSG)
    steered_pres = _presentation(steered)
    committed_pres = _presentation(committed)

    # THE BUG: while the harness has not consumed the steered
    # message, the transcript must show it in an intermediate state —
    # a semantic pending marker or a grayed-out visual treatment. Today
    # it renders bit-identically to the consumed message.
    assert _has_pending_affordance(steered) or steered_pres != committed_pres, (
        "steered message renders identically to a consumed message while the "
        "harness has not consumed it: no pending marker and no visual "
        f"distinction (signature: {steered_pres})"
    )

    # 4. Release the gate and let the turn drain — the loop finishes its
    #    blocked step, consumes the steered input from its inbox, and
    #    idles. Once consumed, the intermediate state must clear: the
    #    steered bubble settles to the normal committed presentation.
    _release_gate(mock_url)
    _wait_for(page, lambda: _session_status(base_url, session_id) == "idle", timeout_s=90.0)

    expect(_user_bubble(page, _FIRST_TURN_MSG)).to_have_count(1)
    expect(_user_bubble(page, _STEERED_MSG)).to_have_count(1)
    _wait_for(page, lambda: not _has_pending_affordance(_user_bubble(page, _STEERED_MSG)))
    page.mouse.move(5, 5)
    assert _presentation(_user_bubble(page, _STEERED_MSG)) == _presentation(
        _user_bubble(page, _FIRST_TURN_MSG)
    ), "consumed steered message should settle to the normal committed presentation"
