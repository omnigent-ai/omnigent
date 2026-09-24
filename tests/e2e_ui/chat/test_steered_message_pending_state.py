from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
from playwright.sync_api import Locator, Page, expect

_COMPOSER_LABEL = "Message the agent"
_STEER_ROW_LABEL = "Send queued message now"
_QUEUED_STRIP_TESTID = "composer-queued-strip"

_FIRST_TURN_MSG = "steer-pending first turn opener sentinel-a17e"
_STEERED_MSG = "steer-pending steered follow-up sentinel-c93b"


def _wait_for(page: Page, predicate: Callable[[], bool], *, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _gate_pending(mock_url: str) -> bool:
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
    return page.locator('[data-testid="message-bubble"][data-role="user"]').filter(has_text=text)


def _presentation(bubble: Locator) -> dict[str, Any]:
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
    base_url, session_id, mock_url = paused_mid_turn_session

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)

    _send(page, _FIRST_TURN_MSG)
    expect(_user_bubble(page, _FIRST_TURN_MSG)).to_be_visible(timeout=15_000)
    _wait_for(page, lambda: _gate_pending(mock_url), timeout_s=60.0)

    _send(page, _STEERED_MSG)
    strip = page.get_by_test_id(_QUEUED_STRIP_TESTID)
    expect(strip).to_contain_text(_STEERED_MSG)

    page.get_by_label(_STEER_ROW_LABEL).click()
    expect(strip).to_have_count(0)

    steered = _user_bubble(page, _STEERED_MSG)
    expect(steered).to_be_visible(timeout=15_000)

    page.mouse.move(5, 5)
    page.wait_for_timeout(1_500)

    assert _gate_pending(mock_url), "test precondition lost: the mock gate released early"

    committed = _user_bubble(page, _FIRST_TURN_MSG)
    steered_pres = _presentation(steered)
    committed_pres = _presentation(committed)

    assert _has_pending_affordance(steered) or steered_pres != committed_pres, (
        "steered message renders identically to a consumed message while the "
        "harness has not consumed it: no pending marker and no visual "
        f"distinction (signature: {steered_pres})"
    )

    _release_gate(mock_url)
    _wait_for(page, lambda: _session_status(base_url, session_id) == "idle", timeout_s=90.0)

    expect(_user_bubble(page, _FIRST_TURN_MSG)).to_have_count(1)
    expect(_user_bubble(page, _STEERED_MSG)).to_have_count(1)
    _wait_for(page, lambda: not _has_pending_affordance(_user_bubble(page, _STEERED_MSG)))
    page.mouse.move(5, 5)
    assert _presentation(_user_bubble(page, _STEERED_MSG)) == _presentation(
        _user_bubble(page, _FIRST_TURN_MSG)
    ), "consumed steered message should settle to the normal committed presentation"
