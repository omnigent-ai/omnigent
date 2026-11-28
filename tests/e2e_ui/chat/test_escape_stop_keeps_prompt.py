"""UI journey: stopping a running agent keeps the prompt that started it.

On a claude-native session the just-sent prompt is still an optimistic bubble
when the composer's Interrupt (Stop) affordance appears: the prompt only
commits after the terminal round-trip. Stopping the turn in that window, with
Esc or the Stop button, must leave the bubble in the chat until the server
reconciles it, and a reload must still show it.

Journey, against the real SPA + live server + real ``claude`` CLI on the mock
provider (its reply is delayed so the agent is observably running):

1. open a fresh claude-native session, wait for the terminal to attach, switch
   to the Chat view,
2. type a sentinel prompt and press Enter; wait for the Interrupt button,
3. press Esc in the composer and sample the sentinel bubble for 12 s,
4. reload and count the sentinel bubble again,
5. control: send a second sentinel, click the Interrupt button instead, sample,
   reload.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import _select_view_mode
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

_USER = '[data-testid="message-bubble"][data-role="user"]'
_COMPOSER_LABEL = "Message the agent"

# Mock reply delay that keeps the agent running until the user stops it.
_REPLY_DELAY_S = 30
# Sample the sentinel bubble from the stop action through the settled state.
_WATCH_S = 12.0
_WATCH_STEP_MS = 500
# Re-send if the prompt had already committed when the stop control was used.
_MAX_ARM_ATTEMPTS = 3
_SESSION_LOAD_TIMEOUT_MS = 60_000


def _composer(page: Page):
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)
    return composer


def _send_from_composer(page: Page, text: str) -> None:
    composer = _composer(page)
    composer.click()
    composer.fill(text)
    composer.press("Enter")


def _sentinel_bubbles(page: Page, sentinel: str):
    return page.locator(_USER, has_text=sentinel)


def _bubble_id(page: Page, sentinel: str) -> str | None:
    # One evaluation over all matches avoids a count()-then-evaluate() race where
    # the bubble detaches between the two reads.
    ids = _sentinel_bubbles(page, sentinel).evaluate_all(
        "els => els.map(el => (el.closest('[data-user-message-id]')"
        " ?? el.querySelector('[data-user-message-id]'))"
        "?.getAttribute('data-user-message-id') ?? null)"
    )
    return ids[0] if ids else None


def _arm_delayed_reply(mock_url: str, sentinel: str) -> None:
    configure_mock_llm(
        mock_url,
        [{"text": f"ast-{sentinel}", "delay": _REPLY_DELAY_S}] * 3,
        match=sentinel,
    )


def _reload_to_chat(page: Page) -> None:
    page.reload()
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=_SESSION_LOAD_TIMEOUT_MS)
    _select_view_mode(page, "Chat")
    _composer(page)


def _count_after_reload(page: Page, sentinel: str) -> int:
    try:
        expect(_sentinel_bubbles(page, sentinel)).to_have_count(1, timeout=30_000)
        return 1
    except AssertionError:
        return _sentinel_bubbles(page, sentinel).count()


def _stop_running_turn(
    page: Page,
    sentinel: str,
    interrupt_posts: list[str],
    *,
    control: str,
) -> dict[str, Any]:
    """Stop the running turn with *control* (``"escape"`` or ``"button"``).

    Returns what was observed: whether the sentinel bubble was still optimistic
    (``pend_*``) at the keypress/click, whether an interrupt was POSTed, the
    sentinel bubble count sampled every 0.5 s from the stop action onwards, and
    when (if ever) the Interrupt button reverted to Send during that window.
    """
    interrupt_button = page.get_by_role("button", name="Interrupt", exact=True)
    expect(_sentinel_bubbles(page, sentinel).first).to_be_visible(timeout=30_000)
    expect(interrupt_button).to_be_visible(timeout=60_000)

    # Attribute only interrupts POSTed from here on. A prior turn's interrupt was
    # already recorded before this baseline, avoiding the clear()/route-append race.
    posts_before = len(interrupt_posts)
    # Snapshot the bubble id as the last read before the stop fires, so a native
    # round-trip that commits the prompt can't widen the window we call optimistic.
    if control == "escape":
        composer = _composer(page)
        composer.focus()
        id_at_stop = _bubble_id(page, sentinel)
        composer.press("Escape")
    else:
        id_at_stop = _bubble_id(page, sentinel)
        interrupt_button.click()

    counts: list[int] = []
    reverted_at_s: float | None = None
    started = time.monotonic()
    while True:
        counts.append(_sentinel_bubbles(page, sentinel).count())
        elapsed = time.monotonic() - started
        if reverted_at_s is None and interrupt_button.count() == 0:
            reverted_at_s = round(elapsed, 1)
        if elapsed >= _WATCH_S:
            break
        page.wait_for_timeout(_WATCH_STEP_MS)
    result = {
        "control": control,
        "sentinel": sentinel,
        "bubble_id_at_stop": id_at_stop,
        "optimistic_at_stop": bool(id_at_stop and id_at_stop.startswith("pend_")),
        "interrupt_posted": len(interrupt_posts) > posts_before,
        "interrupt_button_reverted_at_s": reverted_at_s,
        "counts_from_stop": counts,
    }
    _log.info("stop via %s: %s", control, result)
    return result


def _arm_and_stop(
    page: Page,
    mock_url: str,
    sentinel_suffix: str,
    interrupt_posts: list[str],
    *,
    control: str,
) -> dict[str, Any]:
    """Send a fresh sentinel and stop its turn with *control*, retrying with a new
    sentinel until the stop fires while the prompt is still optimistic."""
    result: dict[str, Any] = {}
    for attempt in range(1, _MAX_ARM_ATTEMPTS + 1):
        sentinel = f"sentinel-{uuid.uuid4().hex[:8]}"
        _arm_delayed_reply(mock_url, sentinel)
        _send_from_composer(page, f"{sentinel} {sentinel_suffix}")
        result = _stop_running_turn(page, sentinel, interrupt_posts, control=control)
        if result["optimistic_at_stop"]:
            return result
        if attempt < _MAX_ARM_ATTEMPTS:
            # Let the interrupted turn settle so the next attempt stops the new
            # turn, not a stale Interrupt button still showing from this one.
            expect(page.get_by_role("button", name="Interrupt", exact=True)).to_have_count(
                0, timeout=30_000
            )
            _log.info("attempt %d: prompt committed before %s stop; re-sending", attempt, control)
    return result


@pytest.mark.timeout(480)
def test_escape_stop_keeps_prompt(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = native_claude_mock_session
    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", "ok")
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, "ok")

    # Create the recorded page only after non-browser setup.
    page: Page = request.getfixturevalue("page")

    interrupt_posts: list[str] = []

    def _record_interrupts(route: Route) -> None:
        body = route.request.post_data or ""
        try:
            is_interrupt = json.loads(body).get("type") == "interrupt"
        except (ValueError, AttributeError):
            is_interrupt = False
        if route.request.method == "POST" and is_interrupt:
            interrupt_posts.append(body)
        route.continue_()

    page.route("**/v1/sessions/*/events", _record_interrupts)

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _select_view_mode(page, "Chat")
    _composer(page)
    expect(page.locator(_USER)).to_have_count(0)

    # --- Esc, then the Stop button: each must keep its prompt through the stop. ---
    esc = _arm_and_stop(
        page,
        mock_llm_server_url,
        "keep me when Esc stops the agent",
        interrupt_posts,
        control="escape",
    )
    _reload_to_chat(page)
    esc_after_reload = _count_after_reload(page, esc["sentinel"])
    _log.info("Esc sentinel bubbles after reload: %d", esc_after_reload)

    button = _arm_and_stop(
        page,
        mock_llm_server_url,
        "keep me when Stop is clicked",
        interrupt_posts,
        control="button",
    )
    _reload_to_chat(page)
    button_after_reload = _count_after_reload(page, button["sentinel"])
    _log.info("Stop sentinel bubbles after reload: %d", button_after_reload)

    failures: list[str] = []
    for leg, result, after_reload in (
        ("Esc", esc, esc_after_reload),
        ("Stop button", button, button_after_reload),
    ):
        if not result["optimistic_at_stop"]:
            failures.append(
                f"{leg}: never caught the prompt while it was still optimistic in "
                f"{_MAX_ARM_ATTEMPTS} attempts (bubble id at stop "
                f"{result['bubble_id_at_stop']!r}) — the regression window was not exercised"
            )
        if not result["interrupt_posted"]:
            failures.append(f"{leg}: did not interrupt the running agent (no interrupt POSTed)")
        if result["counts_from_stop"] != [1] * len(result["counts_from_stop"]):
            failures.append(
                f"{leg}: sentinel bubble count deviated from 1 (removed or duplicated); counts "
                f"sampled every 0.5s from the stop = {result['counts_from_stop']} (bubble id "
                f"at stop {result['bubble_id_at_stop']!r}; bubbles after reload = {after_reload})"
            )
        if after_reload != 1:
            failures.append(
                f"{leg}: expected exactly one '{result['sentinel']}' bubble after reload, "
                f"found {after_reload}"
            )
    assert not failures, "\n".join(failures)
