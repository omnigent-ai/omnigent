"""Background tasks stay visible without making an idle turn look busy.

Real native status events drive the tally, read-only details, and working state.
Monitors arrive as running shells in Claude's Stop-hook background-task list.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.chat._working_labels import WORKING_LABEL_RE as _WORKING_LABEL_RE

_WORKING = '[data-testid="working-indicator"]'
_PILL = '[data-testid="background-task-pill"]'
_MONITOR_TASK = {
    "id": "monitor-ci",
    "type": "shell",
    "status": "running",
    "description": "Watch PR checks and review comments",
    "command": "gh pr checks 123 --watch",
}


def _pill_badge(page: Page, count: int) -> Locator:
    plural = "" if count == 1 else "s"
    return page.get_by_role(
        "button", name=f"{count} background task{plural} still running", exact=True
    )


def _publish_status(
    base_url: str,
    session_id: str,
    status: str,
    *,
    response_id: str | None = None,
    background_task_count: int | None = None,
    background_tasks: list[dict[str, str]] | None = None,
) -> None:
    """Publish a status edge; an omitted count preserves the sticky tally."""
    data: dict[str, object] = {"status": status}
    if response_id is not None:
        data["response_id"] = response_id
    if background_task_count is not None:
        data["background_task_count"] = background_task_count
    if background_tasks is not None:
        data["background_tasks"] = background_tasks
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    resp.raise_for_status()


def test_background_task_indicator_label_lifecycle(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    working = page.locator(_WORKING)
    _publish_status(
        base_url,
        session_id,
        "idle",
        background_task_count=1,
        background_tasks=[_MONITOR_TASK],
    )
    page.goto(f"{base_url}/c/{session_id}")
    expect(_pill_badge(page, 1)).to_have_text("1", timeout=15_000)
    expect(working).to_have_count(0)

    _publish_status(base_url, session_id, "running")
    expect(working).to_contain_text(_WORKING_LABEL_RE, timeout=15_000)
    expect(_pill_badge(page, 1)).to_have_text("1")

    _publish_status(base_url, session_id, "idle")
    expect(working).to_have_count(0, timeout=15_000)
    expect(_pill_badge(page, 1)).to_have_text("1")

    _publish_status(base_url, session_id, "idle", background_task_count=0)
    expect(page.locator(_PILL)).to_have_count(0, timeout=15_000)


@pytest.mark.parametrize(
    "width",
    [1280, pytest.param(375, marks=pytest.mark.browser_context_args(has_touch=True))],
)
def test_monitor_details_keep_the_composer_interactive(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    width: int,
) -> None:
    base_url, session_id = seeded_session

    def activate(control: Locator) -> None:
        (control.tap if width < 768 else control.click)()

    monitor = {
        **_MONITOR_TASK,
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
    page.set_viewport_size({"width": width, "height": 844})
    _publish_status(
        base_url,
        session_id,
        "idle",
        background_task_count=1,
        background_tasks=[monitor],
    )
    page.goto(f"{base_url}/c/{session_id}")
    pill = _pill_badge(page, 1)
    expect(pill).to_have_text("1", timeout=15_000)
    expect(pill).to_have_attribute("aria-expanded", "false")
    activate(pill)
    panel = page.get_by_role("dialog", name="1 background task", exact=True)
    expect(panel).to_be_visible()
    expect(panel.get_by_role("listitem")).to_have_count(1)
    description = panel.get_by_text(monitor["description"], exact=True)
    command = panel.get_by_text(monitor["command"], exact=True)
    expect(description).to_be_visible()
    expect(command).to_be_visible()
    expand_command = panel.get_by_role("button", name="Expand command", exact=True)
    collapse_command = panel.get_by_role("button", name="Collapse command", exact=True)
    expect(expand_command).to_have_attribute("aria-expanded", "false")
    expect(expand_command).to_have_text("")
    expect(expand_command.locator("svg")).to_be_visible()
    expect(panel.get_by_role("button")).to_have_count(1)
    expect(panel.get_by_role("menuitem")).to_have_count(0)

    def compact_command_height() -> float:
        dimensions = command.evaluate(
            """el => ({height: el.clientHeight,
              lineHeight: parseFloat(getComputedStyle(el).lineHeight)})"""
        )
        assert 0 < dimensions["height"] <= 2 * dimensions["lineHeight"] + 1
        return dimensions["height"]

    compact_height = compact_command_height()
    compact_panel_height = panel.evaluate("el => el.clientHeight")
    bounds = panel.bounding_box()
    assert bounds is not None
    assert bounds["x"] >= 0
    assert bounds["x"] + bounds["width"] <= width
    for element in (panel, description, command):
        assert element.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    toggle_bounds = expand_command.bounding_box()
    command_bounds = command.bounding_box()
    description_bounds = description.bounding_box()
    assert toggle_bounds is not None and command_bounds is not None
    assert description_bounds is not None
    assert toggle_bounds["x"] >= command_bounds["x"] + command_bounds["width"] - 0.5
    assert toggle_bounds["y"] == pytest.approx(description_bounds["y"], abs=1)
    panel.screenshot(path=tmp_path / f"monitor-panel-compact-{width}.png", animations="disabled")
    page.screenshot(path=tmp_path / f"monitor-page-compact-{width}.png", animations="disabled")

    activate(expand_command)
    expect(collapse_command).to_have_attribute("aria-expanded", "true")
    expect(collapse_command).to_have_text("")
    assert command.evaluate("el => el.clientHeight") > compact_height
    assert panel.evaluate("el => el.clientHeight") > compact_panel_height
    for element in (panel, description, command):
        assert element.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    expanded_bounds = panel.bounding_box()
    assert expanded_bounds is not None
    assert expanded_bounds["x"] >= 0
    assert expanded_bounds["x"] + expanded_bounds["width"] <= width
    panel.screenshot(path=tmp_path / f"monitor-panel-expanded-{width}.png", animations="disabled")
    page.screenshot(path=tmp_path / f"monitor-page-expanded-{width}.png", animations="disabled")

    activate(collapse_command)
    expect(expand_command).to_have_attribute("aria-expanded", "false")
    assert compact_command_height() == pytest.approx(compact_height, abs=1)
    assert panel.evaluate("el => el.clientHeight") == pytest.approx(compact_panel_height, abs=1)

    activate(expand_command)
    expect(collapse_command).to_have_attribute("aria-expanded", "true")
    exit_animation = page.add_style_tag(
        content='[data-slot="popover-content"][data-state="closed"] '
        "{ animation-duration: 5s !important; }"
    )
    activate(pill)
    expect(pill).to_have_attribute("aria-expanded", "false")
    expect(panel).to_have_attribute("data-state", "closed")
    # Reopen while Radix still retains the closing panel, before it unmounts.
    activate(pill)
    expect(panel).to_have_attribute("data-state", "open")
    expect(expand_command).to_have_attribute("aria-expanded", "false")
    compact_command_height()
    exit_animation.evaluate("el => el.remove()")

    activate(expand_command)
    expect(collapse_command).to_have_attribute("aria-expanded", "true")
    page.keyboard.press("Escape")
    expect(panel).to_have_count(0)
    expect(pill).to_be_focused()

    activate(pill)
    expect(panel).to_be_visible()
    expect(expand_command).to_have_attribute("aria-expanded", "false")
    compact_command_height()
    composer = page.get_by_label("Message the agent")
    activate(composer)
    expect(panel).to_have_count(0)
    expect(composer).to_be_focused()
    draft = "Keep watching while I review."
    page.keyboard.type(draft)
    expect(composer).to_have_value(draft)

    activate(pill)
    expect(panel).to_be_visible()
    exit_animation = page.add_style_tag(
        content='[data-slot="popover-content"][data-state="closed"] '
        "{ animation-duration: 5s !important; }"
    )
    page.keyboard.press("Escape")
    expect(panel).to_have_attribute("data-state", "closed")
    activate(composer)
    expect(composer).to_be_focused()
    exit_animation.evaluate("el => el.remove()")
    expect(panel).to_have_count(0)
    expect(composer).to_be_focused()
    page.keyboard.press("End")
    page.keyboard.type(" Please keep me posted.")
    draft += " Please keep me posted."
    expect(composer).to_have_value(draft)

    activate(pill)
    expect(panel).to_be_visible()
    _publish_status(base_url, session_id, "idle", background_task_count=0)
    expect(panel).to_have_count(0, timeout=15_000)
    expect(page.locator(_PILL)).to_have_count(0)
    _publish_status(base_url, session_id, "idle", background_task_count=1)
    expect(pill).to_have_attribute("aria-expanded", "false", timeout=15_000)
    expect(composer).to_have_value(draft)


def test_sidebar_spinner_ignores_background_tasks(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    working = page.locator(_WORKING)
    running_badge = page.locator('[data-testid="session-state-badge"][data-state="running"]')
    _publish_status(base_url, session_id, "idle", background_task_count=1)
    page.goto(f"{base_url}/c/{session_id}")
    expect(_pill_badge(page, 1)).to_have_text("1", timeout=15_000)
    expect(running_badge).to_have_count(0)

    _publish_status(base_url, session_id, "running")
    expect(running_badge).to_have_count(1, timeout=15_000)

    _publish_status(base_url, session_id, "idle", background_task_count=0)
    expect(working).to_have_count(0, timeout=15_000)
    expect(running_badge).to_have_count(0, timeout=15_000)
    expect(page.locator(_PILL)).to_have_count(0)


def test_badge_survives_reload_and_tracks_updates_after_reconnect(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    _publish_status(
        base_url,
        session_id,
        "idle",
        background_task_count=1,
        background_tasks=[_MONITOR_TASK],
    )
    page.goto(f"{base_url}/c/{session_id}")
    expect(_pill_badge(page, 1)).to_have_text("1", timeout=15_000)
    _pill_badge(page, 1).click()
    expect(page.get_by_role("dialog", name="1 background task", exact=True)).to_be_visible()
    page.reload()
    expect(_pill_badge(page, 1)).to_have_attribute("aria-expanded", "false", timeout=15_000)
    _pill_badge(page, 1).click()
    panel = page.get_by_role("dialog", name="1 background task", exact=True)
    expect(panel.get_by_text(_MONITOR_TASK["description"], exact=True)).to_be_visible()
    page.keyboard.press("Escape")

    _publish_status(base_url, session_id, "idle", background_task_count=2)
    expect(_pill_badge(page, 2)).to_have_text("2", timeout=15_000)
    _pill_badge(page, 2).click()
    panel = page.get_by_role("dialog", name="2 background tasks", exact=True)
    expect(panel).to_contain_text("details unavailable")
    expect(panel.get_by_role("listitem")).to_have_count(0)
    _publish_status(base_url, session_id, "idle", background_task_count=0)
    expect(page.locator(_PILL)).to_have_count(0, timeout=15_000)
    expect(panel).to_have_count(0)


def test_badge_count_is_scoped_to_the_active_session(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    base_url, session_a, session_b = seeded_session_pair
    _publish_status(
        base_url,
        session_a,
        "idle",
        background_task_count=1,
        background_tasks=[_MONITOR_TASK],
    )
    page.goto(f"{base_url}/c/{session_a}")
    expect(_pill_badge(page, 1)).to_have_text("1", timeout=15_000)
    _pill_badge(page, 1).click()
    panel = page.get_by_role("dialog", name="1 background task", exact=True)
    expect(panel).to_be_visible()

    page.locator(f'a[href="/c/{session_b}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_b}", timeout=15_000)
    expect(page.get_by_test_id("composer-workspace-controls")).to_be_visible()
    expect(page.locator(_PILL)).to_have_count(0)
    expect(panel).to_have_count(0)

    page.locator(f'a[href="/c/{session_a}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_a}", timeout=15_000)
    expect(_pill_badge(page, 1)).to_have_attribute("aria-expanded", "false", timeout=15_000)
    _pill_badge(page, 1).click()
    expect(panel.get_by_text(_MONITOR_TASK["description"], exact=True)).to_be_visible()
