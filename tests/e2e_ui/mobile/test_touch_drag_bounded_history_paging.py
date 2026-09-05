"""One touch gesture must not chain a runaway history-paging loop.

Opening a lengthy, tool-heavy agent session on the iOS app (a thin native
shell over this same server-served SPA) renders the loaded window as one
short screen: the giant settled tool turns mount folded behind "Worked for"
rows, so the reader effectively sits at the top with no scroll range. The
first small downward finger drag — the natural "peek at what's above"
gesture, or any incidental touch — then arms history paging, and because
every fetched page also mounts folded (near-zero height) the transcript
never crosses the load threshold: the pager chains page after page, the
"Loading earlier messages…" row keeps re-triggering, and dozens of /items
requests fire from a single gesture while the reader waits.

So: the open fetches its window exactly once and stays put, and one small
drag loads a bounded amount of history — never a runaway pagination loop.
"""

from __future__ import annotations

import json
import os
import time

from playwright.sync_api import Browser, expect

from tests.e2e_ui.conftest import seed_committed_items

# iPhone-13-class portrait profile with touch: the iOS app is a thin shell
# over this same SPA, so the journey is driven on the web lane at a phone
# viewport, matching the report's surface.
_VIEWPORT = {"width": 390, "height": 844}

# Older real text turns above the window — many 20-item history pages remain.
_OLD_TURNS = 30
# Tool calls per giant settled turn. Two such turns (~1000 items) mirror the
# reported lengthy Otto session whose recent history is one huge tool chain.
_CALLS_PER_BIG_TURN = 250

# Regression bound: one gesture may reasonably load a page or two of history,
# never a runaway chain. The buggy build fans out to ~47 pages from one drag.
_MAX_PAGES_PER_GESTURE = 3

# Paging is considered settled once no new /items request lands for this long.
_SETTLE_SECONDS = 3.0
_SETTLE_DEADLINE_SECONDS = 60.0


def _seed_tool_heavy_transcript(session_id: str) -> None:
    """Seed a long session whose recent turns are giant folded tool chains."""
    from omnigent.entities import (
        FunctionCallData,
        FunctionCallOutputData,
        MessageData,
        NewConversationItem,
    )

    items = []
    for turn in range(_OLD_TURNS):
        rid = f"resp_old_{turn:02d}"
        items.append(
            NewConversationItem(
                type="message",
                response_id=rid,
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": f"old prompt {turn}"}],
                ),
            )
        )
        items.append(
            NewConversationItem(
                type="message",
                response_id=rid,
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": f"old reply {turn}"}],
                    agent="hello_world",
                ),
            )
        )
    for big in range(2):
        rid = f"resp_big_{big}"
        items.append(
            NewConversationItem(
                type="message",
                response_id=rid,
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": f"do huge thing {big}"}],
                ),
            )
        )
        for i in range(_CALLS_PER_BIG_TURN):
            call_id = f"call_{big}_{i:04d}"
            items.append(
                NewConversationItem(
                    type="function_call",
                    response_id=rid,
                    data=FunctionCallData(
                        agent="hello_world",
                        name="shell",
                        arguments=json.dumps({"command": f"step {i}"}),
                        call_id=call_id,
                    ),
                )
            )
            items.append(
                NewConversationItem(
                    type="function_call_output",
                    response_id=rid,
                    data=FunctionCallOutputData(call_id=call_id, output=f"ok {i}"),
                )
            )
        items.append(
            NewConversationItem(
                type="message",
                response_id=rid,
                data=MessageData(
                    role="assistant",
                    content=[
                        {"type": "output_text", "text": f"FINAL SUMMARY {big}: huge thing done"}
                    ],
                    agent="hello_world",
                ),
            )
        )
    seed_committed_items(session_id, items)


