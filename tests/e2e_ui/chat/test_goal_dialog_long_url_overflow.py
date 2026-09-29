"""E2E: a goal objective holding a long unbroken URL stays inside the Goal dialog,
the saved-goal card and the workspace-bar goal tooltip."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Locator, Page, Response, expect

from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

_LONG_URL = (
    "https://docs.google.com/document/d/1o7zEPyRllUgZ4Fjoe2X6ia9IX9mbkBVIfkbUeb0L2gg/edit"
    "?tab=t.oqsylxrv06gf."
)
_OBJECTIVE = f"Let's drive\n{_LONG_URL}"

_DIALOG_PANEL = '[data-slot="dialog-content"]'
_DIALOG_FOOTER = '[data-slot="dialog-footer"]'
_TOOLTIP = '[data-slot="tooltip-content"]'

# Sub-pixel layout rounding; anything beyond this is real overflow.
_TOLERANCE_PX = 1.0
_GOAL_API_TIMEOUT_MS = 30_000

_BOX_JS = (
    "el => { const rect = el.getBoundingClientRect(); "
    "return {left: rect.left, right: rect.right, width: rect.width, "
    "scrollWidth: el.scrollWidth, clientWidth: el.clientWidth}; }"
)
# Measures the laid-out text itself, which can extend past the element's box.
_TEXT_BOX_JS = (
    "el => { const range = document.createRange(); range.selectNodeContents(el); "
    "const rect = range.getBoundingClientRect(); "
    "return {left: rect.left, right: rect.right, width: rect.width}; }"
)


def _codex_goal_response(session_id: str, method: str):
    def _matches(response: Response) -> bool:
        return (
            response.request.method == method
            and urlparse(response.url).path == f"/v1/sessions/{session_id}/codex_goal"
        )

    return _matches


def _box(locator: Locator) -> dict[str, float]:
    return locator.evaluate(_BOX_JS)


def _text_box(locator: Locator) -> dict[str, float]:
    return locator.evaluate(_TEXT_BOX_JS)


def _open_goal_dialog(page: Page, base_url: str, session_id: str) -> Locator:
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    page.get_by_test_id("composer-attach").click()
    goal_action = page.get_by_test_id("composer-goal-action")
    expect(goal_action).to_be_visible(timeout=_GOAL_API_TIMEOUT_MS)
    with page.expect_response(_codex_goal_response(session_id, "GET")):
        goal_action.click()
    expect(page.get_by_test_id("goal-empty")).to_be_visible(timeout=_GOAL_API_TIMEOUT_MS)
    dialog = page.get_by_role("dialog", name="Goal")
    expect(dialog).to_be_visible()
    return dialog


def _type_long_objective(page: Page) -> Locator:
    objective = page.get_by_test_id("goal-objective")
    objective.fill(_OBJECTIVE)
    expect(objective).to_have_value(_OBJECTIVE)
    return objective


def _save_goal(page: Page, session_id: str) -> Locator:
    page.get_by_test_id("goal-mode-paused").click()
    with page.expect_response(_codex_goal_response(session_id, "PUT")) as saved:
        page.get_by_test_id("goal-save").click()
    assert saved.value.status == 200, saved.value.text()
    card = page.get_by_test_id("goal-current")
    expect(card).to_contain_text("Let's drive", timeout=_GOAL_API_TIMEOUT_MS)
    return card


@pytest.mark.timeout(300)
def test_long_url_objective_keeps_goal_dialog_controls_inside_panel(
    page: Page,
    native_codex_mock_session: tuple[str, str],
) -> None:
    base_url, session_id = native_codex_mock_session
    _open_goal_dialog(page, base_url, session_id)
    objective = _type_long_objective(page)
    page.wait_for_timeout(1_500)

    panel = _box(page.locator(_DIALOG_PANEL))
    controls = {
        "objective": _box(objective),
        "mode": _box(page.get_by_test_id("goal-mode")),
        "token_budget": _box(page.get_by_test_id("goal-token-budget")),
        "footer": _box(page.locator(_DIALOG_FOOTER)),
    }

    spilled = {
        name: round(box["right"] - panel["right"])
        for name, box in controls.items()
        if box["right"] > panel["right"] + _TOLERANCE_PX
    }
    assert not spilled, (
        f"goal dialog controls extend past the dialog panel's right edge by {spilled} px"
    )
    assert controls["objective"]["scrollWidth"] <= controls["objective"]["clientWidth"] + 1, (
        "objective text overflows the Objective field horizontally"
    )


@pytest.mark.timeout(300)
def test_saved_long_url_objective_stays_inside_goal_summary(
    page: Page,
    native_codex_mock_session: tuple[str, str],
) -> None:
    base_url, session_id = native_codex_mock_session
    _open_goal_dialog(page, base_url, session_id)
    _type_long_objective(page)
    card = _save_goal(page, session_id)
    page.wait_for_timeout(1_500)

    panel = _box(page.locator(_DIALOG_PANEL))
    card_box = _box(card)
    objective = card.locator("p")
    text = _text_box(objective)

    assert card_box["right"] <= panel["right"] + _TOLERANCE_PX, (
        f"current-goal card extends {card_box['right'] - panel['right']:.0f}px past the dialog"
    )
    assert text["right"] <= card_box["right"] + _TOLERANCE_PX, (
        f"saved objective text extends {text['right'] - card_box['right']:.0f}px past the card"
    )


@pytest.mark.timeout(300)
def test_goal_indicator_tooltip_keeps_long_url_objective_inside_bubble(
    page: Page,
    native_codex_mock_session: tuple[str, str],
) -> None:
    base_url, session_id = native_codex_mock_session
    dialog = _open_goal_dialog(page, base_url, session_id)
    _type_long_objective(page)
    _save_goal(page, session_id)
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)

    indicator = page.get_by_test_id("composer-goal-mode")
    expect(indicator).to_be_visible(timeout=_GOAL_API_TIMEOUT_MS)
    indicator.hover()
    tooltip = page.locator(_TOOLTIP)
    expect(tooltip).to_be_visible(timeout=5_000)
    page.wait_for_timeout(1_500)

    bubble = _box(tooltip)
    objective = tooltip.locator("div", has_text="docs.google.com")
    objective_box = _box(objective)
    text = _text_box(objective)

    box_spill = objective_box["right"] - bubble["right"]
    assert box_spill <= _TOLERANCE_PX, (
        f"tooltip objective extends {box_spill:.0f}px past the bubble"
    )
    text_spill = text["right"] - bubble["right"]
    assert text_spill <= _TOLERANCE_PX, (
        f"tooltip objective text extends {text_spill:.0f}px past the bubble"
    )
