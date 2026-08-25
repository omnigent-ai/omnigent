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


def _scroll_with_native_touch(page: Page) -> dict:
    """Scroll through Chromium's native touch-panning path."""
    session = page.context.new_cdp_session(page)
    try:
        session.send(
            "Emulation.setTouchEmulationEnabled",
            {"enabled": True, "maxTouchPoints": 1},
        )
        target = page.evaluate(
            """() => {
                const form = document.querySelector(
                    'textarea[aria-label="Message the agent"]'
                ).closest('form');
                const scroller = form.parentElement.querySelector('[role="log"] > div');
                const rect = scroller.getBoundingClientRect();
                const events = [];
                const record = (event) => events.push(event.type);
                for (const type of ['pointerdown', 'pointermove', 'pointercancel']) {
                    window.addEventListener(type, record, { capture: true });
                }
                scroller.addEventListener('scroll', record);
                scroller.addEventListener('scrollend', record);
                window.__nativeTouchProbe = { events, scroller };
                return {
                    x: Math.round(rect.left + rect.width / 2),
                    y: Math.round(rect.top + Math.min(rect.height * 0.35, 220)),
                    initialScrollTop: Math.round(scroller.scrollTop),
                };
            }"""
        )
        point = {
            "x": target["x"],
            "y": target["y"],
            "id": 1,
            "radiusX": 2,
            "radiusY": 2,
            "force": 1,
        }
        session.send(
            "Input.dispatchTouchEvent",
            {"type": "touchStart", "touchPoints": [point]},
        )
        for offset in (35, 75, 120, 170, 225, 280):
            session.send(
                "Input.dispatchTouchEvent",
                {
                    "type": "touchMove",
                    "touchPoints": [{**point, "y": point["y"] + offset}],
                },
            )
            page.wait_for_timeout(16)
        session.send(
            "Input.dispatchTouchEvent",
            {"type": "touchEnd", "touchPoints": []},
        )
        page.wait_for_timeout(600)
        return page.evaluate(
            """initialScrollTop => {
                const { events, scroller } = window.__nativeTouchProbe;
                return {
                    events,
                    initialScrollTop,
                    settledScrollTop: Math.round(scroller.scrollTop),
                    settledDistance: Math.round(
                        scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop
                    ),
                };
            }""",
            target["initialScrollTop"],
        )
    finally:
        session.send("Emulation.setTouchEmulationEnabled", {"enabled": False})
        session.detach()


def _click_scroll_to_bottom(page: Page) -> None:
    """Use the conversation's visible library-driven re-lock control."""
    button = page.locator('[role="log"] button:has(svg.lucide-arrow-down)')
    expect(button).to_be_visible()
    button.click()


def _append_streamed_output(page: Page, height: int) -> dict:
    """Grow transcript content without scrolling its container."""
    return page.evaluate(
        """height => {
            const form = document.querySelector(
                'textarea[aria-label="Message the agent"]'
            ).closest('form');
            const scroller = form.parentElement.querySelector('[role="log"] > div');
            const content = scroller.firstElementChild;
            const spacer = content.lastElementChild;
            const geometry = () => ({
                scrollHeight: scroller.scrollHeight,
                scrollTop: scroller.scrollTop,
            });
            const before = geometry();
            const streamed = document.createElement('div');
            streamed.dataset.testid = 'streamed-output-probe';
            streamed.style.flex = '0 0 auto';
            streamed.style.height = `${height}px`;
            streamed.textContent = 'Additional streamed output below the reader.';
            content.insertBefore(streamed, spacer);
            return {
                before,
                after: geometry(),
            };
        }""",
        height,
    )