def _track_items_requests(page, session_id: str) -> None:
    """Record every `/items` request the page makes, in order."""
    endpoint = f"/v1/sessions/{session_id}/items"
    page.add_init_script(
        f"""
        (() => {{
          const endpoint = {json.dumps(endpoint)};
          window.__itemsUrls = [];
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => {{
            const url = typeof input === "string" ? input : input.url;
            if (url.includes(endpoint)) window.__itemsUrls.push(url);
            return originalFetch(input, init);
          }};
        }})();
        """
    )


# One small downward finger drag on the transcript: the "show me what's above"
# gesture. The finger travels 60px down, well past the pager's 8px drag slop.
_TOUCH_DRAG = """
() => {
  const el = document.querySelector('[role="log"]').firstElementChild;
  const mk = (y) => new Touch({identifier: 1, target: el, clientX: 195, clientY: y});
  const fire = (type, y) => el.dispatchEvent(new TouchEvent(type, {
    bubbles: true, cancelable: true,
    touches: type === 'touchend' ? [] : [mk(y)],
    changedTouches: [mk(y)],
  }));
  fire('touchstart', 300);
  fire('touchmove', 330);
  fire('touchmove', 360);
  fire('touchend', 360);
}
"""


def test_one_touch_drag_loads_bounded_history(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """A single small touch drag must not chain dozens of history pages.

    Failure mode this catches: on a tool-heavy transcript the first small
    downward drag arms history paging, and folded (height-neutral) pages keep
    the pane under the load threshold, so the pager chains the entire history
    — ~47 "Loading earlier messages…" pages from one 60px gesture.

    :param browser: Playwright browser to open a touch phone context on.
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    _seed_tool_heavy_transcript(session_id)

    ctx_kwargs: dict = {
        "viewport": _VIEWPORT,
        "has_touch": True,
        "is_mobile": True,
    }
    # Film the journey when the recording harness asks for it. The autouse
    # _record_video fixture only patches the async API, and this test drives
    # the sync API through its own context, so honor the env var directly.
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        ctx_kwargs["record_video_dir"] = record_dir

    context = browser.new_context(**ctx_kwargs)
    try:
        page = context.new_page()
        _track_items_requests(page, session_id)
        page.goto(f"{base_url}/c/{session_id}")

        # The newest assistant summary hydrates into view.
        expect(page.get_by_text("FINAL SUMMARY 1", exact=False)).to_be_visible(timeout=30_000)

        # Hands off: the open fetches its whole window in exactly one request
        # and nothing more happens while the reader hasn't touched anything.
        page.wait_for_timeout(3_000)
        urls = page.evaluate("window.__itemsUrls")
        assert len(urls) == 1, urls

        # One small downward finger drag — the reader peeking at what's above.
        page.evaluate(_TOUCH_DRAG)

        # Let paging settle: wait until no new /items request lands for a
        # while, so a chaining regression is fully counted rather than raced.
        deadline = time.monotonic() + _SETTLE_DEADLINE_SECONDS
        last_count = page.evaluate("window.__itemsUrls.length")
        settled_for = 0.0
        while time.monotonic() < deadline and settled_for < _SETTLE_SECONDS:
            page.wait_for_timeout(500)
            current = page.evaluate("window.__itemsUrls.length")
            if current == last_count:
                settled_for += 0.5
            else:
                settled_for = 0.0
                last_count = current

        pages_loaded = last_count - 1
        # The drag must actually work: at least one page, never a runaway.
        assert pages_loaded >= 1, "the touch drag loaded no history at all; paging never armed"
        assert pages_loaded <= _MAX_PAGES_PER_GESTURE, (
            f"one 60px touch drag chained {pages_loaded} history-page requests "
            f"(expected at most {_MAX_PAGES_PER_GESTURE}); the reader asked to "
            f"peek up once, not to page in the whole transcript"
        )
        # And the loading row must not still be churning after the settle.
        expect(page.get_by_text("Loading earlier messages", exact=False)).to_have_count(0)
    finally:
        context.close()
