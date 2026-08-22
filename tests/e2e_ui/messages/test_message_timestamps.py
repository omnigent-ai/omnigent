"""E2E: hover-revealed timestamps on chat message bubbles.

Chat bubbles show their send/receive time on hover next to the Copy/Fork
controls, without taking permanent vertical space. This drives the full
browser → SPA → server stack: send a message, wait for the mock-LLM reply,
and assert for both the user and the assistant bubble that

  - the timestamp rides inside the existing 24px action row (no new row),
  - it matches a locale time format (``h:MM AM`` style),
  - the action row is fully transparent at rest on desktop and reaches full
    opacity on hover (the hover-reveal contract),
  - the ordering matches the design target (user: timestamp → Copy at the
    right edge; assistant: Copy/Fork → timestamp at the left edge),
  - the stamp survives a full page reload (server-stamped path, not a
    re-stamped render time),
  - on a touch-sized viewport the row rests at 40% opacity so the actions
    stay discoverable without a hover affordance.

Selectors:
  - bubbles: ``data-testid="message-bubble"`` + ``data-role="user|assistant"``
  - timestamp: ``data-testid="message-timestamp"`` inside the action row
  - action row: the timestamp's parent div (``opacity-0``/``opacity-40`` base
    with ``md:group-hover:opacity-100`` reveal)
"""

from __future__ import annotations

import re
import sqlite3
import time

import httpx
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e_ui.conftest import _server_state, seed_committed_items

_COMPOSER_PLACEHOLDER = "Ask the agent anything…"
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_TIMESTAMP = '[data-testid="message-timestamp"]'
# Generous budget for the mock-LLM roundtrip: the shards run 120+ tests
# against one server, and a 60s wait proved tight under CI load.
_ASSISTANT_TIMEOUT_MS = 120_000
# en-US renders "2:55 PM"; 24-hour locales render "14:55". Both are valid —
# the assertion is about a clock-shaped value, not a specific locale.
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(\s?[AP]M)?$")


def _send(page: Page, text: str) -> None:
    """Type ``text`` into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER_PLACEHOLDER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _seed_assistant_text(base_url: str, session_id: str, text: str) -> None:
    """Append one deterministic assistant message to ``session_id``.

    Same events-API seeding as ``test_adjacent_assistant_text_spacing``:
    the store stamps ``created_at`` at append time, so the bubble renders
    with a server-stamped timestamp without depending on a live mock-LLM
    turn (which proved flaky under full-shard load when driven from a
    second, touch-sized browser context).
    """
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {
                "agent": "hello_world",
                "response_id": "resp_message_timestamps_touch",
                "text": text,
            },
        },
        timeout=10.0,
    )
    resp.raise_for_status()


def _action_row(timestamp: Locator) -> Locator:
    """The action row is the timestamp span's parent div."""
    return timestamp.locator("..")


def _opacity(locator: Locator) -> str:
    return locator.evaluate("el => getComputedStyle(el).opacity")


