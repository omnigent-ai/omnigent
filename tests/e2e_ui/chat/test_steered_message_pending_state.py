"""E2E: a follow-up steered into a running turn looks pending until consumed.

The turn is held open on the mock gate, so the steered bubble is compared
with the already-consumed first bubble while the agent is provably still
working and has not seen the follow-up.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, expect

_COMPOSER_LABEL = "Message the agent"
_FIRST_MSG = "sentinel-steer-pending-a7c1 inspect the workspace"
_STEERED_MSG = "sentinel-steer-pending-e4f2 follow-up steered mid-turn"
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'

# What a reader uses to tell an unconsumed message from a committed one: a
# marker on the bubble, the effective opacity of its text, and its colours.
# Walks up through wrappers unique to this bubble, stopping at the container
# shared with other bubbles.
_PRESENTATION_JS = """
(el, sentinel) => {
  const leaf =
    [...el.querySelectorAll('*')].find(
      (n) => n.childElementCount === 0 && (n.textContent ?? '').includes(sentinel),
    ) ?? el;
  const transparent = 'rgba(0, 0, 0, 0)';
  let opacity = 1;
  let background = transparent;
  for (
    let node = leaf;
    node && node.querySelectorAll('[data-testid="message-bubble"]').length <= 1;
    node = node.parentElement
  ) {
    const cs = getComputedStyle(node);
    opacity *= parseFloat(cs.opacity);
    if (background === transparent && cs.backgroundColor !== transparent) {
      background = cs.backgroundColor;
    }
  }
  const markers = Object.fromEntries(
    Object.entries(el.dataset).filter(
      ([k]) => !['testid', 'role', 'userMessageId', 'messageId'].includes(k),
    ),
  );
  return {
    markers,
    ariaBusy: el.getAttribute('aria-busy'),
    effectiveOpacity: Number(opacity.toFixed(3)),
    color: getComputedStyle(leaf).color,
    background,
  };
}
"""

# The parts of the presentation a reader can actually see.
_VISIBLE_KEYS = ("effectiveOpacity", "color", "background")


def _wait_for(predicate: Callable[[], bool], *, what: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"{what} not observed within {timeout_s:.0f}s")


def _gate_pending(mock_url: str) -> bool:
    return bool(httpx.get(f"{mock_url}/gate/pending", timeout=5.0).json()["pending"])


def _session_status(base_url: str, session_id: str) -> str:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return str(response.json().get("status"))


def _items_contain(base_url: str, session_id: str, text: str) -> bool:
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 1000},
        timeout=10.0,
    )
    response.raise_for_status()
    return text in json.dumps(response.json())


def _send(page: Page, text: str) -> None:
    page.get_by_label(_COMPOSER_LABEL).fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _user_bubble(page: Page, text: str) -> Locator:
    return page.locator(_USER_BUBBLE).filter(has_text=text)


def _presentation(bubble: Locator, sentinel: str) -> dict[str, Any]:
    return bubble.evaluate(_PRESENTATION_JS, sentinel)


def test_steered_followup_looks_pending_until_the_agent_consumes_it(
    request: pytest.FixtureRequest,
    browser: Browser,
    browser_context_args: dict[str, Any],
    paused_mid_turn_session: tuple[str, str, str],
) -> None:
    base_url, session_id, mock_url = paused_mid_turn_session
    shots = Path(request.config.getoption("--output")) / "steered-message-pending-state"
    shots.mkdir(parents=True, exist_ok=True)

    context = browser.new_context(**browser_context_args)
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}")
        expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)

        _send(page, _FIRST_MSG)
        first = _user_bubble(page, _FIRST_MSG)
        expect(first).to_be_visible(timeout=15_000)
        # First tool call done, second model call blocked: the agent is working.
        _wait_for(lambda: _gate_pending(mock_url), what="turn held open on the mock gate")
        expect(page.locator(_ASSISTANT_BUBBLE).first).to_be_visible(timeout=30_000)

        _send(page, _STEERED_MSG)
        strip = page.get_by_test_id("composer-queued-strip")
        expect(strip).to_contain_text(_STEERED_MSG, timeout=15_000)
        page.get_by_role("button", name="Send queued message now").click()
        expect(strip).to_be_hidden(timeout=15_000)

        steered = _user_bubble(page, _STEERED_MSG)
        expect(steered).to_be_visible(timeout=15_000)
        _wait_for(
            lambda: _items_contain(base_url, session_id, _STEERED_MSG),
            what="steered message delivered to the server",
        )
        # Let the optimistic→committed swap and the entrance animation settle.
        page.wait_for_timeout(2_000)
        assert _gate_pending(mock_url), (
            "the turn ended before the comparison; the follow-up may already be consumed"
        )

        consumed_look = _presentation(first, _FIRST_MSG)
        pending_look = _presentation(steered, _STEERED_MSG)
        page.screenshot(path=str(shots / "steered-while-agent-working.png"))
        (shots / "observation.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "gate_pending": _gate_pending(mock_url),
                    "consumed_first_message": consumed_look,
                    "steered_follow_up": pending_look,
                },
                indent=2,
            )
        )
        assert pending_look != consumed_look, (
            "steered follow-up renders exactly like the consumed first message "
            f"while the agent is still working: {pending_look}"
        )
        assert any(pending_look[k] != consumed_look[k] for k in _VISIBLE_KEYS), (
            "steered follow-up is marked pending but looks identical to the consumed "
            f"first message: {pending_look}"
        )

        # A cold load restores the pending look from the session snapshot.
        page.reload()
        expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)
        expect(steered).to_be_visible(timeout=15_000)
        expect(steered).to_have_attribute("data-pending", "true", timeout=15_000)
        assert _gate_pending(mock_url), "the turn ended before the reload comparison"
        consumed_look = _presentation(first, _FIRST_MSG)
        reloaded_look = _presentation(steered, _STEERED_MSG)
        page.screenshot(path=str(shots / "steered-after-reload.png"))
        assert any(reloaded_look[k] != consumed_look[k] for k in _VISIBLE_KEYS), (
            f"steered follow-up lost its pending look after a reload: {reloaded_look}"
        )

        httpx.post(f"{mock_url}/gate/release", timeout=5.0).raise_for_status()
        # The gated turn wraps up and the follow-up is consumed on the way to idle.
        _wait_for(
            lambda: (
                _session_status(base_url, session_id) == "idle" and not _gate_pending(mock_url)
            ),
            what="session idle after the gate release",
            timeout_s=90.0,
        )
        expect(steered).to_have_count(1)
        _wait_for(
            lambda: _presentation(steered, _STEERED_MSG) == consumed_look,
            what="steered bubble settling to the committed presentation",
            timeout_s=30.0,
        )
        page.screenshot(path=str(shots / "steered-after-consumption.png"))
    finally:
        context.close()
