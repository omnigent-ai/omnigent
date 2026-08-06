"""E2E: scrolling back through history must not fight the reader.

``HistoryAutoLoader`` used to write ``scrollTop`` after every history prepend
to hold the reader's position. An imperative write cancels in-flight scroll
momentum, so a page landing mid-flick yanked the transcript out from under
them — on a long real session, 32 corrections of up to ~2000px, every one
while the wheel was still moving. Native scroll anchoring performs the same
correction off the main thread, so the fix is to write nothing and let the
browser do it.

Neither half of that is visible below a browser. jsdom has no layout, no
scroll anchoring and no compositor, so a component test can assert the loader
does not call the setter but never that the transcript actually stays put —
and the scrollbar's thumb has no size there at all.

So this drives a real paginated transcript: park at the bottom, escape the
stick-to-bottom lock, then wheel up far enough to pull in older pages while
watching (a) whether anything assigns ``scrollTop``, and (b) whether the
scrollbar thumb ever changes size. Before the fix the first is ~30 and the
second is a thumb that shrinks a step per page.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _server_state

# Comfortably past SESSION_HISTORY_PAGE_SIZE (20 items) so the initial window
# is a fraction of the transcript and scrolling up must fetch more.
_TURNS = 30
_NEWEST_REPLY = f"reply number {_TURNS - 1}"

_VIEWPORT = {"width": 1280, "height": 480}

# Park at the bottom and tag the scroller: the tallest scrollable descendant
# of the log region, same shape the other transcript tests use.
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
  el.scrollTop = el.scrollHeight;
  return el.scrollHeight > el.clientHeight + 4;
}
"""

# Installed only after the reader has already scrolled up, so the
# stick-to-bottom lock is released and its own bottom-pinning writes — which
# this change does not touch — are not what we are counting.
_WATCH = """
() => {
  const el = document.querySelector('[data-pw-scroller]');
  const desc = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop');
  window.__writes = [];
  window.__thumbHeights = new Set();
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get() { return desc.get.call(this); },
    set(v) {
      window.__writes.push(Math.round(v - desc.get.call(this)));
      return desc.set.call(this, v);
    },
  });
  const sample = () => {
    const thumb = document.querySelector('[data-testid="transcript-scrollbar-thumb"]');
    if (thumb) window.__thumbHeights.add(Math.round(thumb.getBoundingClientRect().height));
    requestAnimationFrame(sample);
  };
  requestAnimationFrame(sample);
}
"""

_READING = """
() => ({
  writes: window.__writes,
  thumbHeights: [...window.__thumbHeights],
  rendered: document.querySelectorAll('[data-user-message-id]').length,
})
"""


def _seed_turns(session_id: str) -> None:
    """Write *_TURNS* committed exchanges straight into the store.

    Bypasses the runner and the model the same way
    :func:`tests.e2e_ui.conftest.seed_committed_turn` does — this test is about
    scrolling a settled transcript, not about producing one.

    :param session_id: Session to append to, e.g. ``"conv_abc123"``.
    """
    from omnigent.entities import MessageData, NewConversationItem
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    items: list[NewConversationItem] = []
    for turn in range(_TURNS):
        response_id = f"resp_scroll_{turn:03d}"
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
                    # Several lines so a handful of turns overflow the short
                    # viewport and the wheel has somewhere to travel.
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


def test_scrolling_back_through_history_never_moves_the_offset(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Paging older history writes no scrollTop and never resizes the thumb."""
    base_url, session_id = seeded_session
    _seed_turns(session_id)

    page.set_viewport_size(_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_text(_NEWEST_REPLY).first).to_be_visible(timeout=30_000)

    assert page.evaluate(_TAG_SCROLLER), "transcript did not overflow; seed more turns"
    page.wait_for_timeout(500)

    # Break the bottom lock first, so what we watch afterwards is only the
    # history path.
    page.mouse.move(_VIEWPORT["width"] // 2, _VIEWPORT["height"] // 2)
    page.mouse.wheel(0, -400)
    page.wait_for_timeout(400)

    page.evaluate(_WATCH)
    before = page.evaluate(_READING)["rendered"]

    # Wheel up in bursts, giving each fetch room to land mid-scroll — the
    # moment the old correction fired.
    for _ in range(30):
        for _ in range(10):
            page.mouse.wheel(0, -240)
            page.wait_for_timeout(16)
        page.wait_for_timeout(250)
        if page.evaluate(_READING)["rendered"] > before:
            break

    page.wait_for_timeout(1000)
    reading = page.evaluate(_READING)

    # The scroll must actually have pulled in older history, or the rest of
    # this proves nothing.
    assert reading["rendered"] > before, reading

    # The point of the change: position across a prepend belongs to the
    # browser, and nothing here reaches for the scroll offset.
    assert reading["writes"] == [], reading

    # A proportional thumb shrinks a step per page, because the document
    # genuinely lengthens; this one reports position only.
    assert len(reading["thumbHeights"]) == 1, reading