def _append_output_during_composer_reflow(
    page: Page,
    initial_height: int,
    followup_height: int,
) -> dict:
    """Grow output and composer together, then append again while layout settles."""
    return page.evaluate(
        """async ({ initialHeight, followupHeight }) => {
            const textarea = document.querySelector(
                'textarea[aria-label="Message the agent"]'
            );
            const form = textarea.closest('form');
            const scroller = form.parentElement.querySelector('[role="log"] > div');
            const content = scroller.firstElementChild;
            const spacer = content.lastElementChild;
            const appendProbe = (height, testid) => {
                const streamed = document.createElement('div');
                streamed.dataset.testid = testid;
                streamed.style.flex = '0 0 auto';
                streamed.style.height = `${height}px`;
                streamed.textContent = 'Additional streamed output below the reader.';
                content.insertBefore(streamed, spacer);
            };
            const geometry = () => ({
                clientHeight: scroller.clientHeight,
                scrollHeight: scroller.scrollHeight,
                scrollTop: scroller.scrollTop,
            });
            const before = geometry();

            appendProbe(initialHeight, 'same-frame-output-probe');
            const valueSetter = Object.getOwnPropertyDescriptor(
                HTMLTextAreaElement.prototype,
                'value'
            ).set;
            valueSetter.call(textarea, `${textarea.value}\n\n\n`);
            textarea.dispatchEvent(new InputEvent('input', { bubbles: true }));

            // Let the combined content/container resize begin settling, then
            // append another chunk before the layout has gone idle.
            await new Promise((resolve) => requestAnimationFrame(resolve));
            await new Promise((resolve) => requestAnimationFrame(resolve));
            appendProbe(followupHeight, 'during-reflow-output-probe');

            return { before, afterDuringReflow: geometry() };
        }""",
        {
            "initialHeight": initial_height,
            "followupHeight": followup_height,
        },
    )


