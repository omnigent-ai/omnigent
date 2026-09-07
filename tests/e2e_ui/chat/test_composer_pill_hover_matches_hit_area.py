"""E2E: the composer split pill's hover highlight matches its hit area.

The composer's model/effort pill is one visual control: a read-only
``<Model> <Effort>`` label beside the config gear. The whole pill lights up
(``bg-muted``) when the pointer is anywhere over it, so the entire pill reads
as clickable — but only the gear segment actually opens the session-config
modal. Clicking the highlighted label half does nothing.

This test encodes the consistency contract: any region of the pill that
triggers the hover highlight must also act on click. It fails while the label
half highlights-but-ignores clicks, and passes once the whole pill is
clickable (or once the highlight is scoped to the clickable segment).
"""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.chat.test_claude_model_picker import _patch_session_as_claude_native


def _pill(page: Page) -> Locator:
    """The composer split pill: innermost div holding both label and gear.

    The pill wrapper carries no test id of its own, so locate it as the last
    (innermost, in document order) ``div`` that contains both the model/effort
    label and the config gear.

    :param page: Playwright page on a session route.
    :returns: Locator for the pill wrapper element.
    """
    return (
        page.locator("div")
        .filter(has=page.get_by_test_id("composer-model-effort-label"))
        .filter(has=page.get_by_test_id("composer-config-gear"))
        .last
    )


def _background(pill: Locator) -> str:
    """Computed background color of the pill, e.g. ``"rgba(0, 0, 0, 0)"``.

    :param pill: The pill wrapper locator.
    :returns: The ``background-color`` computed style value.
    """
    return pill.evaluate("el => getComputedStyle(el).backgroundColor")


def test_composer_pill_highlighted_label_is_clickable(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Hovering the label highlights the pill, so clicking it must act.

    Journey: open a session whose composer shows the model label + config
    gear, hover the read-only label half (the whole pill highlights), click
    that highlighted half, and expect the config modal to open — the same
    action the highlight advertises and the gear half performs.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real
        server-backed session; the browser snapshot is patched to
        claude-native so the pill shows a model label beside the gear.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_claude_native(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=15_000)
    label = page.get_by_test_id("composer-model-effort-label")
    expect(label).to_be_visible()

    pill = _pill(page)

    # Rest state: park the pointer away from the composer and let the
    # color transition settle before sampling the baseline background.
    page.mouse.move(5, 5)
    page.wait_for_timeout(400)
    rest_background = _background(pill)

    # Hover the read-only label half and watch for the pill-wide highlight.
    label.hover()
    highlighted = False
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if _background(pill) != rest_background:
            highlighted = True
            break
        page.wait_for_timeout(100)

    if not highlighted:
        # No highlight over the label: the hover affordance already matches
        # the hit area, which is the other acceptable resolution.
        return

    # The pill advertises interactivity over the label, so clicking there
    # must open the config modal.
    label.click()
    modal = page.get_by_test_id("composer-config-modal")
    label_click_opened = True
    try:
        expect(modal).to_be_visible(timeout=3_000)
    except AssertionError:
        label_click_opened = False

    if label_click_opened:
        return

    # Sanity check before failing: the gear half does open the modal, so the
    # miss above is the label's dead hit-area, not broken modal machinery.
    gear.click()
    expect(modal).to_be_visible()
    page.keyboard.press("Escape")
    expect(modal).not_to_be_visible()

    pytest.fail(
        "composer pill highlights on hover over the read-only model/effort "
        "label, but clicking that highlighted region does nothing — only the "
        "gear segment opens the config modal (hover affordance is larger "
        "than the hit area)"
    )
