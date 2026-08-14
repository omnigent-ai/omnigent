"""E2E: growing the composer must reflow the transcript above it.

The composer auto-grows by collapsing its textarea to ``height: auto`` and
reading the content height back (``hooks/useAutoGrowTextarea.ts``). That
collapse lasts one layout pass, during which the composer is one row tall and
the transcript's scroll viewport is correspondingly taller. Without the
hook's wrapper pin, the browser clamps ``scrollTop`` against that temporary
viewport and shunts the transcript on every re-measure.

The composer's persistent height is different: it belongs in the flex layout.
As the input grows, the transcript viewport must shrink by the same amount and
remain bottom-locked, with its bottom edge meeting the composer's top edge.
Floating the extra rows over an unchanged viewport covers visible output.

Layout regressions like these are invisible below the browser because jsdom
has no real scroll geometry. The assertions pin both behaviors: real growth
reflows the transcript without overlap, while typing at a stable height does
not move the transcript or knock it loose from bottom lock.
"""

from __future__ import annotations

import time

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_turn

_TEXT_SECTION = '[data-testid="assistant-text-section"]'

# Geometry of the transcript and the composer, read together so a measurement
# cannot straddle a layout change. ``distanceFromBottom`` is 0-or-1 while the
# transcript is stuck to the bottom (use-stick-to-bottom parks it one pixel
# short of the maximum). ``railTicks`` is the left-edge turn minimap, centered
# on the transcript viewport.
_PROBE = """() => {
    const ta = document.querySelector('textarea[aria-label="Message the agent"]');
    const form = ta.closest('form');
    const card = form.querySelector('[data-composer-card]');
    const scroller = form.parentElement.querySelector('[role="log"] > div');
    const rail = document.querySelector('.turn-rail-fade');
    const sections = [...document.querySelectorAll(
        '[data-testid="assistant-text-section"]')];
    const composerTop = Math.round(card.getBoundingClientRect().top);
    const transcriptBottom = Math.round(scroller.getBoundingClientRect().bottom);
    return {
        messageTops: sections.map(
            (section) => Math.round(section.getBoundingClientRect().top)),
        lastMessageBottom: Math.round(
            sections.at(-1).getBoundingClientRect().bottom),
        composerHeight: Math.round(ta.getBoundingClientRect().height),
        composerTop,
        transcriptBottom,
        overlap: transcriptBottom - composerTop,
        formMarginTop: Math.round(
            parseFloat(getComputedStyle(form).marginTop) || 0),
        distanceFromBottom: Math.round(
            scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop),
        viewport: [
            scroller.clientHeight,
            scroller.scrollHeight,
            Math.round(scroller.scrollTop),
        ],
        railTicks: rail
            ? [...rail.querySelectorAll('button')]
                  .map((tick) => Math.round(tick.getBoundingClientRect().top))
            : null,
    };
}"""


def _settled_geometry(page: Page, timeout_s: float = 15.0) -> dict:
    """Read :data:`_PROBE` once two consecutive reads agree.

    The layout settles through several passes — the composer's measurement,
    the trailing spacer's ResizeObserver, then stick-to-bottom — and how long
    that takes varies with CI load. Polling for a stable read beats sleeping a
    fixed guess.

    :param page: Playwright page on the chat surface.
    :param timeout_s: How long to keep polling before giving up.
    :returns: The probe reading, once stable.
    :raises AssertionError: If the layout never stops changing.
    """
    previous = page.evaluate(_PROBE)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        page.wait_for_timeout(100)
        current = page.evaluate(_PROBE)
        if current == previous:
            return current
        previous = current
    raise AssertionError(f"layout never settled; last reading: {previous}")


def _scroll_to_distance_from_bottom(page: Page, distance: int) -> dict:
    """Scroll the transcript to a precise distance from its bottom."""
    return page.evaluate(
        """distance => {
            const form = document.querySelector(
                'textarea[aria-label="Message the agent"]'
            ).closest('form');
            const scroller = form.parentElement.querySelector('[role="log"] > div');
            scroller.dispatchEvent(new WheelEvent('wheel', { deltaY: -100 }));
            scroller.scrollTop = scroller.scrollHeight - scroller.clientHeight - distance;
            scroller.dispatchEvent(new Event('scroll'));
            return {
                scrollTop: Math.round(scroller.scrollTop),
                distanceFromBottom: Math.round(
                    scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop
                ),
            };
        }""",
        distance,
    )


def _append_streamed_output(page: Page, height: int) -> dict:
    """Grow transcript content without scrolling its container."""
    return page.evaluate(
        """height => {
            const form = document.querySelector(
                'textarea[aria-label="Message the agent"]'
            ).closest('form');
            const scroller = form.parentElement.querySelector('[role="log"] > div');
            const content = scroller.firstElementChild;
            const before = {
                scrollHeight: scroller.scrollHeight,
                scrollTop: scroller.scrollTop,
            };
            const streamed = document.createElement('div');
            streamed.dataset.testid = 'streamed-output-probe';
            streamed.style.flex = '0 0 auto';
            streamed.style.height = `${height}px`;
            streamed.textContent = 'Additional streamed output below the reader.';
            content.append(streamed);
            return {
                before,
                after: {
                    scrollHeight: scroller.scrollHeight,
                    scrollTop: scroller.scrollTop,
                },
            };
        }""",
        height,
    )


