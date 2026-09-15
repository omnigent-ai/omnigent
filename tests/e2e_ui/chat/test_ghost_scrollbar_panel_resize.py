"""E2E: no ghost scrollbar on a transcript that fully fits the viewport.

Reported journey: with a short conversation fully visible (no scrollbar),
dragging the right workspace panel's resize handle makes a scrollbar thumb
appear over the blank space below the last message — and it stays after the
drag ends, even though the entire conversation is still on screen. Dragging
that thumb across its whole track scrolls essentially nothing, so its size
and position misrepresent the content (the "scrollbar height" half of the
report).

Mechanism (root-cause lead, verified live): ``LatestTurnSpacer`` reserves
``viewport − anchorToEnd − topGapPx`` (topGap 96px), but the transcript
content column carries pt-20 (80px) + pb-6 (24px) = 104px of its own padding,
so whenever the spacer is active below its ⅓-viewport cap the content totals
``viewport + 8`` — a constant 8px phantom scroll range. ``TranscriptScrollbar``
paints its constant 56px thumb for any range ≥ 1px, so those phantom 8px
draw a full scrollbar. Narrowing the chat column (the panel drag) re-wraps
the reply taller, which moves the needed spacer below the cap mid-drag —
which is why the ghost pops in on panel resize.

Both tests assert the user-facing invariant: a transcript that is entirely
visible (nothing hidden above or below) must not paint a scrollbar thumb.
They FAIL on the unfixed build and pass once the phantom overflow (or the
scrollbar's paint threshold) is fixed.
"""

from __future__ import annotations

import json

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _server_state

_VIEWPORT = {"width": 1400, "height": 800}

_THUMB = '[data-testid="transcript-scrollbar-thumb"]'

# Transcript scroll state + visual-fit measurements, read in one evaluate so a
# mid-drag sample is internally consistent. `max` is the scrollable range;
# `lastMsgBottom`/`viewportBottom` establish whether the conversation is
# entirely on screen (with nothing hidden above: this seed has a single turn
# and the scroller is parked at the bottom).
_STATE = """
() => {
  const el = document.querySelector('.transcript-hide-native-scrollbar');
  const thumb = document.querySelector('[data-testid="transcript-scrollbar-thumb"]');
  const msgs = el ? [...el.querySelectorAll('[data-role]')] : [];
  const first = msgs[0];
  const last = msgs[msgs.length - 1];
  const rect = el ? el.getBoundingClientRect() : null;
  return {
    max: el ? el.scrollHeight - el.clientHeight : null,
    scrollTop: el ? Math.round(el.scrollTop) : null,
    thumbVisible: !!thumb,
    thumbRect: thumb ? (() => { const r = thumb.getBoundingClientRect();
      return {x: Math.round(r.x), y: Math.round(r.y), h: Math.round(r.height)}; })() : null,
    firstMsgTop: first ? Math.round(first.getBoundingClientRect().top) : null,
    lastMsgBottom: last ? Math.round(last.getBoundingClientRect().bottom) : null,
    viewportTop: rect ? Math.round(rect.top) : null,
    viewportBottom: rect ? Math.round(rect.bottom) : null,
    chatW: rect ? Math.round(rect.width) : null,
  };
}
"""


def _seed_single_turn(session_id: str, words: int) -> None:
    """Write one committed exchange whose reply is *words* space-separated words.

    Bypasses the runner/model the same way the other transcript tests do —
    this test is about the scrollbar over a settled transcript, not about
    producing one.

    :param session_id: Session to append to, e.g. ``"conv_abc123"``.
    :param words: Reply length; picks where the re-wrap boundary falls.
    """
    from omnigent.entities import MessageData, NewConversationItem
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    text = " ".join(f"word{n:04d}" for n in range(words))
    items = [
        NewConversationItem(
            type="message",
            response_id="resp_ghost_scrollbar_000",
            data=MessageData(
                role="user",
                content=[{"type": "input_text", "text": "please summarize the layout system"}],
            ),
        ),
        NewConversationItem(
            type="message",
            response_id="resp_ghost_scrollbar_000",
            data=MessageData(
                role="assistant",
                content=[{"type": "output_text", "text": text}],
                agent="hello_world",
            ),
        ),
    ]
    SqlAlchemyConversationStore(str(_server_state["database_uri"])).append(session_id, items)


def _fully_visible(state: dict) -> bool:
    """The whole (single-turn) conversation is on screen with room to spare."""
    return (
        state["firstMsgTop"] is not None
        and state["lastMsgBottom"] is not None
        and state["firstMsgTop"] >= state["viewportTop"]
        and state["lastMsgBottom"] <= state["viewportBottom"] - 40
    )


