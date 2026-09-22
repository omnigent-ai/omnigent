"""E2E: no native scrollbar (or reserved gutter strip) beside the custom one.

Reported journey: while a long reply streams and the transcript keeps growing,
a blocky grey native scrollbar paints at the transcript's right edge next to
the custom constant-height thumb, then vanishes when the turn settles. Only
the custom TranscriptScrollbar should ever be visible.

The scrollbar itself is unreachable below the compositor: headless Chromium
rasterizes no native scrollbar pixels, and it drops the gutter reservation
whenever scrollbar-width:none applies, so unfixed and fixed builds screenshot
identically here. What is assertable everywhere is the state that makes the
bar paintable at all on renderers with classic scrollbars: the transcript
scroller still demands a stable scrollbar gutter (the stick-to-bottom
library's inline `scrollbar-gutter: stable both-edges` survives the
`.transcript-hide-native-scrollbar` suppression class) even though the same
class turns the native scrollbar off. The test drives the reported journey,
samples the scroller per painted frame while the reply grows it, and asserts
full native-scrollbar suppression — computed style and geometry — throughout
and after the turn settles. It FAILS on the unfixed build (computed
scrollbar-gutter stays "stable both-edges") and passes once the suppression
really covers every knob.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_VIEWPORT = {"width": 1280, "height": 720}

_PROMPT = "Tell me a very long streamed story."
_DONE_MARKER = "gutter story complete marker"
_REPLY_WORDS = 400

_THUMB = '[data-testid="transcript-scrollbar-thumb"]'

# Per painted frame: the scroller's native-scrollbar suppression state and its
# document height, so samples can be filtered to the window where content
# height was still changing and the transcript overflowed.
_INSTALL_SAMPLER = """
() => {
  window.__gutterSamples = [];
  const tick = () => {
    const el = document.querySelector('.transcript-hide-native-scrollbar');
    if (el) {
      const cs = getComputedStyle(el);
      window.__gutterSamples.push({
        t: Math.round(performance.now()),
        scrollHeight: el.scrollHeight,
        overflowing: el.scrollHeight > el.clientHeight + 4,
        scrollbarWidth: cs.scrollbarWidth,
        scrollbarGutter: cs.scrollbarGutter,
        gutterDeficit: el.offsetWidth - el.clientWidth,
        thumb: !!document.querySelector('[data-testid="transcript-scrollbar-thumb"]'),
      });
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}
"""


def _assert_native_scrollbar_suppressed(state: dict, phase: str) -> None:
    """The scroller must neither show a native scrollbar nor reserve its strip.

    :param state: One sampled suppression state.
    :param phase: Label for failure messages, e.g. ``"streaming"``.
    """
    assert state["scrollbarWidth"] == "none", (
        f"{phase}: native scrollbar not disabled on the transcript scroller: "
        f"scrollbar-width={state['scrollbarWidth']!r}"
    )
    assert "stable" not in state["scrollbarGutter"], (
        f"{phase}: transcript scroller still demands a native scrollbar gutter "
        f"(computed scrollbar-gutter={state['scrollbarGutter']!r}); on renderers "
        "with classic scrollbars this reserves an edge strip and lets the native "
        "bar paint beside the custom thumb while content height changes"
    )
    assert state["gutterDeficit"] == 0, (
        f"{phase}: {state['gutterDeficit']}px of the scroller's width is reserved "
        "for native scrollbar gutters beside the custom thumb"
    )


def test_streaming_reply_paints_no_native_scrollbar_beside_custom_thumb(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Streaming growth never surfaces the native scrollbar next to the custom one."""
    base_url, session_id = seeded_session
    reply = " ".join(f"gutterword{n:04d}" for n in range(_REPLY_WORDS))
    reply = f"{reply} {_DONE_MARKER}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": reply, "stream": True}],
        key="native-gutter",
        match=_PROMPT,
    )

    page.set_viewport_size(_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    page.evaluate(_INSTALL_SAMPLER)
    composer.fill(_PROMPT)
    page.get_by_role("button", name="Send", exact=True).click()

    expect(page.get_by_text(_DONE_MARKER).first).to_be_visible(timeout=120_000)
    page.wait_for_timeout(500)

    samples = page.evaluate("() => window.__gutterSamples")
    assert samples, "sampler saw no transcript scroller"

    # The reported window: the transcript overflows while its content height
    # is still changing (the reply growing under the scroller).
    heights = {s["scrollHeight"] for s in samples}
    assert len(heights) >= 2, f"content height never changed while sampling: {heights}"
    streaming = [s for s in samples if s["overflowing"]]
    assert streaming, "transcript never overflowed; lengthen the reply"
    assert any(s["thumb"] for s in streaming), "custom scrollbar thumb never appeared"

    for state in streaming:
        _assert_native_scrollbar_suppressed(state, "streaming")

    expect(page.locator(_THUMB)).to_be_visible()
    settled = page.evaluate(
        """
        () => {
          const el = document.querySelector('.transcript-hide-native-scrollbar');
          const cs = getComputedStyle(el);
          return {
            overflowing: el.scrollHeight > el.clientHeight + 4,
            scrollbarWidth: cs.scrollbarWidth,
            scrollbarGutter: cs.scrollbarGutter,
            gutterDeficit: el.offsetWidth - el.clientWidth,
          };
        }
        """
    )
    assert settled["overflowing"], "transcript no longer overflows; nothing to guard"
    _assert_native_scrollbar_suppressed(settled, "settled")
