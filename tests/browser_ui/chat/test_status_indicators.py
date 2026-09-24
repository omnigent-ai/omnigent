"""Browser-lane coverage for live status events reaching chat indicators."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, session_status_event

_WORKING_LABEL_RE = re.compile(
    r"^(Working|Cooking|Crunching|Tinkering|Pondering|Brewing|Noodling|Wrangling|"
    r"Conjuring|Assembling|Percolating|Untangling|Scheming|Finagling|Whirring|Puzzling)…$"
)
_MONITOR_TASK = {
    "id": "monitor-ci",
    "type": "shell",
    "status": "running",
    "description": (
        "Watch PR checks and review comments while CI finishes, including new failures "
        "and the final review approval before the pull request is ready to merge"
    ),
    "command": (
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "endpoint=repos/example/monitoring-compatibility-checks-for-background-task-indicators/"
        "commits/0123456789abcdef0123456789abcdef01234567/check-runs\n"
        "while true; do\n"
        '  checks=$(gh api "$endpoint")\n'
        "  echo \"$checks\" | jq -r '.check_runs[].conclusion'\n"
        '  pending=$(echo "$checks" | jq \'[.check_runs[] | select(.status != "completed")] '
        "| length')\n"
        '  if [ "$pending" -eq 0 ]; then\n'
        "    break\n"
        "  fi\n"
        "  sleep 15\n"
        "done"
    ),
}


def _background_status(
    chat: ChatSessionContract,
    count: int,
    tasks: list[dict[str, str]] | None = None,
) -> None:
    data: dict[str, object] = {
        "conversation_id": chat.session_id,
        "status": "idle",
        "background_task_count": count,
    }
    if tasks is not None:
        data["background_tasks"] = tasks
    chat.emit({"event": "session.status", "data": data})


def _pill(page: Page, count: int) -> Locator:
    plural = "" if count == 1 else "s"
    return page.get_by_role(
        "button", name=f"{count} background task{plural} still running", exact=True
    )


def test_bare_idle_clears_the_live_working_indicator(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """The real SPA consumes a running edge followed by an id-less idle."""
    chat = chat_session_contract
    page.goto(chat.url)
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=20_000)
    chat.wait_for_stream()
    working = page.get_by_test_id("working-indicator")

    chat.emit_busy("browser-turn")
    expect(working).to_be_visible(timeout=10_000)

    chat.emit_idle(None)
    expect(working).to_be_hidden(timeout=10_000)


def test_blocked_reason_reaches_the_live_working_indicator(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """A parked native turn names its reason and clears it on the next edge."""
    chat = chat_session_contract
    page.goto(chat.url)
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=20_000)
    chat.wait_for_stream()
    working = page.get_by_test_id("working-indicator")

    chat.emit(
        {
            "event": "session.status",
            "data": {
                "conversation_id": chat.session_id,
                "status": "running",
                "blocked_on": "permission prompt",
            },
        }
    )
    expect(working).to_contain_text("Blocked on: permission prompt", timeout=10_000)

    chat.emit(session_status_event(chat.session_id, "running"))
    expect(working).not_to_contain_text("Blocked on:", timeout=10_000)
    expect(working.get_by_text(_WORKING_LABEL_RE)).to_be_visible(timeout=10_000)

    chat.emit_idle(None)
    expect(working).to_be_hidden(timeout=10_000)


@pytest.mark.parametrize(
    "width",
    [1280, pytest.param(375, marks=pytest.mark.browser_context_args(has_touch=True))],
)
def test_background_task_popover_stays_interactive_and_within_viewport(
    page: Page,
    chat_session_contract: ChatSessionContract,
    width: int,
) -> None:
    """Popover layout, animation focus, and native touch survive real browser geometry."""
    chat = chat_session_contract
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(chat.url)
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=20_000)
    chat.wait_for_stream()
    _background_status(chat, 1, [_MONITOR_TASK])

    def activate(control: Locator) -> None:
        if width < 768:
            control.tap()
        else:
            control.click()

    pill = _pill(page, 1)
    expect(pill).to_have_text("1", timeout=10_000)
    expect(pill).to_have_attribute("aria-expanded", "false")
    activate(pill)

    panel = page.get_by_role("dialog", name="1 background task", exact=True)
    expect(panel).to_be_visible()
    description = panel.get_by_text(_MONITOR_TASK["description"], exact=True)
    command = panel.get_by_text(_MONITOR_TASK["command"], exact=True)
    expand_command = panel.get_by_role("button", name="Expand command", exact=True)
    collapse_command = panel.get_by_role("button", name="Collapse command", exact=True)
    expect(description).to_be_visible()
    expect(command).to_be_visible()
    expect(expand_command).to_have_attribute("aria-expanded", "false")

    def compact_command_height() -> float:
        dimensions = command.evaluate(
            """element => ({
              height: element.clientHeight,
              lineHeight: parseFloat(getComputedStyle(element).lineHeight),
            })"""
        )
        assert 0 < dimensions["height"] <= 2 * dimensions["lineHeight"] + 1
        return dimensions["height"]

    compact_height = compact_command_height()
    compact_panel_height = panel.evaluate("element => element.clientHeight")
    bounds = panel.bounding_box()
    assert bounds is not None
    assert bounds["x"] >= 0
    assert bounds["x"] + bounds["width"] <= width
    for element in (panel, description, command):
        assert element.evaluate("node => node.scrollWidth <= node.clientWidth + 1")

    toggle_bounds = expand_command.bounding_box()
    command_bounds = command.bounding_box()
    description_bounds = description.bounding_box()
    assert toggle_bounds is not None and command_bounds is not None
    assert description_bounds is not None
    assert toggle_bounds["x"] >= command_bounds["x"] + command_bounds["width"] - 0.5
    assert toggle_bounds["y"] == pytest.approx(description_bounds["y"], abs=2)

    activate(expand_command)
    expect(collapse_command).to_have_attribute("aria-expanded", "true")
    assert command.evaluate("element => element.clientHeight") > compact_height
    assert panel.evaluate("element => element.clientHeight") > compact_panel_height
    for element in (panel, description, command):
        assert element.evaluate("node => node.scrollWidth <= node.clientWidth + 1")

    activate(collapse_command)
    expect(expand_command).to_have_attribute("aria-expanded", "false")
    assert compact_command_height() == pytest.approx(compact_height, abs=1)
    assert panel.evaluate("element => element.clientHeight") == pytest.approx(
        compact_panel_height, abs=1
    )

    activate(expand_command)
    expect(collapse_command).to_have_attribute("aria-expanded", "true")
    exit_animation = page.add_style_tag(
        content='[data-slot="popover-content"][data-state="closed"] '
        "{ animation-duration: 5s !important; }"
    )
    activate(pill)
    expect(panel).to_have_attribute("data-state", "closed")
    activate(pill)
    expect(panel).to_have_attribute("data-state", "open")
    expect(expand_command).to_have_attribute("aria-expanded", "false")
    compact_command_height()
    exit_animation.evaluate("element => element.remove()")

    composer = page.get_by_label("Message the agent")
    exit_animation = page.add_style_tag(
        content='[data-slot="popover-content"][data-state="closed"] '
        "{ animation-duration: 5s !important; }"
    )
    page.keyboard.press("Escape")
    expect(panel).to_have_attribute("data-state", "closed")
    activate(composer)
    expect(composer).to_be_focused()
    exit_animation.evaluate("element => element.remove()")
    expect(panel).to_have_count(0)
    expect(composer).to_be_focused()

    draft = "Keep watching while I review."
    page.keyboard.type(draft)
    expect(composer).to_have_value(draft)
    _background_status(chat, 0)
    expect(page.get_by_test_id("background-task-pill")).to_have_count(0)
    _background_status(chat, 1, [_MONITOR_TASK])
    expect(_pill(page, 1)).to_have_attribute("aria-expanded", "false", timeout=10_000)
    expect(composer).to_have_value(draft)