def _demonstrate_thumb_drag(page: Page) -> None:
    """For the camera: sweep the ghost thumb across its whole track.

    On the unfixed build the transcript barely moves (the phantom range is
    ~8px), which is the user-visible absurdity. No-op when there is no thumb.
    """
    thumb = page.locator(_THUMB)
    if thumb.count() == 0:
        return
    box = thumb.bounding_box()
    if box is None:
        return
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.wait_for_timeout(400)
    page.mouse.down()
    page.mouse.move(x, 100, steps=20)
    page.wait_for_timeout(400)
    page.mouse.move(x, y, steps=20)
    page.mouse.up()
    page.wait_for_timeout(400)


def test_panel_resize_must_not_summon_ghost_scrollbar(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Dragging the workspace panel wider must not paint a scrollbar over a
    conversation that still fully fits the viewport."""
    base_url, session_id = seeded_session
    # 95 words: at the initial chat width the reply fits with no scroll range;
    # once the panel drag narrows the column it re-wraps taller, crossing the
    # spacer's cap boundary — where the unfixed build gains the phantom 8px.
    _seed_single_turn(session_id, words=95)

    page.set_viewport_size(_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_text("word0000").first).to_be_visible(timeout=30_000)
    page.wait_for_timeout(1200)

    workspace = page.get_by_role("complementary", name="Workspace")
    expect(workspace).to_be_visible()

    before = page.evaluate(_STATE)
    assert _fully_visible(before), f"seed too long for the viewport: {before}"

    # Drag the panel's left-edge resize handle to widen the panel (narrowing
    # the chat column), sampling the scrollbar state along the way.
    handle = workspace.locator('[aria-label="Resize panel"]')
    box = handle.bounding_box()
    assert box is not None
    hx = box["x"] + box["width"] / 2
    hy = box["y"] + box["height"] / 2

    violations: list[dict] = []
    page.mouse.move(hx, hy)
    page.mouse.down()
    for dx in range(0, -380, -20):
        page.mouse.move(hx + dx, hy, steps=2)
        page.wait_for_timeout(60)
        st = page.evaluate(_STATE)
        if st["thumbVisible"] and st["max"] is not None and st["max"] <= 24 and _fully_visible(st):
            violations.append({"dx": dx, **st})
    page.mouse.up()
    page.wait_for_timeout(600)
    after = page.evaluate(_STATE)

    # Precondition: the drag actually re-wrapped the reply (the column
    # narrowed and the last message moved down) — otherwise this run never
    # exercised the reported journey.
    assert after["chatW"] < before["chatW"] - 40, (before, after)
    assert after["lastMsgBottom"] > before["lastMsgBottom"], (before, after)
    # The conversation still fully fits after the resize.
    assert _fully_visible(after), f"reply no longer fits; shorten the seed: {after}"

    # For the camera: show that dragging the ghost thumb scrolls nothing.
    _demonstrate_thumb_drag(page)

    # The bug: a scrollbar thumb painted over a fully-visible conversation —
    # during the drag or persisting after it. Settled state: the conversation
    # fully fits (asserted above), so *any* thumb is a ghost no matter how
    # large the phantom range grew. Mid-drag samples keep the small-range cap:
    # a stale frame between the re-wrap and the spacer's ResizeObserver write
    # can show a transient real-looking range that settles within a step.
    ghost_after = after["thumbVisible"]
    assert not ghost_after and not violations, (
        "ghost scrollbar on panel resize: thumb painted while the whole "
        f"conversation is visible (after={json.dumps(after)}, "
        f"first_mid_drag={json.dumps(violations[0]) if violations else None})"
    )


def test_fully_visible_transcript_paints_no_scrollbar(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A conversation that entirely fits on screen must not show a scrollbar
    thumb at rest — and the thumb's geometry must never imply hidden content
    that does not exist (the report's wrong-"scrollbar height" half)."""
    base_url, session_id = seeded_session
    # 140 words: still fits the viewport with ~180px to spare, but on the
    # unfixed build the spacer's +8px phantom range exists already at rest,
    # so the (constant-height) thumb renders parked near the track's bottom —
    # advertising a screenful of hidden content below when there is none.
    _seed_single_turn(session_id, words=140)

    page.set_viewport_size(_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_text("word0000").first).to_be_visible(timeout=30_000)
    page.wait_for_timeout(1200)

    state = page.evaluate(_STATE)
    assert _fully_visible(state), f"seed too long for the viewport: {state}"

    # For the camera: sweeping the ghost thumb across its whole ~600px track
    # scrolls the phantom ~8px — the transcript visibly does not move.
    _demonstrate_thumb_drag(page)

    # The conversation fully fits (asserted above), so any thumb — whatever
    # the phantom range — advertises hidden content that does not exist.
    ghost = state["thumbVisible"]
    assert not ghost, (
        "scrollbar painted for a transcript that fully fits the viewport "
        f"(scroll range {state['max']}px, thumb={json.dumps(state['thumbRect'])})"
    )
