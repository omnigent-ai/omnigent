"""E2E: scrolling back through history must not fight the reader.

A history page prepends above what the reader is looking at. The transcript
is virtualized — its rows are absolutely positioned — so the browser's native
scroll anchoring cannot hold the read position across a prepend; the transcript
holds it itself, writing the scroll offset through stick-to-bottom so the write
is not mistaken for a reader gesture. What must be true, then, is not that
nothing writes ``scrollTop`` but that nothing the reader sees moves except by
the reader's own scrolling.

None of that is visible below a browser. jsdom has no layout, no scroll
anchoring and no compositor, and the scrollbar's thumb has no size there at all.

So these tests drive a real paginated transcript: park at the bottom, escape the
stick-to-bottom lock, then wheel up far enough to pull in older pages while
watching, per painted frame, whether any mounted row moves on screen by more
than the reader's own scrolling accounts for, and whether the scrollbar thumb
behaves as the visible share of the loaded document.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _server_state, configure_mock_llm

# Each turn is 2 items, so this must stay comfortably past
# INITIAL_WINDOW_ITEMS (100) — otherwise the open loads the whole transcript,
# nothing is left to page, and the scroll-up this test watches never happens.
_TURNS = 80
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
# stick-to-bottom lock is released. Every programmatic scroll write is logged
# with the offset it produced, so a frame's motion can be attributed.
_WATCH = """
() => {
  const el = document.querySelector('[data-pw-scroller]');
  const desc = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop');
  window.__writes = [];
  window.__thumbHeights = [];
  const log = (from, to) => window.__writes.push([
    Math.round(performance.now()), Math.round(from), Math.round(to), Math.round(desc.get.call(el)),
  ]);
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get() { return desc.get.call(this); },
    set(v) {
      const from = desc.get.call(this);
      desc.set.call(this, v);
      log(from, v);
    },
  });
  const origScrollTo = el.scrollTo.bind(el);
  el.scrollTo = (...args) => {
    const from = desc.get.call(el);
    const result = origScrollTo(...args);
    log(from, typeof args[0] === 'object' ? args[0].top : args[1]);
    return result;
  };
  const sample = () => {
    const thumb = document.querySelector('[data-testid="transcript-scrollbar-thumb"]');
    if (thumb) {
      const h = Math.round(thumb.getBoundingClientRect().height);
      const last = window.__thumbHeights[window.__thumbHeights.length - 1];
      if (last !== h) window.__thumbHeights.push(h);
    }
    requestAnimationFrame(sample);
  };
  requestAnimationFrame(sample);
}
"""

# Per painted frame: every mounted row's on-screen top by key, plus scrollTop,
# so a frame's scroll delta can be split into the reader's part and the code's.
_TRACK_ROWS = """
() => {
  const el = document.querySelector('[data-pw-scroller]');
  window.__rowSamples = [];
  const sample = () => {
    const rows = {};
    for (const r of el.querySelectorAll('[data-index]')) {
      rows[r.getAttribute('data-bubble-key')] = Math.round(r.getBoundingClientRect().top);
    }
    window.__rowSamples.push([Math.round(performance.now()), Math.round(el.scrollTop), rows]);
  };
  const tick = () => { setTimeout(sample, 0); requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
}
"""

_READING = """
() => {
  const el = document.querySelector('[data-pw-scroller]');
  return {
  writes: window.__writes,
  rowSamples: window.__rowSamples,
  thumbHeights: window.__thumbHeights,
  trackHeight: document.querySelector('[data-testid="transcript-scrollbar"]')
    .getBoundingClientRect().height,
  scrollHeight: el.scrollHeight,
  clientHeight: el.clientHeight,
  rendered: document.querySelectorAll('[data-user-message-id]').length,
  };
}
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


def _row_moves(
    samples: list[list[Any]],
    writers: list[list[int | str]],
    since: int,
) -> list[tuple[int, int]]:
    """On-screen moves of the mounted rows after *since* that the reader did not make.

    Per painted frame, the scroll delta minus the code's own writes is the
    reader's scrolling, which moves every row by the same amount the other way.
    The median leftover across rows present in both frames is content shifting
    under the reader.
    """
    moves: list[tuple[int, int]] = []
    for (t0, st0, rows0), (t1, st1, rows1) in pairwise(samples):
        if t1 < since:
            continue
        common = [key for key in rows0 if key in rows1]
        if not common:
            continue
        programmatic = sum(int(w[3]) - int(w[1]) for w in writers if t0 < int(w[0]) <= t1)
        reader = (st1 - st0) - programmatic
        leftovers = sorted((rows1[key] - rows0[key]) + reader for key in common)
        unexplained = leftovers[len(leftovers) // 2]
        if abs(unexplained) <= 4:
            continue
        if moves and moves[-1][1] == -unexplained and t1 - moves[-1][0] <= 20:
            moves.pop()
            continue
        moves.append((t1, unexplained))
    return moves


def test_scrolling_back_through_history_never_moves_the_offset(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Paging older text turns moves nothing on screen, and the thumb only shrinks as they land."""
    base_url, session_id = seeded_session
    _seed_turns(session_id)
    page_fetches: list[str] = []
    page.on("request", lambda r: page_fetches.append(r.url) if "after=" in r.url else None)

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
    page.evaluate(_TRACK_ROWS)
    t_start = page.evaluate("() => Math.round(performance.now())")

    # Wheel up in bursts, giving each fetch room to land mid-scroll — the
    # moment the old correction fired. The transcript is virtualized, so the
    # count of mounted rows says nothing about paging; the requests do.
    for _ in range(40):
        for _ in range(10):
            page.mouse.wheel(0, -240)
            page.wait_for_timeout(16)
        page.wait_for_timeout(250)
        if len(page_fetches) >= 2:
            break

    page.wait_for_timeout(1500)
    reading = page.evaluate(_READING)

    # The scroll must actually have pulled in older history, or the rest of
    # this proves nothing.
    assert len(page_fetches) >= 2, page_fetches

    # The point of the change: whatever holds the read position across a
    # prepend, nothing the reader sees moves except by the reader's own
    # scrolling — pages landing mid-flick included.
    moves = _row_moves(reading["rowSamples"], reading["writes"], t_start)
    assert moves == [], (moves, reading["writes"][:10])

    # The thumb is the visible share of the loaded document: it only shrinks as
    # pages lengthen the document, and ends up sized to what is loaded now.
    heights = reading["thumbHeights"]
    assert heights == sorted(heights, reverse=True), reading
    expected = max(
        56, round(reading["trackHeight"] * reading["clientHeight"] / reading["scrollHeight"])
    )
    assert abs(heights[-1] - expected) <= 2, (heights[-1], expected, reading)


# --- Tool-heavy transcripts: the view holds while pages land and stops with the reader ---

# Earlier turns: a prompt, a folded tool run, a reply. The newest turn alone
# spans INITIAL_WINDOW_ITEMS (100), so the open shows one folded "Worked for"
# row plus the last reply, and the next several pages merge into that fold —
# the shape that renamed the top bubble on each page and made the transcript
# bounce, and that left a reader with nothing new to read per flick.
_TOOL_TURNS = 10
_TOOL_PAIRS = 8
# Well past what a fixed page budget would reach: the initial window of 100 items
# leaves 140 of this turn's tool items above, i.e. seven pages that all fold into
# the turn already on screen before the previous turn's reply can appear.
_NEWEST_TOOL_PAIRS = 120
_NEWEST_TOOL_REPLY = "newest reply after the long tool run"
# Long enough that the transcript overflows the viewport on open, like a real
# session: it starts pinned to the bottom and the first wheel-up is a real
# scroll. (A transcript shorter than its viewport cannot scroll to hold position
# while its first pages land — the browser clamps — so that regime is not
# what this test is about.)
_NEWEST_TOOL_REPLY_BODY = (
    _NEWEST_TOOL_REPLY + "\n\n" + "\n\n".join(f"newest detail line {line}" for line in range(24))
)

_TALL_VIEWPORT = {"width": 1280, "height": 900}

# The transcript starts shorter than the viewport here, so tag the scroll
# element by its class rather than by overflow.
_TAG_TRANSCRIPT_SCROLLER = """
() => {
  const el = document.querySelector('.transcript-hide-native-scrollbar');
  if (!el) return false;
  el.setAttribute('data-pw-scroller', '1');
  return true;
}
"""

# Once per painted frame: the newest reply's on-screen top, the height of the
# "Loading earlier messages…" row (if shown), and scrollTop. Every programmatic
# scroll write is logged with the offset it actually produced, so a frame's
# scroll delta can be split into the reader's part and the code's part.
_TRACK_LANDMARK = """
(landmark) => {
  const el = document.querySelector('[data-pw-scroller]');
  window.__samples = [];
  window.__writers = [];
  const desc = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop');
  const log = (from, to, stack) => window.__writers.push([
    Math.round(performance.now()), Math.round(from), Math.round(to),
    Math.round(desc.get.call(el)), stack,
  ]);
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get() { return desc.get.call(this); },
    set(v) {
      const from = desc.get.call(this);
      desc.set.call(this, v);
      log(from, v, (new Error().stack || '').split('\\n').slice(2, 5).join(' < ').slice(0, 300));
    },
  });
  const origScrollTo = el.scrollTo.bind(el);
  el.scrollTo = (...args) => {
    const from = desc.get.call(el);
    const result = origScrollTo(...args);
    log(from, typeof args[0] === 'object' ? args[0].top : args[1], 'scrollTo');
    return result;
  };
  const find = () => {
    for (const p of el.querySelectorAll('p')) {
      if ((p.textContent || '').startsWith(landmark)) return p;
    }
    return null;
  };
  // ResizeObserver callbacks (the transcript's own compensation among them)
  // run after rAF and before paint; sampling from a timeout scheduled in rAF
  // reads what actually painted.
  const tick = () => {
    setTimeout(sample, 0);
    requestAnimationFrame(tick);
  };
  const sample = () => {
    const p = find();
    const indicator = el.querySelector('[role="status"]');
    const rows = [...el.querySelectorAll('[data-index]')].map((r) => {
      const rect = r.getBoundingClientRect();
      return (r.getAttribute('data-bubble-key') || '').slice(0, 24)
        + '@' + Math.round(rect.top) + 'x' + Math.round(rect.height);
    });
    window.__samples.push([
      Math.round(performance.now()),
      p ? Math.round(p.getBoundingClientRect().top) : null,
      indicator ? Math.round(indicator.getBoundingClientRect().height) : 0,
      Math.round(el.scrollTop),
      Math.round(el.getBoundingClientRect().top),
      el.scrollHeight,
      el.clientHeight,
      rows.join(' '),
    ]);
  };
  requestAnimationFrame(tick);
}
"""


def _seed_tool_heavy_turns(session_id: str) -> None:
    """Write committed tool-heavy exchanges straight into the store.

    :param session_id: Session to append to, e.g. ``"conv_abc123"``.
    """
    from omnigent.entities import (
        FunctionCallData,
        FunctionCallOutputData,
        MessageData,
        NewConversationItem,
    )
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    items: list[NewConversationItem] = []
    for turn in range(_TOOL_TURNS + 1):
        response_id = f"resp_tools_{turn:03d}"
        newest = turn == _TOOL_TURNS
        items.append(
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": f"tool prompt {turn}"}],
                ),
            )
        )
        for n in range(_NEWEST_TOOL_PAIRS if newest else _TOOL_PAIRS):
            call_id = f"{response_id}_call_{n:03d}"
            items.append(
                NewConversationItem(
                    type="function_call",
                    response_id=response_id,
                    data=FunctionCallData(
                        agent="hello_world",
                        name="shell",
                        arguments='{"cmd": "ls"}',
                        call_id=call_id,
                    ),
                )
            )
            items.append(
                NewConversationItem(
                    type="function_call_output",
                    response_id=response_id,
                    data=FunctionCallOutputData(call_id=call_id, output=f"listing {n}"),
                )
            )
        items.append(
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="assistant",
                    content=[
                        {
                            "type": "output_text",
                            "text": _NEWEST_TOOL_REPLY_BODY if newest else f"tool reply {turn}",
                        }
                    ],
                    agent="hello_world",
                ),
            )
        )
    SqlAlchemyConversationStore(str(_server_state["database_uri"])).append(session_id, items)


def _wheel_gestures(page: Page, *, count: int, delta_y: int) -> None:
    """Trackpad-like flicks: bursts of small ticks with a pause between them."""
    for _ in range(count):
        for _ in range(6):
            page.mouse.wheel(0, delta_y)
            page.wait_for_timeout(16)
        page.wait_for_timeout(400)


def _landmark_moves(
    samples: list[list[int | None]],
    writers: list[list[int | str]],
    since: int,
) -> list[tuple[int, int]]:
    """On-screen moves of the landmark after *since* that the reader did not make.

    Per painted frame, the scroll delta minus the code's own writes is the
    reader's scrolling, which moves the landmark by the same amount the other
    way. Anything left over is content shifting under the reader — a history
    page landing unheld, a lock snapping to the bottom, a row snapping from its
    size estimate.
    """
    moves: list[tuple[int, int]] = []
    for (t0, top0, _ind0, st0, *_), (t1, top1, _ind1, st1, *_) in pairwise(samples):
        if t1 < since or top0 is None or top1 is None:
            continue
        programmatic = sum(int(w[3]) - int(w[1]) for w in writers if t0 < int(w[0]) <= t1)
        reader = (st1 - st0) - programmatic
        unexplained = (top1 - top0) + reader
        if abs(unexplained) <= 4:
            continue
        # A sample can land between a DOM change and the compensation that runs
        # before that frame paints; the next sample then shows the exact
        # reverse. Neither was ever on screen.
        if moves and moves[-1][1] == -unexplained and t1 - moves[-1][0] <= 20:
            moves.pop()
            continue
        moves.append((t1, unexplained))
    return moves


def test_paging_a_tool_heavy_transcript_holds_the_view_and_stops_with_the_reader(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Scrolling up pages older history without moving what the reader sees.

    Before the fix, each page renamed the folded top bubble, the virtualizer
    treated it as a new row and "corrected" the scroll for a shift that never
    painted, stick-to-bottom snapped the view back down, and the loader read
    every such move as another scroll-up — so the transcript bounced and then
    kept paging and drifting upward with the reader's hands off the trackpad.
    """
    base_url, session_id = seeded_session
    _seed_tool_heavy_turns(session_id)

    page_fetches: list[str] = []
    page.on("request", lambda r: page_fetches.append(r.url) if "/items?" in r.url else None)
    page.set_viewport_size(_TALL_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_text(_NEWEST_TOOL_REPLY).first).to_be_visible(timeout=30_000)
    # Tool folds whose last activity is under RECENT_ACTIVITY_WINDOW_S (15s) old
    # mount expanded and collapse a few seconds later — a legitimate on-screen
    # shrink this test must not mistake for a yank. The seed is seconds old, so
    # let it age past that window before paging any of it in.
    page.wait_for_timeout(15_000)
    assert page.evaluate(_TAG_TRANSCRIPT_SCROLLER), "transcript scroller not found"
    page.evaluate(_TRACK_LANDMARK, _NEWEST_TOOL_REPLY)
    page.mouse.move(_TALL_VIEWPORT["width"] // 2, _TALL_VIEWPORT["height"] // 2)

    t_up_start = page.evaluate("() => Math.round(performance.now())")
    # One flick must bring the previous turn's reply into scrollback even though
    # seven pages of tool items fold into the newest turn first — a fixed page
    # budget stopped short and showed nothing new until the next flick.
    _wheel_gestures(page, count=1, delta_y=-100)
    expect(page.get_by_text(f"tool reply {_TOOL_TURNS - 1}").first).to_be_attached(timeout=5_000)
    _wheel_gestures(page, count=7, delta_y=-100)
    t_up_end = page.evaluate("() => Math.round(performance.now())")
    # A gesture may chain a bounded number of follow-up pages; give them room
    # to land, then require the transcript to be still with nobody touching it.
    page.wait_for_timeout(1500)
    fetches_settled = len(page_fetches)
    page.wait_for_timeout(1500)
    fetches_hands_off = len(page_fetches)

    _wheel_gestures(page, count=6, delta_y=100)
    t_down_end = page.evaluate("() => Math.round(performance.now())")
    page.wait_for_timeout(3000)
    fetches_final = len(page_fetches)

    # Ask for older history again, then scroll back down before it lands: the
    # page in flight may still land, but nothing chains after it and the view
    # must not move when it does.
    _wheel_gestures(page, count=3, delta_y=-100)
    fetches_before_leaving = len(page_fetches)
    for _ in range(6):
        page.mouse.wheel(0, 100)
        page.wait_for_timeout(16)
    t_left = page.evaluate("() => Math.round(performance.now())")
    page.wait_for_timeout(3000)
    fetches_after_leaving = len(page_fetches)
    samples = page.evaluate("() => window.__samples")
    writers = page.evaluate("() => window.__writers")

    # The scroll must actually have paged in older history.
    assert fetches_settled >= 3, page_fetches

    # Nothing may load while the reader's hands are off — before, the loader
    # read the settle's own upward corrections as more scrolling.
    assert fetches_hands_off == fetches_settled, page_fetches
    # Scrolling down and resting there asks for nothing either.
    assert fetches_final == fetches_hands_off, page_fetches

    # While scrolling up, a page landing must not throw the view around: the
    # bottom lock's snap and the runaway correction cascade were several hundred
    # px each. Smaller shifts during ACTIVE scrolling are the virtualizer's own
    # doing (a row scrolled into the window paints at its size estimate and
    # snaps when measured) and are outside this test; the hands-off assertions
    # below are the strict ones.
    up_moves = [
        (t, d) for t, d in _landmark_moves(samples, writers, t_up_start) if t <= t_up_end + 1500
    ]
    shifted = [(t, d) for t, d in up_moves if abs(d) >= 300]
    assert shifted == [], (
        shifted,
        [s for s in samples if shifted[0][0] - 300 <= s[0] <= shifted[0][0] + 60],
        [w for w in writers if shifted[0][0] - 300 <= int(w[0]) <= shifted[0][0] + 60],
    )

    # With hands off, the transcript is still — after scrolling up (once the
    # chained pages have landed) and after scrolling back down.
    still_after_up = [
        move
        for move in _landmark_moves(samples, writers, t_up_end + 1500)
        if move[0] < t_up_end + 3000
    ]
    assert still_after_up == [], still_after_up
    # Reaching the bottom re-engages stick-to-bottom, whose settle animation
    # runs for a few frames; after that, still.
    still_after_down = [
        move for move in _landmark_moves(samples, writers, t_down_end + 500) if move[0] < t_left
    ]
    assert still_after_down == [], still_after_down

    # Scrolling back down withdrew the request: at most the page already in
    # flight landed, and it landed without moving what the reader sees.
    assert fetches_after_leaving - fetches_before_leaving <= 1, page_fetches
    still_after_leaving = _landmark_moves(samples, writers, t_left + 300)
    assert still_after_leaving == [], still_after_leaving


# --- Pinned at the bottom: a streaming reply keeps the view on the newest text ---

_STREAMED_REPLY_WORDS = 160
_STREAMED_REPLY_DONE = "streamed reply complete marker"
_STREAM_PROMPT = "Tell me a long story, streamed."
# Seconds between streamed words on the mock LLM, so the reply grows over a
# few seconds and the view has to keep up rather than jump once.
_STREAM_CHUNK_DELAY_S = 0.03

# Per painted frame while the reply streams: how far the view sits above the
# bottom, and the document height.
_TRACK_BOTTOM = """
() => {
  const el = document.querySelector('[data-pw-scroller]');
  window.__bottomSamples = [];
  const sample = () => {
    window.__bottomSamples.push([
      Math.round(performance.now()),
      Math.round(el.scrollHeight - el.clientHeight - el.scrollTop),
      el.scrollHeight,
    ]);
  };
  const tick = () => { setTimeout(sample, 0); requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
}
"""


def test_streaming_reply_keeps_a_bottom_pinned_view_at_the_bottom(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A reader parked at the bottom sees each new line of a streaming reply."""
    base_url, session_id = seeded_session
    _seed_turns(session_id)
    # Long words wrap into many lines, so the reply grows in many steps.
    reply = " ".join(f"streamedword{n:03d}" for n in range(_STREAMED_REPLY_WORDS))
    reply = f"{reply} {_STREAMED_REPLY_DONE}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": reply, "stream": True, "chunk_delay": _STREAM_CHUNK_DELAY_S}],
        key="scroll-pin",
        match=_STREAM_PROMPT,
    )

    page.set_viewport_size(_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_text(_NEWEST_REPLY).first).to_be_visible(timeout=30_000)
    assert page.evaluate(_TAG_SCROLLER), "transcript did not overflow; seed more turns"
    page.wait_for_timeout(500)
    page.evaluate(_TRACK_BOTTOM)
    height_before = page.evaluate(
        "() => document.querySelector('[data-pw-scroller]').scrollHeight"
    )

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(_STREAM_PROMPT)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.get_by_text(_STREAMED_REPLY_DONE).first).to_be_visible(timeout=60_000)
    page.wait_for_timeout(1000)

    samples = page.evaluate("() => window.__bottomSamples")
    height_after = samples[-1][2]
    # The reply must actually have lengthened the transcript, or this proves nothing.
    assert height_after > height_before + 200, (height_before, height_after)

    # Ends at the bottom, on the newest text.
    assert samples[-1][1] <= 2, samples[-1]
    # And followed the reply as it grew.
    growing = [s for s in samples if s[2] > height_before]
    assert growing, samples[:3]
    # Each growth step lands a frame before stick-to-bottom's resize handler
    # scrolls to it, so one frame away from the bottom is normal. Staying away
    # is not: that is the view falling behind the reply.
    behind_ms = 0
    longest_behind_ms = 0
    for (t0, _d0, _h0), (t1, d1, _h1) in pairwise(growing):
        behind_ms = behind_ms + (t1 - t0) if d1 > 8 else 0
        longest_behind_ms = max(longest_behind_ms, behind_ms)
    assert longest_behind_ms <= 100, (longest_behind_ms, [s for s in growing if s[1] > 8][:20])