def test_composer_growth_reflows_transcript_without_covering_output(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Growth reflows the viewport; same-height edits remain stable."""
    base_url, session_id = seeded_session
    # Enough turns to overflow the viewport: the transcript has to be
    # scrollable for a scrollTop clamp to have anywhere to land, and each turn
    # is one tick on the rail.
    for i in range(6):
        seed_committed_turn(
            session_id,
            prompt=f"Question {i}?",
            reply=f"Paragraph {i}. " + ("filler sentence for height. " * 12),
            response_id=f"resp_{i}",
        )

    page.goto(f"{base_url}/c/{session_id}")

    sections = page.locator(_TEXT_SECTION)
    expect(sections).to_have_count(6, timeout=30_000)
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.click()
    baseline = _settled_geometry(page)
    assert baseline["railTicks"], baseline

    def assert_clearance(state: dict, label: str) -> None:
        """The transcript ends at the composer and remains bottom-locked."""
        assert abs(state["overlap"]) <= 1, (label, state)
        assert state["lastMessageBottom"] <= state["composerTop"] + 1, (
            label,
            state,
        )
        assert state["formMarginTop"] == 0, (label, state)
        assert state["distanceFromBottom"] <= 1, (label, state)

    assert_clearance(baseline, "baseline")

    # Grow: three Shift+Enter newlines, one line taller each.
    for _ in range(3):
        composer.press("Shift+Enter")
    grown = _settled_geometry(page)
    assert grown["composerHeight"] > baseline["composerHeight"], grown
    growth = grown["composerHeight"] - baseline["composerHeight"]
    viewport_shrink = baseline["viewport"][0] - grown["viewport"][0]
    assert abs(viewport_shrink - growth) <= 1, (baseline, grown)
    assert_clearance(grown, "after newlines")

    # Typing at the same height still re-measures the textarea. The wrapper pin
    # must prevent that temporary collapse from moving the transcript.
    composer.type("hello", delay=30)
    typed = _settled_geometry(page)
    for key in (
        "messageTops",
        "composerHeight",
        "composerTop",
        "transcriptBottom",
        "viewport",
        "railTicks",
    ):
        assert typed[key] == grown[key], (
            "after typing",
            key,
            grown[key],
            typed[key],
        )
    assert_clearance(typed, "after typing")

    # Shrink back: deleting the newlines restores the resting geometry.
    for _ in range(len("hello") + 3):
        composer.press("Backspace")
    shrunk = _settled_geometry(page)
    assert shrunk["composerHeight"] == baseline["composerHeight"], (
        baseline,
        shrunk,
    )
    for key in (
        "messageTops",
        "composerTop",
        "transcriptBottom",
        "viewport",
        "railTicks",
    ):
        assert shrunk[key] == baseline[key], (
            "after deleting",
            key,
            baseline[key],
            shrunk[key],
        )
    assert_clearance(shrunk, "after deleting")

    # Escape bottom lock, then let output stream below the reader without
    # moving the scroll container. Composer growth must preserve the real
    # post-stream distance, not the distance cached before content grew.
    _scroll_to_distance_from_bottom(page, 180)
    escaped = _settled_geometry(page)
    assert abs(escaped["distanceFromBottom"] - 180) <= 1, escaped

    appended = _append_streamed_output(page, 240)
    streamed = _settled_geometry(page)
    added_height = appended["after"]["scrollHeight"] - appended["before"]["scrollHeight"]
    assert added_height > 0, appended
    assert appended["after"]["scrollTop"] == appended["before"]["scrollTop"], appended
    expected_streamed_distance = escaped["distanceFromBottom"] + added_height
    assert abs(streamed["distanceFromBottom"] - expected_streamed_distance) <= 1, (
        escaped,
        appended,
        streamed,
    )

    for _ in range(3):
        composer.press("Shift+Enter")
    streamed_grown = _settled_geometry(page)
    assert abs(streamed_grown["overlap"]) <= 1, streamed_grown
    assert abs(streamed_grown["distanceFromBottom"] - streamed["distanceFromBottom"]) <= 1, (
        {
            "before": {
                "distance": streamed["distanceFromBottom"],
                "viewport": streamed["viewport"],
            },
            "after": {
                "distance": streamed_grown["distanceFromBottom"],
                "viewport": streamed_grown["viewport"],
            },
        },
    )

    # The first genuine user scroll after another content-height change must
    # replace the reconciled distance instead of being mistaken for a synthetic
    # content-resize scroll.
    for _ in range(3):
        composer.press("Backspace")
    _settled_geometry(page)
    _append_streamed_output(page, 120)
    after_more_output = _settled_geometry(page)
    user_distance = after_more_output["distanceFromBottom"] + 70
    _scroll_to_distance_from_bottom(page, user_distance)
    after_user_scroll = _settled_geometry(page)
    assert abs(after_user_scroll["distanceFromBottom"] - user_distance) <= 1, after_user_scroll

    for _ in range(3):
        composer.press("Shift+Enter")
    after_user_scroll_grown = _settled_geometry(page)
    assert abs(after_user_scroll_grown["overlap"]) <= 1, after_user_scroll_grown
    assert (
        abs(
            after_user_scroll_grown["distanceFromBottom"] - after_user_scroll["distanceFromBottom"]
        )
        <= 1
    ), (after_user_scroll, after_user_scroll_grown)
