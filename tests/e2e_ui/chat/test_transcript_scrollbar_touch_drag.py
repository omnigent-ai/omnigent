"""E2E: the transcript scrollbar thumb must be draggable by touch.

Bug: on a coarse-pointer device
(Android foldable/tablet), the transcript scrollbar thumb renders but a direct
touch-drag on it never moves the transcript — ``scrollTop`` stays where it was;
only touch-scrolling the content itself works.

The thumb (``TranscriptScrollbar.tsx``) drives its drag with pointer events
(``onPointerDown``/``onPointerMove``/``onPointerUp``) but declares no
``touch-action``, so on touch Chromium runs its native pan arbitration,
cancels the pointer stream (``pointercancel``) right after ``pointerdown``,
and the drag dies after at most one ``pointermove``. A mouse drag — which has
no pan arbitration — works, which is what makes the failure touch-specific.

Journey this test drives (a real browser context with touch, tablet viewport):

  1. open a session whose transcript overflows the viewport (parked at the
     bottom, so the thumb sits at the end of its track);
  2. reference: drag the thumb up with the MOUSE — the transcript scrolls the
     full mapped distance (proves the thumb works and measures what a
     completed drag of this length scrolls);
  3. re-park at the bottom;
  4. perform the SAME drag by TOUCH (native CDP touch events, the same input
     pipeline a finger uses);
  5. expected: the transcript scrolls like the mouse drag did.
     Actual (bug): ``pointercancel`` kills the drag after at most one
     ``pointermove``, so the transcript moves a small fraction of the drag
     (or nothing at all) instead of tracking the finger.

The test FAILS on the unfixed build (step 5's assertion) and PASSES once the
thumb's pointer handling supports touch.
"""

from __future__ import annotations

import os

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _server_state

# Enough turns to overflow a 1280px-tall viewport, but comfortably under the
# transcript's INITIAL_WINDOW_ITEMS (100 items = 50 turns) so the whole
# history loads at once and lazy pagination never runs during the drags.
_TURNS = 30
_NEWEST_REPLY = f"reply number {_TURNS - 1}"

# Tablet/foldable-class portrait viewport — matches the coarse-pointer regime
# the bug was found in, and stays above the Tailwind ``md`` breakpoint so the
# desktop chat layout (which mounts the custom scrollbar) is in effect.
_VIEWPORT = {"width": 800, "height": 1280}

_THUMB = '[data-testid="transcript-scrollbar-thumb"]'

