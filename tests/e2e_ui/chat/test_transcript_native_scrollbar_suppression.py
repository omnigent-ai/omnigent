"""E2E: no native scrollbar (or reserved gutter strip) beside the transcript's
custom scrollbar while a reply streams.

Reported journey: open a session and send a message that produces a long
streamed reply; while the content height keeps changing, a blocky grey native
scrollbar paints at the right edge of the transcript, beside the custom
constant-height scrollbar thumb, and vanishes shortly after the turn settles.
Only the custom scrollbar (``TranscriptScrollbar``) may ever be visible there.

Mechanism (root-cause lead, verified live): use-stick-to-bottom renders the
transcript scroller with an inline ``scrollbar-gutter: stable both-edges``,
which directs the browser to keep native-scrollbar strips on both edges of the
viewport. The suppression class on the same element sets a plain
``scrollbar-width: none`` and never counters ``scrollbar-gutter``, so the
inline directive wins. Engines that keep that reservation (or flash a
scrollbar on content growth — e.g. macOS/Electron) paint the reported grey bar
in the reserved strip whenever the transcript grows.

Native scrollbars are not DOM elements and headless Chromium never rasterizes
them, so the bar itself cannot be screenshotted in CI. The test therefore
drives the real streaming journey and pins the user-facing invariant in its
deterministic form, sampling every rendered frame from send to settle:

* the scroller's effective scrollbar policy must never request native
  scrollbar gutters (computed ``scrollbar-gutter`` free of ``stable``) and
  must keep the native bar disabled (``scrollbar-width: none``) — the exact
  state that makes the bar paintable where it reproduces visually; and
* no frame may actually lay out a reserved strip (client width narrower than
  the border box, or the content column inset from either edge).

The test FAILS on the unfixed build (the inline gutter directive stays in
force through the whole streamed turn) and passes once the transcript
suppresses the native scrollbar completely.
"""

from __future__ import annotations

import json
import time

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _server_state, configure_mock_llm

_COMPOSER_LABEL = "Message the agent"
_THUMB = '[data-testid="transcript-scrollbar-thumb"]'
_VIEWPORT = {"width": 1400, "height": 800}

# Unique content-routing token: the mock LLM serves this test's streamed reply
# to whichever request carries it, so a parallel test can't drain the queue.
_TOKEN = "transcript-scrollbar-suppression-journey"

# ~700 words stream in as many deltas, growing the transcript well past one
# viewport so the (custom) scrollbar has real work to do.
_REPLY_WORDS = 700

# Per-frame sampler for the transcript scroller's scrollbar state. Installed
# before the message is sent so every rendered frame of the streaming turn is
# covered — the reported bar lives exactly in those frames. Each sample holds
# the computed scrollbar policy plus the measured strip geometry.
_START_SAMPLER = """
() => {
  const samples = [];
  const state = { samples, stopped: false };
  const tick = () => {
    if (state.stopped || samples.length >= 30000) return;
    const el = document.querySelector('.transcript-hide-native-scrollbar');
    if (el) {
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      const c = el.firstElementChild;
      const cr = c ? c.getBoundingClientRect() : null;
      samples.push({
        gutter: cs.scrollbarGutter,
        sbw: cs.scrollbarWidth,
        deficit: Math.round(r.width) - el.clientWidth,
        leftInset: cr ? Math.round(cr.left - r.left) : 0,
        rightInset: cr ? Math.round(r.right - cr.right) : 0,
        scrollH: el.scrollHeight,
        clientH: el.clientHeight,
      });
    }
    requestAnimationFrame(tick);
  };
  window.__scrollbarSampler = state;
  requestAnimationFrame(tick);
}
"""

_STOP_SAMPLER = """
() => {
  const state = window.__scrollbarSampler;
  if (!state) return [];
  state.stopped = true;
  return state.samples;
}
"""


def _settled_scroll_height(page: Page, timeout_s: float = 15.0) -> int:
    """Poll until two consecutive scrollHeight reads agree, then return it."""
    read = "() => document.querySelector('.transcript-hide-native-scrollbar').scrollHeight"
    previous = page.evaluate(read)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        page.wait_for_timeout(250)
        current = page.evaluate(read)
        if current == previous:
            return current
        previous = current
    raise AssertionError(f"transcript never settled; last scrollHeight: {previous}")


def test_streaming_reply_must_not_summon_native_scrollbar(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A streaming turn must never leave the native scrollbar paintable
    beside the transcript's custom scrollbar."""
    base_url, session_id = seeded_session
    mock_url = str(_server_state["mock_llm_url"])

    reply = " ".join(f"streamword{n:04d}" for n in range(_REPLY_WORDS))
    configure_mock_llm(
        mock_url,
        [{"text": reply, "stream": True}],
        key="transcript-scrollbar-suppression",
        match=_TOKEN,
    )

    page.set_viewport_size(_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)
    page.wait_for_timeout(500)

    # Sample every rendered frame from before the send until after the settle.
    page.evaluate(_START_SAMPLER)

    composer.fill(f"please stream a long reply {_TOKEN}")
    page.get_by_role("button", name="Send", exact=True).click()

    # The reply streams to completion and the transcript settles.
    expect(page.get_by_text(f"streamword{_REPLY_WORDS - 1:04d}").first).to_be_visible(
        timeout=90_000
    )
    settled_scroll_h = _settled_scroll_height(page)

    samples: list[dict] = page.evaluate(_STOP_SAMPLER)
    assert len(samples) >= 30, f"sampler captured too few frames: {len(samples)}"

    # Preconditions: this run exercised the reported journey. The content
    # height kept changing while the reply streamed, the transcript ended up
    # genuinely scrollable, and the custom scrollbar thumb is on screen.
    heights = {s["scrollH"] for s in samples}
    assert len(heights) >= 3, (
        f"reply did not stream progressively (scrollHeight values seen: {sorted(heights)})"
    )
    client_h = samples[-1]["clientH"]
    assert settled_scroll_h > client_h + 200, (
        f"reply too short to overflow the transcript "
        f"(scrollHeight {settled_scroll_h}, clientHeight {client_h})"
    )
    expect(page.locator(_THUMB)).to_be_visible()

    # The bug: while the transcript grew (and at rest), the scroller kept a
    # scrollbar policy that lets the native bar paint in a reserved gutter
    # strip beside the custom thumb — or actually laid such a strip out.
    gutter_violations = [s for s in samples if "stable" in s["gutter"]]
    width_violations = [s for s in samples if s["sbw"] != "none"]
    strip_violations = [
        s for s in samples if s["deficit"] > 0 or s["leftInset"] > 0 or s["rightInset"] > 0
    ]
    assert not gutter_violations and not width_violations and not strip_violations, (
        "native scrollbar not fully suppressed on the streaming transcript: "
        f"{len(gutter_violations)}/{len(samples)} frames reserve native scrollbar "
        "gutters (computed scrollbar-gutter "
        f"{sorted({s['gutter'] for s in gutter_violations or samples})}), "
        f"{len(width_violations)} frames re-enable the native bar, and "
        f"{len(strip_violations)} frames laid out a reserved strip "
        f"(first: {json.dumps((strip_violations or samples)[0])}). "
        "Only the custom TranscriptScrollbar may render at the transcript's "
        "edge; any reserved gutter lets the grey native bar paint beside it "
        "while a reply streams."
    )