def _wait_opacity(locator: Locator, value: str, timeout_s: float = 5.0) -> None:
    """Poll the computed opacity until it reaches ``value``.

    ``expect.poll`` is not available in the pinned Playwright, and the
    hover reveal is a CSS transition (no DOM event to await), so poll.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _opacity(locator) == value:
            return
        time.sleep(0.05)
    raise AssertionError(f"opacity never became {value!r} within {timeout_s}s")


def test_hover_reveals_timestamp_on_user_and_assistant_bubbles(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Both bubbles reveal a correctly ordered timestamp row on hover."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    _send(page, "Timestamp probe: say anything.")

    # --- user bubble ---
    user_bubble = page.locator(_USER_BUBBLE).first
    expect(user_bubble).to_be_visible(timeout=15_000)
    user_ts = user_bubble.locator(_TIMESTAMP)
    expect(user_ts).to_have_count(1)
    expect(user_ts).to_have_text(_TIME_RE)
    user_row = _action_row(user_ts)

    # At rest on a desktop viewport the row is fully transparent — the
    # timestamp must not permanently occupy visual space.
    assert _opacity(user_row) == "0"
    # The row exists at the design's 24px height even while hidden.
    assert round(user_row.bounding_box()["height"]) == 24

    user_bubble.hover()
    _wait_opacity(user_row, "1")

    # Design order: timestamp → Copy at the bubble's right edge.
    copy_button = user_bubble.get_by_role("button", name="Copy")
    assert user_ts.bounding_box()["x"] < copy_button.bounding_box()["x"]

    # --- assistant bubble ---
    assistant_bubble = page.locator(_ASSISTANT_BUBBLE).first
    expect(assistant_bubble).to_be_visible(timeout=_ASSISTANT_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_ASSISTANT_TIMEOUT_MS)
    assistant_ts = assistant_bubble.locator(_TIMESTAMP)
    expect(assistant_ts).to_have_count(1)
    expect(assistant_ts).to_have_text(_TIME_RE)
    assistant_row = _action_row(assistant_ts)

    assert _opacity(assistant_row) == "0"
    assert round(assistant_row.bounding_box()["height"]) == 24

    assistant_bubble.hover()
    _wait_opacity(assistant_row, "1")

    # Design order: Copy/Fork → timestamp at the bubble's left edge.
    assistant_copy = assistant_bubble.get_by_role("button", name="Copy")
    assert assistant_copy.bounding_box()["x"] < assistant_ts.bounding_box()["x"]

    # --- persistence: reload must show the same server-stamped values ---
    user_stamp = user_ts.inner_text()
    assistant_stamp = assistant_ts.inner_text()
    page.reload()
    expect(page.locator(_USER_BUBBLE).first.locator(_TIMESTAMP)).to_have_text(
        user_stamp, timeout=30_000
    )
    expect(page.locator(_ASSISTANT_BUBBLE).first.locator(_TIMESTAMP)).to_have_text(
        assistant_stamp, timeout=30_000
    )


def test_assistant_bubble_timestamp_tracks_latest_turn_activity(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A multi-item turn's bubble shows the LATEST item's time, not the first.

    Regression: the bubble took the FIRST stamped block of a response
    group, so a long turn stayed pinned at turn start (30+ minutes stale
    in the field report) and a refresh reverted to it. Seeds a settled
    turn whose items span an hour and asserts the hydrated bubble reads
    the latest item's minute.
    """
    from omnigent.db.db_models import uuid_to_bytes
    from omnigent.entities import MessageData, NewConversationItem

    base_url, session_id = seeded_session
    response_id = "resp_ts_latest_activity"
    seed_committed_items(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "Long turn probe."}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "Turn start."}],
                    agent="hello_world",
                ),
            ),
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "Turn end."}],
                    agent="hello_world",
                ),
            ),
        ],
    )
    # Spread the turn across an hour: the store stamps created_at at
    # append time, so backdate directly in the spawned server's sqlite —
    # the same file the page hydrates from.
    db_path = str(_server_state["database_uri"]).removeprefix("sqlite:///")
    turn_end = int(time.time())
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        rows = conn.execute(
            "SELECT id FROM conversation_items "
            "WHERE conversation_id = ? AND response_id = ? ORDER BY position",
            (uuid_to_bytes(session_id), response_id),
        ).fetchall()
        assert len(rows) == 3
        conn.execute(
            "UPDATE conversation_items SET created_at = ? WHERE id = ?",
            (turn_end - 3600, rows[1][0]),
        )
        conn.execute(
            "UPDATE conversation_items SET created_at = ? WHERE id = ?",
            (turn_end, rows[2][0]),
        )
        conn.commit()
    finally:
        conn.close()

    page.goto(f"{base_url}/c/{session_id}")
    assistant_ts = page.locator(_ASSISTANT_BUBBLE).first.locator(_TIMESTAMP)
    expect(assistant_ts).to_have_count(1, timeout=30_000)

    # The browser locale decides 12h vs 24h rendering; both describe the
    # same minute. The turn start is exactly one hour earlier, so it can
    # never format to either string.
    end_local = time.localtime(turn_end)
    twelve_h = (
        f"{end_local.tm_hour % 12 or 12}:{end_local.tm_min:02d} "
        f"{'AM' if end_local.tm_hour < 12 else 'PM'}"
    )
    twenty_four_h = f"{end_local.tm_hour}:{end_local.tm_min:02d}"
    stamp = assistant_ts.inner_text()
    assert stamp in (twelve_h, twenty_four_h), (
        f"bubble shows {stamp!r}; expected the turn's latest activity "
        f"({twelve_h!r} or {twenty_four_h!r}), not its start"
    )


def test_touch_viewport_keeps_timestamp_row_discoverable(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Below the md breakpoint the action row rests at 40% opacity.

    Touch devices have no hover; a permanently invisible row would hide the
    Copy action and the timestamp entirely. A failure means the
    ``opacity-40`` touch fallback regressed.
    """
    base_url, session_id = seeded_session
    _seed_assistant_text(base_url, session_id, "Touch probe assistant reply.")
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, has_touch=True)
    try:
        page = ctx.new_page()
        page.goto(f"{base_url}/c/{session_id}")

        # The seeded reply is already on screen; the composer send still
        # exercises the real touch UI path for the user bubble.
        assistant_bubble = page.locator(_ASSISTANT_BUBBLE).first
        expect(assistant_bubble).to_be_visible(timeout=30_000)

        _send(page, "Touch timestamp probe: say anything.")
        user_bubble = page.locator(_USER_BUBBLE).first
        expect(user_bubble).to_be_visible(timeout=15_000)

        user_ts = user_bubble.locator(_TIMESTAMP)
        expect(user_ts).to_have_count(1)
        expect(user_ts).to_have_text(_TIME_RE)

        # No hover performed: the row must still be partially visible.
        assert _opacity(_action_row(user_ts)) == "0.4"

        assistant_ts = assistant_bubble.locator(_TIMESTAMP)
        expect(assistant_ts).to_have_count(1)
        expect(assistant_ts).to_have_text(_TIME_RE)
        assert _opacity(_action_row(assistant_ts)) == "0.4"
    finally:
        ctx.close()