# Tag the transcript scroller: the tallest scrollable descendant of the log
# region — the same shape the other transcript tests use.
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
  return el.scrollHeight > el.clientHeight + 4;
}
"""

_SCROLL_STATE = """
() => {
  const el = document.querySelector('[data-pw-scroller]');
  return {
    scrollTop: Math.round(el.scrollTop),
    max: Math.round(el.scrollHeight - el.clientHeight),
  };
}
"""

# Record what the thumb's pointer stream actually delivers during the touch
# drag — on the unfixed build the tell is a `pointercancel` right after
# `pointerdown`, with at most one `pointermove` in between.
_WATCH_POINTER_EVENTS = """
() => {
  const thumb = document.querySelector('[data-testid="transcript-scrollbar-thumb"]');
  window.__thumbEvents = [];
  for (const type of ['pointerdown', 'pointermove', 'pointerup', 'pointercancel']) {
    thumb.addEventListener(type, (e) => {
      window.__thumbEvents.push(`${e.type}:${e.pointerType}`);
    });
  }
}
"""


def _seed_turns(session_id: str) -> None:
    """Write *_TURNS* committed exchanges straight into the store.

    Bypasses the runner and the model the same way
    :func:`tests.e2e_ui.conftest.seed_committed_turn` does — this test is about
    dragging a settled transcript's scrollbar, not about producing one.

    :param session_id: Session to append to, e.g. ``"conv_abc123"``.
    """
    from omnigent.entities import MessageData, NewConversationItem
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    items: list[NewConversationItem] = []
    for turn in range(_TURNS):
        response_id = f"resp_touchdrag_{turn:03d}"
        items.append(
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": f"prompt number {turn}"}],
                ),
            )
        )
        items.append(
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="assistant",
                    # Several paragraphs so the transcript overflows the tall
                    # tablet viewport and the thumb has real travel to map.
                    content=[
                        {
                            "type": "output_text",
                            "text": f"reply number {turn}\n\n"
                            + "\n\n".join(f"detail line {turn}.{line}" for line in range(6)),
                        }
                    ],
                    agent="hello_world",
                ),
            )
        )
    SqlAlchemyConversationStore(str(_server_state["database_uri"])).append(session_id, items)


def _park_at_bottom(page: Page) -> dict:
    """Pin the transcript to its bottom and return the settled scroll state."""
    page.eval_on_selector(
        "[data-pw-scroller]",
        "el => { el.scrollTop = el.scrollHeight; }",
    )
    page.wait_for_timeout(400)
    state = page.evaluate(_SCROLL_STATE)
    assert state["max"] - state["scrollTop"] <= 2, state
    return state


def _thumb_center(page: Page) -> tuple[float, float]:
    """Center point of the scrollbar thumb, in page coordinates."""
    box = page.locator(_THUMB).bounding_box()
    assert box is not None, "scrollbar thumb has no bounding box"
    return (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


def _drag_thumb_with_mouse(page: Page, dy: float) -> None:
    """Drag the thumb vertically by *dy* px using the mouse."""
    x, y = _thumb_center(page)
    page.mouse.move(x, y)
    page.mouse.down()
    steps = 8
    for i in range(1, steps + 1):
        page.mouse.move(x, y + dy * i / steps)
        page.wait_for_timeout(16)
    page.mouse.up()
    page.wait_for_timeout(300)


def _drag_thumb_with_touch(page: Page, dy: float) -> None:
    """Drag the thumb vertically by *dy* px through Chromium's native touch path.

    Dispatches real CDP touch events (the same input pipeline a finger uses),
    so the browser's touch-pan arbitration runs exactly as on a device — the
    behavior this bug lives in. Mirrors ``_scroll_with_native_touch`` in
    ``test_composer_growth_transcript_stability.py``.
    """
    session = page.context.new_cdp_session(page)
    try:
        session.send(
            "Emulation.setTouchEmulationEnabled",
            {"enabled": True, "maxTouchPoints": 1},
        )
        x, y = _thumb_center(page)
        point = {
            "x": round(x),
            "y": round(y),
            "id": 1,
            "radiusX": 2,
            "radiusY": 2,
            "force": 1,
        }
        session.send(
            "Input.dispatchTouchEvent",
            {"type": "touchStart", "touchPoints": [point]},
        )
        steps = 8
        for i in range(1, steps + 1):
            session.send(
                "Input.dispatchTouchEvent",
                {
                    "type": "touchMove",
                    "touchPoints": [{**point, "y": round(y + dy * i / steps)}],
                },
            )
            page.wait_for_timeout(16)
        session.send(
            "Input.dispatchTouchEvent",
            {"type": "touchEnd", "touchPoints": []},
        )
        page.wait_for_timeout(500)
    finally:
        session.detach()


def test_scrollbar_thumb_drags_by_touch(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A touch-drag on the scrollbar thumb must track the finger like a mouse drag."""
    base_url, session_id = seeded_session
    _seed_turns(session_id)

    browser = page.context.browser
    assert browser is not None
    context_args: dict = {"viewport": _VIEWPORT, "has_touch": True}
    # The autouse _record_video fixture only instruments the async Playwright
    # API; this sync-created context passes the record dir through directly so
    # a recorded run films the touch journey.
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        context_args["record_video_dir"] = record_dir
    touch_context = browser.new_context(**context_args)
    try:
        touch_page = touch_context.new_page()
        touch_page.goto(f"{base_url}/c/{session_id}")
        expect(touch_page.get_by_text(_NEWEST_REPLY).first).to_be_visible(timeout=30_000)

        assert touch_page.evaluate(_TAG_SCROLLER), "transcript did not overflow; seed more turns"
        touch_page.wait_for_timeout(500)

        thumb = touch_page.locator(_THUMB)
        expect(thumb).to_be_visible()

        # Reference: the same drag with a mouse. The thumb maps its travel
        # onto the full scroll range, so this measures how far a COMPLETED
        # 200px drag scrolls — the yardstick the touch drag must match.
        bottom = _park_at_bottom(touch_page)
        _drag_thumb_with_mouse(touch_page, dy=-200)
        after_mouse = touch_page.evaluate(_SCROLL_STATE)
        mouse_delta = bottom["scrollTop"] - after_mouse["scrollTop"]
        assert mouse_delta > 200, (
            "mouse drag on the thumb did not scroll — the thumb itself is "
            "broken, so this test cannot isolate the touch path",
            bottom,
            after_mouse,
        )

        # Re-park at the bottom, then perform the same drag by touch.
        bottom = _park_at_bottom(touch_page)
        touch_page.evaluate(_WATCH_POINTER_EVENTS)
        _drag_thumb_with_touch(touch_page, dy=-200)
        after_touch = touch_page.evaluate(_SCROLL_STATE)
        touch_delta = bottom["scrollTop"] - after_touch["scrollTop"]
        events = touch_page.evaluate("() => window.__thumbEvents")

        # The point of the bug: a touch drag anchored on the thumb must track
        # the finger the way the mouse drag tracks the cursor. On the unfixed
        # build the thumb's pointer stream is `pointerdown:touch`, at most one
        # `pointermove:touch`, then `pointercancel:touch` — native touch-pan
        # arbitration steals the gesture (the thumb declares no touch-action)
        # — so the transcript moves a small fraction of the mapped drag
        # instead of following it.
        assert touch_delta > mouse_delta * 0.5, (
            "touch drag on the scrollbar thumb did not track the finger "
            "(pointercancel killed the drag)",
            {"mouse_delta": mouse_delta, "touch_delta": touch_delta},
            bottom,
            after_touch,
            events,
        )
    finally:
        touch_context.close()
