"""E2E: no native scrollbar may paint beside the transcript's custom one.

Reported journey: send a message that produces a long
streamed reply and watch the right edge of the transcript while the content
height keeps changing — a blocky grey native scrollbar paints in a reserved
strip beside the custom constant-height ``TranscriptScrollbar`` thumb, then
vanishes when the turn settles. Observed on macOS (Electron/Chrome), both
themes; the bar is not a DOM element, so it can't be picked in DevTools.

Mechanism (root-cause lead, confirmed live on the unfixed build):
``use-stick-to-bottom`` renders the transcript scroller with an **inline**
``scrollbar-gutter: stable both-edges``, and the suppression class
``.transcript-hide-native-scrollbar`` (web/src/index.css) uses plain
(non-``!important``) rules, so the inline gutter directive survives on the
scroller. Wherever the engine gives that directive effect — classic-scrollbar
platforms, engines that don't honour ``scrollbar-width: none`` — the reserved
strip exists and the native bar paints in it while content grows.

The exact pixel artifact is engine-dependent: this suite's Linux Chromium
honours ``scrollbar-width: none`` (verified by control: even a fully
unsuppressed scroller reserves a 30px gutter but rasterizes no scrollbar
pixels headless, and paints a grey thumb only headed), so the test asserts
the cross-platform user-facing invariant instead of pixels:

1. while and after a streamed reply grows the transcript, the scroller must
   never reserve scrollbar-gutter strips (``offsetWidth == clientWidth``
   every animation frame), and
2. the effective computed style on the scroller must fully suppress native
   scrollbars *in a way that beats the library's inline style*: computed
   ``scrollbar-width`` is ``none`` and computed ``scrollbar-gutter`` reserves
   no stable edge strips.

On the unfixed build (2) FAILS — computed ``scrollbar-gutter`` is
``stable both-edges`` — which is precisely the state that paints the stray
bar on the reporter's platform. With the fix (forcing every knob off with
``!important``, incl. ``scrollbar-gutter: auto``) both hold and the test
passes.
"""

from __future__ import annotations

import json

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_THUMB = '[data-testid="transcript-scrollbar-thumb"]'

_VIEWPORT = {"width": 1400, "height": 800}

# Routed by content so the queue fires exactly for this test's turn.
_PROMPT = "please stream me a very long story about scrollbars"
_MATCH = "very long story about scrollbars"
_REPLY_WORDS = 700
_LAST_WORD = f"streamword{_REPLY_WORDS - 1:04d}"

# The transcript scroller: the StickToBottom.Content viewport that carries
# both the suppression class and the library's inline scrollbar-gutter.
# Scoped under the conversation's role="log" root so a second scroller
# elsewhere can never shadow it.
_SCROLLER = '[role="log"] .transcript-hide-native-scrollbar'

# Per-frame recorder: reserved-gutter width and content height, sampled on
# every animation frame for the whole streamed turn. offsetWidth-clientWidth
# is the layout footprint of the native scrollbar/gutter strips — 0 only
# when no strip is reserved (borders are 0 on this element).
_RAF_RECORDER = f"""
() => {{
  const el = document.querySelector('{_SCROLLER}');
  window.__gutterSamples = [];
  const loop = () => {{
    window.__gutterSamples.push([el.offsetWidth - el.clientWidth, el.scrollHeight]);
    if (window.__gutterSamples.length < 100000) requestAnimationFrame(loop);
  }};
  requestAnimationFrame(loop);
}}
"""

_STATE = f"""
() => {{
  const el = document.querySelector('{_SCROLLER}');
  if (!el) return null;
  const cs = getComputedStyle(el);
  return {{
    scrollbarWidth: cs.scrollbarWidth,
    scrollbarGutter: cs.scrollbarGutter,
    inlineGutter: el.style.scrollbarGutter,
    gutterPx: el.offsetWidth - el.clientWidth,
    scrollHeight: el.scrollHeight,
    clientHeight: el.clientHeight,
  }};
}}
"""


def test_streaming_turn_must_not_admit_native_scrollbar(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A long streamed reply must never give the native scrollbar a home
    beside the custom transcript scrollbar."""
    base_url, session_id = seeded_session
    reply = " ".join(f"streamword{n:04d}" for n in range(_REPLY_WORDS))
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": reply, "stream": True}],
        key="stray-native-scrollbar",
        match=_MATCH,
    )

    page.set_viewport_size(_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)

    # Record the scroller's gutter footprint on every frame from before the
    # send until after the turn settles — the reported bar shows up exactly
    # while the content height keeps changing.
    page.evaluate(_RAF_RECORDER)

    composer.fill(_PROMPT)
    page.get_by_role("button", name="Send", exact=True).click()

    # Turn end: the reply's last word rendered and the working shimmer gone.
    expect(page.get_by_text(_LAST_WORD).first).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
    page.wait_for_timeout(500)

    state = page.evaluate(_STATE)
    assert state is not None, "transcript scroller not found"
    raf = page.evaluate("() => window.__gutterSamples")

    # --- Journey preconditions: this run really exercised the report's
    # trigger. The reply overflowed the viewport, the custom scrollbar is
    # painting, and the content height kept changing while streaming.
    assert state["scrollHeight"] > state["clientHeight"] + 200, state
    expect(page.locator(_THUMB)).to_be_visible()
    heights = sorted({h for _, h in raf})
    assert len(heights) >= 3, f"reply did not stream (content height never changed): {heights}"

    # --- The bug, part 1 (layout): at no frame during or after the stream
    # may the scroller reserve gutter strips for a native scrollbar. On
    # engines where the reserved strip appears (the reported macOS/Electron
    # rendering) the native bar paints inside it beside the custom thumb.
    gutters = sorted({g for g, _ in raf} | {state["gutterPx"]})
    assert gutters == [0], (
        "native scrollbar gutter reserved on the transcript scroller while "
        f"the reply streamed (offsetWidth-clientWidth saw {gutters}px over "
        f"{len(raf)} frames) — a native bar paints in that strip beside the "
        "custom transcript scrollbar"
    )

    # --- The bug, part 2 (effective style): the suppression must beat the
    # library's inline `scrollbar-gutter: stable both-edges`. On the unfixed
    # build the inline style survives (computed scrollbar-gutter is
    # `stable both-edges`), which is exactly what hands the native scrollbar
    # a reserved strip beside the custom thumb on classic-scrollbar
    # platforms — the reported stray grey bar.
    assert state["scrollbarWidth"] == "none", (
        f"native scrollbar not suppressed on the transcript scroller: {json.dumps(state)}"
    )
    assert "stable" not in state["scrollbarGutter"], (
        "the transcript scroller still reserves native scrollbar gutters: "
        f"computed scrollbar-gutter is {state['scrollbarGutter']!r} (the "
        "use-stick-to-bottom inline style beat .transcript-hide-native-"
        "scrollbar), so a stray native scrollbar can paint beside the "
        f"custom transcript scrollbar while a session streams: {json.dumps(state)}"
    )
