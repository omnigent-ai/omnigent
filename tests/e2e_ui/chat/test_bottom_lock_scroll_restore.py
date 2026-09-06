"""E2E: the bottom-lock preference and per-session scroll restore.

``Keep chat pinned to bottom`` (Settings -> Appearance) gates whether the
transcript jumps to and follows the response on send. When it is off, the
``use-stick-to-bottom`` lock is released, so a reader parked mid-history keeps
their position — including when they send. Independently, each conversation's
reading position is captured when the user navigates away and restored when
they come back, anchored to the user message at the top of the viewport.

Neither behavior is observable in jsdom (no layout, no compositor), so this
drives two real sessions against the mock LLM: build a scrollable transcript,
park mid-history, leave and return, and assert the viewport lands back on the
same region; then, with the toggle off, send mid-history and assert the
viewport does not move.
"""

from __future__ import annotations

import uuid

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Send a message…"
_SEND = "Send"
_USER = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

# Small viewport so a handful of turns make the transcript scrollable.
_VIEWPORT = {"width": 1280, "height": 480}

# Mock reply long enough that six turns overflow a 480px viewport.
_REPLY = (
    "A measured reply with enough prose to occupy real vertical space in the "
    "transcript column, so that a handful of turns produces a genuine scroll "
    "range rather than a transcript that always fits the viewport."
)

# Tag the transcript's scroll element (tallest scrollable descendant of the
# log region — the same shape the other transcript tests use) and return its
# geometry.
_TAG_SCROLLER = """
() => {
  const log = document.querySelector('[role="log"]');
  let best = null;
  log.querySelectorAll('*').forEach((el) => {
    if (el.scrollHeight > el.clientHeight + 4) {
      if (!best || el.scrollHeight > best.scrollHeight) best = el;
    }
  });
  const el = best || log;
  el.setAttribute('data-pw-scroller', '1');
  return {
    scrollTop: el.scrollTop,
    maxScrollTop: el.scrollHeight - el.clientHeight,
  };
}
"""

_READ_SCROLLER = """
() => {
  const el = document.querySelector('[data-pw-scroller]');
  return {
    scrollTop: el.scrollTop,
    maxScrollTop: el.scrollHeight - el.clientHeight,
  };
}
"""

_PARK = "el => { el.scrollTop = el.scrollHeight / 2; }"


def _build_turns(page: Page, count: int) -> None:
    """Send *count* turns, waiting for each to settle before the next."""
    for i in range(count):
        page.get_by_placeholder(_COMPOSER).fill(f"next turn {i}")
        page.get_by_role("button", name=_SEND, exact=True).click()
        expect(page.locator(_USER)).to_have_count(i + 1, timeout=30_000)
        expect(page.locator(_ASSISTANT)).to_have_count(i + 1, timeout=60_000)
        expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)


def test_scroll_position_restored_after_leaving_and_returning(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
    mock_llm_server_url: str,
) -> None:
    """Returning to a session restores the reader's viewport, not the bottom."""
    base_url, session_a, session_b = seeded_session_pair
    configure_mock_llm(mock_llm_server_url, [{"text": _REPLY}] * 6, key="default")

    page.set_viewport_size(_VIEWPORT)
    page.goto(f"{base_url}/c/{session_a}")
    _build_turns(page, 6)

    # Escape the bottom lock and park mid-history.
    geometry = page.evaluate(_TAG_SCROLLER)
    assert geometry["maxScrollTop"] > 100, "transcript did not build a scroll range"
    page.locator("[data-pw-scroller]").evaluate(_PARK)
    parked = page.evaluate(_READ_SCROLLER)
    assert parked["scrollTop"] < parked["maxScrollTop"] - 50

    # Leave for another session, then come back — via the sidebar, the way a
    # user does: a full page.goto reload would tear down the SPA and skip the
    # capture-on-switch effect this feature relies on.
    page.locator(f'a[href="/c/{session_b}"]').click()
    page.locator(f'a[href="/c/{session_a}"]').click()
    expect(page.locator(_USER)).to_have_count(6, timeout=30_000)
    expect(page.locator(_ASSISTANT)).to_have_count(6, timeout=30_000)

    # The transcript remounts on return, so the previously tagged element is
    # gone — re-tag the fresh scroller, then read the restored offset.
    page.evaluate(_TAG_SCROLLER)
    restored = page.evaluate(_READ_SCROLLER)
    # Restored to the parked region, NOT jumped to the bottom. The anchor is a
    # user message, so the exact pixel can differ from the parked scrollTop —
    # a bounded drift is the contract.
    assert restored["scrollTop"] < restored["maxScrollTop"] - 50
    assert abs(restored["scrollTop"] - parked["scrollTop"]) <= 200


def test_send_with_bottom_lock_off_keeps_reader_position(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """With the toggle off, sending from mid-history does not yank to the bottom."""
    base_url, session_id = seeded_session
    token = f"lock-off-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(mock_llm_server_url, [{"text": _REPLY}] * 6, key="default")
    configure_mock_llm(mock_llm_server_url, [{"text": f"ack {token}"}], key="default", match=token)

    # The preference is read on boot; seed it before the SPA loads.
    page.add_init_script("window.localStorage.setItem('omnigent:bottom-lock', 'false')")
    page.set_viewport_size(_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    _build_turns(page, 6)

    geometry = page.evaluate(_TAG_SCROLLER)
    assert geometry["maxScrollTop"] > 100, "transcript did not build a scroll range"
    page.locator("[data-pw-scroller]").evaluate(_PARK)
    parked = page.evaluate(_READ_SCROLLER)

    # Send from mid-history: the user bubble must appear and the assistant must
    # reply, while the viewport stays parked (bottom lock off).
    page.get_by_placeholder(_COMPOSER).fill(f"say {token}")
    page.get_by_role("button", name=_SEND, exact=True).click()
    expect(page.locator(_USER)).to_have_count(7, timeout=30_000)
    expect(page.locator(_ASSISTANT).last).to_contain_text(token, timeout=60_000)

    after = page.evaluate(_READ_SCROLLER)
    assert after["maxScrollTop"] >= parked["maxScrollTop"], "transcript shrank unexpectedly"
    # Not yanked to the bottom: still parked (within the drift the appended
    # turn's own height allows), far from the new maximum.
    assert after["scrollTop"] < after["maxScrollTop"] - 200