def test_composer_hides_native_scrollbar_without_disabling_scroll(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A capped draft and transcript scroll without painting native browser chrome."""
    base_url, session_id = seeded_session
    for i in range(6):
        seed_committed_turn(
            session_id,
            prompt=f"Question {i}?",
            reply=f"Paragraph {i}. " + ("filler sentence for height. " * 12),
            response_id=f"scrollbar_probe_{i}",
        )
    page.goto(f"{base_url}/c/{session_id}")

    expect(page.locator(_TEXT_SECTION)).to_have_count(6, timeout=30_000)
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    transition = page.evaluate(
        r"""async () => {
            const composer = document.querySelector(
                'textarea[aria-label="Message the agent"]'
            );
            const valueSetter = Object.getOwnPropertyDescriptor(
                HTMLTextAreaElement.prototype,
                'value'
            ).set;
            const samples = [];
            for (let i = 0; i < 20; i += 1) {
                valueSetter.call(
                    composer,
                    Array.from({ length: i + 1 }, (_, line) => `Draft line ${line}`).join('\n')
                );
                composer.dispatchEvent(new InputEvent('input', { bubbles: true }));
                await new Promise((resolve) => requestAnimationFrame(resolve));
                samples.push({
                    inputViewportOverflowY:
                        getComputedStyle(composer.parentElement).overflowY,
                    rootOverflowY: getComputedStyle(document.documentElement).overflowY,
                    bodyOverflowY: getComputedStyle(document.body).overflowY,
                    rootScrollTop: document.scrollingElement.scrollTop,
                });
            }
            return samples;
        }"""
    )
    assert all(
        sample
        == {
            "inputViewportOverflowY": "hidden",
            "rootOverflowY": "hidden",
            "bodyOverflowY": "hidden",
            "rootScrollTop": 0,
        }
        for sample in transition
    ), transition

    geometry = page.evaluate(
        """() => {
            const composer = document.querySelector(
                'textarea[aria-label="Message the agent"]'
            );
            const transcript = document.querySelector('[role="log"] > div');
            const probe = (element) => ({
                clientHeight: element.clientHeight,
                scrollHeight: element.scrollHeight,
                scrollbarWidth: getComputedStyle(element).scrollbarWidth,
                webkitScrollbarDisplay:
                    getComputedStyle(element, '::-webkit-scrollbar').display,
            });
            return {
                composer: probe(composer),
                transcript: probe(transcript),
                inputViewportOverflowY:
                    getComputedStyle(composer.parentElement).overflowY,
                rootOverflowY: getComputedStyle(document.documentElement).overflowY,
                bodyOverflowY: getComputedStyle(document.body).overflowY,
            };
        }"""
    )
    for surface in ("composer", "transcript"):
        assert geometry[surface]["scrollHeight"] > geometry[surface]["clientHeight"], geometry
        assert geometry[surface]["scrollbarWidth"] == "none", geometry
        assert geometry[surface]["webkitScrollbarDisplay"] == "none", geometry
    assert geometry["inputViewportOverflowY"] == "hidden", geometry
    assert geometry["rootOverflowY"] == "hidden", geometry
    assert geometry["bodyOverflowY"] == "hidden", geometry

    composer.evaluate("textarea => { textarea.scrollTop = textarea.scrollHeight; }")
    assert composer.evaluate("textarea => textarea.scrollTop") > 0


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

    def assert_reader_anchor(before: dict, after: dict, label: str) -> None:
        """Composer reflow must not move the transcript under an escaped reader."""
        assert abs(after["viewport"][2] - before["viewport"][2]) <= 1, (
            label,
            before,
            after,
        )
        assert len(after["messageTops"]) == len(before["messageTops"]), (
            label,
            before,
            after,
        )
        assert all(
            abs(after_top - before_top) <= 1
            for before_top, after_top in zip(
                before["messageTops"], after["messageTops"], strict=True
            )
        ), (label, before, after)

    assert_clearance(baseline, "baseline")

    # The public near-bottom alias includes readers within 70px, but a reader
    # who deliberately stopped 50px above bottom is not library-locked. The
    # bottom bridge must leave native browser anchoring in control.
    page.locator('[role="log"] > div').first.hover()
    page.mouse.wheel(0, -50)
    near_bottom_escaped = _settled_geometry(page)
    assert abs(near_bottom_escaped["distanceFromBottom"] - 50) <= 1, near_bottom_escaped

    for _ in range(3):
        composer.press("Shift+Enter")
    near_bottom_grown = _settled_geometry(page)
    assert_reader_anchor(
        near_bottom_escaped,
        near_bottom_grown,
        "near-bottom escaped composer growth",
    )
    assert abs(near_bottom_grown["overlap"]) <= 1, near_bottom_grown
    expect(composer).to_be_focused()

    for _ in range(3):
        composer.press("Backspace")
    _settled_geometry(page)
    _scroll_to_distance_from_bottom(page, 0)
    assert_clearance(_settled_geometry(page), "after near-bottom reset")

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

    # Chromium touch panning cancels pointer delivery before its native scroll
    # events. Browser anchoring, not application pointer state, must preserve the
    # settled transcript position when the composer later grows.
    browser = page.context.browser
    assert browser is not None
    touch_context = browser.new_context(
        viewport={"width": 500, "height": 713},
        has_touch=True,
    )
    try:
        touch_page = touch_context.new_page()
        touch_page.goto(f"{base_url}/c/{session_id}")
        expect(touch_page.locator(_TEXT_SECTION)).to_have_count(6, timeout=30_000)
        touch_composer = touch_page.get_by_label("Message the agent")
        expect(touch_composer).to_be_visible(timeout=30_000)
        touch_composer.click()
        touch_baseline = _settled_geometry(touch_page)
        assert_clearance(touch_baseline, "native touch baseline")

        touch = _scroll_with_native_touch(touch_page)
        assert touch["settledDistance"] > 100, touch
        assert touch["settledScrollTop"] < touch["initialScrollTop"], touch
        assert "pointercancel" in touch["events"], touch
        assert "scroll" in touch["events"], touch
        assert touch["events"].index("pointercancel") < touch["events"].index("scroll"), touch
        assert "scrollend" in touch["events"], touch
        touch_settled = _settled_geometry(touch_page)
        assert abs(touch_settled["distanceFromBottom"] - touch["settledDistance"]) <= 1, (
            touch,
            touch_settled,
        )
        expect(touch_composer).to_be_focused()

        touch_composer.press("Shift+Enter")
        touch_grown = _settled_geometry(touch_page)
        assert_reader_anchor(touch_settled, touch_grown, "native touch composer growth")
        assert abs(touch_grown["overlap"]) <= 1, touch_grown
        expect(touch_composer).to_be_focused()
    finally:
        touch_context.close()

    # Escape bottom lock, then let output stream below the reader without
    # moving the scroll container. Native anchoring must keep the visible
    # transcript stable when later composer growth shrinks the viewport.
    _scroll_to_distance_from_bottom(page, 180)
    escaped = _settled_geometry(page)
    assert abs(escaped["distanceFromBottom"] - 180) <= 1, escaped

    appended = _append_streamed_output(page, 240)
    streamed = _settled_geometry(page)
    added_height = streamed["viewport"][1] - appended["before"]["scrollHeight"]
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
    assert_reader_anchor(streamed, streamed_grown, "streamed composer growth")

    # A genuine reader scroll after more output must remain visually stable
    # through the next composer reflow.
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
    assert_reader_anchor(after_user_scroll, after_user_scroll_grown, "reader composer growth")

    # Output and composer growth may land in one layout delivery while a
    # follow-up stream chunk arrives before the reflow settles. Neither change
    # may move the visible transcript under an escaped reader.
    for _ in range(3):
        composer.press("Backspace")
    _settled_geometry(page)
    _scroll_to_distance_from_bottom(page, 180)
    same_frame_escaped = _settled_geometry(page)
    assert abs(same_frame_escaped["distanceFromBottom"] - 180) <= 1, same_frame_escaped

    combined = _append_output_during_composer_reflow(page, 240, 90)
    same_frame_grown = _settled_geometry(page)
    total_added_height = same_frame_grown["viewport"][1] - combined["before"]["scrollHeight"]
    assert total_added_height > 0, (combined, same_frame_grown)
    assert same_frame_grown["viewport"][0] < combined["before"]["clientHeight"], (
        combined,
        same_frame_grown,
    )
    assert page.locator('[data-testid="same-frame-output-probe"]').count() == 1
    assert page.locator('[data-testid="during-reflow-output-probe"]').count() == 1
    assert_reader_anchor(same_frame_escaped, same_frame_grown, "same-frame reflow")
    assert abs(same_frame_grown["overlap"]) <= 1, same_frame_grown
    expect(composer).to_be_focused()

    # The library's scroll control owns re-locking. The bottom bridge must keep
    # that lock through the next composer resize and subsequent streamed output.
    composer.fill("")
    _settled_geometry(page)
    _scroll_to_distance_from_bottom(page, 400)
    button_escaped = _settled_geometry(page)
    assert abs(button_escaped["distanceFromBottom"] - 400) <= 1, button_escaped
    _click_scroll_to_bottom(page)
    button_relocked = _settled_geometry(page)
    assert button_relocked["distanceFromBottom"] <= 1, button_relocked

    composer.press("Shift+Enter")
    button_relocked_grown = _settled_geometry(page)
    assert button_relocked_grown["composerHeight"] > button_relocked["composerHeight"], (
        button_relocked,
        button_relocked_grown,
    )
    assert button_relocked_grown["distanceFromBottom"] <= 1, button_relocked_grown
    assert abs(button_relocked_grown["overlap"]) <= 1, button_relocked_grown
    _append_streamed_output(page, 80)
    button_following_output = _settled_geometry(page)
    assert button_following_output["distanceFromBottom"] <= 1, button_following_output

    # Sending a multiline message re-locks through ScrollToBottomOnSend while
    # clearing the draft shrinks the composer. Later growth must keep following.
    composer.fill("")
    _settled_geometry(page)
    _scroll_to_distance_from_bottom(page, 320)
    send_escaped = _settled_geometry(page)
    assert abs(send_escaped["distanceFromBottom"] - 320) <= 1, send_escaped
    composer.fill("round-three line one\nline two\nline three")
    send_multiline = _settled_geometry(page)
    assert send_multiline["distanceFromBottom"] >= 319, send_multiline
    page.get_by_role("button", name="Send", exact=True).click()
    send_relocked = _settled_geometry(page)
    assert send_relocked["distanceFromBottom"] <= 1, send_relocked

    composer.fill("next draft\nline two")
    send_relocked_grown = _settled_geometry(page)
    assert send_relocked_grown["distanceFromBottom"] <= 1, send_relocked_grown
    assert abs(send_relocked_grown["overlap"]) <= 1, send_relocked_grown
    expect(composer).to_be_focused()
