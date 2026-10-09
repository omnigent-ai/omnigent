"""A bottom-pinned reader sees each reply the moment it lands, even on a slow main thread."""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Page, expect

from tests.e2e_ui.chat.test_transcript_scroll_stability import (
    _NEWEST_REPLY,
    _TAG_SCROLLER,
    _VIEWPORT,
    _seed_turns,
)
from tests.e2e_ui.conftest import configure_mock_llm

# Applied through CDP once the seeded history is on screen, so the journey runs
# at roughly the speed of a loaded CI runner.
_CPU_THROTTLE_RATE = 6
# Whether a landing paints before its correction is a per-landing race, so
# several replies land in one session.
_LANDINGS = 4
_BEHIND_BUDGET_MS = 100
_BOTTOM_EPSILON_PX = 8
_REPLY_WORDS = 160
_STREAM_CHUNK_DELAY_S = 0.03
_PROMPT = "Tell me a long story, streamed."
_DONE_MARKER = "streamed reply complete marker"
_WORKING = '[data-testid="working-indicator"]'
# Script phases that run after a frame starts and before it paints.
_PRE_PAINT_PHASES = ("raf", "ro")

# Installed before the app loads: names the phase each scroll write runs in.
# Animation-frame callbacks (with their microtasks) and ResizeObserver callbacks
# run after a frame starts and before it paints; anything else is a plain task.
_TAG_PHASES = """
(() => {
  window.__phase = 'task';
  const enter = (phase, run) => {
    window.__phase = phase;
    try { return run(); } finally { queueMicrotask(() => { window.__phase = 'task'; }); }
  };
  const rAF = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = (cb) => rAF((ts) => enter('raf', () => cb(ts)));
  const RO = window.ResizeObserver;
  window.ResizeObserver = class extends RO {
    constructor(cb) { super((entries, observer) => enter('ro', () => cb(entries, observer))); }
  };
})();"""

# At the start of every frame, and on every programmatic scroll write: how far
# the view sits above the bottom, the document height, and (for writes) the
# phase. A frame paints its starting reading unless a pre-paint write moves it.
_TRACK_BOTTOM = """
() => {
  const el = document.querySelector('[data-pw-scroller]');
  const desc = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop');
  const offset = () => Math.round(el.scrollHeight - el.clientHeight - el.scrollTop);
  const reading = () => [Math.round(performance.now()), offset(), el.scrollHeight];
  window.__reset = () => { window.__frames = []; window.__writes = []; };
  window.__reset();
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get() { return desc.get.call(this); },
    set(v) { desc.set.call(this, v); window.__writes.push([...reading(), window.__phase]); },
  });
  const origScrollTo = el.scrollTo.bind(el);
  el.scrollTo = (...args) => {
    const result = origScrollTo(...args);
    window.__writes.push([...reading(), window.__phase]);
    return result;
  };
  const tick = () => {
    window.__frames.push(reading());
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}"""


def _land_reply(page: Page, marker: str) -> tuple[int, list[list[int]], list[list[Any]]]:
    """Send the prompt and wait for the reply ending in *marker*; return the transcript
    height before the send, then the frame and scroll-write readings taken meanwhile."""
    height_before = page.evaluate(
        "() => document.querySelector('[data-pw-scroller]').scrollHeight"
    )
    page.evaluate("() => window.__reset()")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(_PROMPT)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.get_by_text(marker).first).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
    page.wait_for_timeout(1000)
    frames = page.evaluate("() => window.__frames")
    writes = page.evaluate("() => window.__writes")
    return height_before, frames, writes


def _painted_frames(frames: list[list[int]], writes: list[list[Any]]) -> list[list[int]]:
    """What each frame painted: its starting reading, unless a write in the
    frame's animation-frame or resize-observer phase moved the view first."""
    painted: list[list[int]] = []
    for i, (t, off, height) in enumerate(frames):
        t_next = frames[i + 1][0] if i + 1 < len(frames) else float("inf")
        pre_paint = [w for w in writes if t <= w[0] < t_next and w[3] in _PRE_PAINT_PHASES]
        if pre_paint:
            off, height = pre_paint[-1][1], pre_paint[-1][2]
        painted.append([t, off, height])
    return painted


def _behind_after_landing(
    frames: list[list[int]], writes: list[list[Any]], height_before: int
) -> tuple[int, list[Any]]:
    """Longest span the view stayed painted off the bottom once the reply landed,
    ending at the first scroll write or later painted frame back within the epsilon."""
    # The prompt bubble lands first and is small; the reply is most of the growth.
    threshold = height_before + (frames[-1][2] - height_before) / 2
    landed = [f for f in frames if f[2] >= threshold]
    longest = 0
    detail: list[Any] = []
    for i, (t, off, _h) in enumerate(landed):
        if off <= _BOTTOM_EPSILON_PX:
            continue
        back = [w[0] for w in writes if w[0] > t and w[1] <= _BOTTOM_EPSILON_PX]
        back += [f[0] for f in landed[i + 1 :] if f[1] <= _BOTTOM_EPSILON_PX]
        if not back:
            continue
        span = min(back) - t
        if span > longest:
            longest = span
            detail = [
                ("painted off-bottom", t, off),
                ("frames", landed[i : i + 4]),
                ("writes", [w for w in writes if t - 50 <= w[0] <= min(back) + 50]),
            ]
    return longest, detail


def test_reply_landing_keeps_a_bottom_pinned_view_at_the_bottom_on_a_slow_main_thread(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A reader parked at the bottom is on the newest text soon after each reply lands."""
    base_url, session_id = seeded_session
    _seed_turns(session_id)
    words = " ".join(f"streamedword{n:03d}" for n in range(_REPLY_WORDS))
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": f"{words} {_DONE_MARKER} {n}",
                "stream": True,
                "chunk_delay": _STREAM_CHUNK_DELAY_S,
            }
            for n in range(1, _LANDINGS + 1)
        ],
        key="scroll-pin-slow",
        match=_PROMPT,
    )

    page.add_init_script(_TAG_PHASES)
    page.set_viewport_size(_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_text(_NEWEST_REPLY).first).to_be_visible(timeout=30_000)
    assert page.evaluate(_TAG_SCROLLER), "transcript did not overflow; seed more turns"
    page.wait_for_timeout(500)
    page.evaluate(_TRACK_BOTTOM)
    page.context.new_cdp_session(page).send(
        "Emulation.setCPUThrottlingRate", {"rate": _CPU_THROTTLE_RATE}
    )

    spans: list[tuple[int, list[Any]]] = []
    for n in range(1, _LANDINGS + 1):
        height_before, frames, writes = _land_reply(page, f"{_DONE_MARKER} {n}")
        painted = _painted_frames(frames, writes)
        # The reply must actually have lengthened the transcript, or this proves nothing.
        assert painted[-1][2] > height_before + 200, (height_before, painted[-1])
        assert painted[-1][1] <= 2, painted[-1]
        spans.append(_behind_after_landing(painted, writes, height_before))

    longest_behind_ms, detail = max(spans, key=lambda s: s[0])
    assert longest_behind_ms <= _BEHIND_BUDGET_MS, (
        longest_behind_ms,
        [s[0] for s in spans],
        detail,
    )
