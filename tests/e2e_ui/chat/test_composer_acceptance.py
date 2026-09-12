from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.chat.test_claude_model_picker import _patch_session_as_claude_native
from tests.e2e_ui.chat.test_working_indicator_background_tasks import _publish_status
from tests.e2e_ui.conftest import fetch_with_retry


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(
    ("viewport_width", "font_size"),
    [
        pytest.param(1280, 13, id="desktop-default"),
        pytest.param(
            390,
            18,
            marks=pytest.mark.browser_context_args(has_touch=True),
            id="mobile-crowded",
        ),
        pytest.param(
            375,
            18,
            marks=pytest.mark.browser_context_args(has_touch=True),
            id="mobile-narrow-crowded",
        ),
    ],
)
def test_status_counts_and_pr_share_workspace_bar(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    theme: str,
    viewport_width: int,
    font_size: int,
) -> None:
    base_url, session_id = seeded_session
    is_mobile = viewport_width < 768

    def snapshot(route):
        response = fetch_with_retry(route)
        body = response.json()
        body.update(
            workspace="/work/repo",
            host_id="acceptance-host",
            git_branch="old",
            context_window=1_000_000,
            last_total_tokens=1_000_000,
        )
        route.fulfill(response=response, json=body)

    page.route(re.compile(rf"/v1/sessions/{session_id}(?:\?.*)?$"), snapshot)
    page.route(
        "**/v1/hosts/acceptance-host/worktrees?*",
        lambda route: route.fulfill(
            json={"data": [{"path": "/work/repo", "branch": "live-branch", "is_main": True}]}
        ),
    )
    page.route(
        f"**/v1/sessions/{session_id}/resources/github",
        lambda route: route.fulfill(
            json={
                "object": "session.github.info",
                "available": True,
                "gh_available": True,
                "authenticated": True,
                "repo": {"name_with_owner": "example/repo"},
                "branch": "pr-head-not-checkout",
                "pr": {
                    "number": 1234,
                    "title": "Acceptance PR",
                    "url": "https://github.com/example/repo/pull/1234",
                    "state": "OPEN",
                    "is_draft": False,
                    "checks": {"passing": 0, "failing": 0, "pending": 0, "total": 0, "runs": []},
                },
            }
        ),
    )
    page.route(
        f"**/v1/sessions/{session_id}/child_sessions",
        lambda route: route.fulfill(
            json={
                "data": [
                    {
                        "id": "acceptance-child",
                        "busy": True,
                        "title": "Review changes",
                        "task_summary": "Review changes",
                        "tool": "agent",
                        "labels": {},
                    }
                ]
            }
        ),
    )
    page.emulate_media(color_scheme=theme)
    page.add_init_script(f"localStorage.setItem('web-theme', '{theme}')")
    page.add_init_script(f"localStorage.setItem('omnigent:ui-font-size', '{font_size}')")
    page.set_viewport_size({"width": viewport_width, "height": 844 if is_mobile else 900})
    _publish_status(base_url, session_id, "idle", background_task_count=2)
    page.goto(f"{base_url}/c/{session_id}")
    bar = page.get_by_test_id("composer-workspace-controls")
    expect(bar.get_by_test_id("composer-pr-link").locator("span").last).to_have_text(
        "#1234", timeout=30_000
    )
    context = bar.get_by_test_id("composer-context-ring")
    expect(context).to_have_text("100%")
    expect(bar.get_by_test_id("background-task-pill")).to_have_text("2")
    expect(bar.get_by_test_id("subagent-task-pill")).to_have_text("1")
    expect(bar).to_contain_text("live-branch")
    expect(bar).not_to_contain_text("pr-head-not-checkout")
    bounds = bar.bounding_box()
    assert bounds is not None
    status_ids = (
        "composer-pr-link",
        "composer-context-ring",
        "background-task-pill",
        "subagent-task-pill",
    )
    control_bounds = {}
    icon_bounds = {}
    for test_id in ("composer-workspace-dir", "composer-git-branch", *status_ids):
        control = bar.get_by_test_id(test_id)
        rect = control.bounding_box()
        assert rect is not None
        control_bounds[test_id] = rect
        icon_bounds[test_id] = []
        for icon in control.locator("svg").all():
            icon_rect = icon.bounding_box()
            assert icon_rect is not None
            icon_bounds[test_id].append(icon_rect)
    measured_font = context.locator("span").evaluate(
        "el => parseFloat(getComputedStyle(el).fontSize)"
    )
    print(
        f"Status bar ({viewport_width}px, {font_size}px preference, {theme}): "
        f"bar={bounds}, controls={control_bounds}, icons={icon_bounds}, font={measured_font}"
    )
    bar.screenshot(path=tmp_path / f"status-bar-{theme}.png", animations="disabled")
    page.screenshot(path=tmp_path / f"status-page-{theme}.png", animations="disabled")
    assert measured_font == pytest.approx(
        font_size * 0.9 * (14 / 13 if is_mobile else 1), abs=0.01
    )
    directory_icon = icon_bounds["composer-workspace-dir"][0]
    center_y = directory_icon["y"] + directory_icon["height"] / 2
    assert bounds["height"] == pytest.approx(37, abs=0.5)
    assert center_y == pytest.approx(bounds["y"] + 19, abs=0.5)
    trailing = control_bounds[status_ids[-1]]
    assert trailing["x"] + trailing["width"] == pytest.approx(
        bounds["x"] + bounds["width"] - 9, abs=0.5
    )
    for test_id in status_ids:
        rect = control_bounds[test_id]
        assert rect["y"] + rect["height"] / 2 == pytest.approx(center_y, abs=0.5)
    for test_id, rect in control_bounds.items():
        assert rect["x"] >= bounds["x"] - 0.5, (test_id, rect, bounds)
        assert rect["x"] + rect["width"] <= bounds["x"] + bounds["width"] + 0.5, (
            test_id,
            rect,
            bounds,
        )
        assert rect["y"] >= bounds["y"] - 0.5, (test_id, rect, bounds)
        assert rect["y"] + rect["height"] <= bounds["y"] + bounds["height"] + 0.5, (
            test_id,
            rect,
            bounds,
        )
        for icon in icon_bounds[test_id]:
            assert icon["x"] >= rect["x"] - 0.5, (test_id, icon, rect)
            assert icon["x"] + icon["width"] <= rect["x"] + rect["width"] + 0.5, (
                test_id,
                icon,
                rect,
            )
    ordered_icons = [(test_id, icon) for test_id, icons in icon_bounds.items() for icon in icons]
    for (left_id, left), (right_id, right) in pairwise(ordered_icons):
        assert left["x"] + left["width"] <= right["x"] + 0.5, (
            left_id,
            left,
            right_id,
            right,
        )
    if is_mobile:
        bar.get_by_test_id("subagent-task-pill").tap()
    else:
        bar.get_by_test_id("subagent-task-pill").click()
    expect(page.get_by_role("dialog")).to_contain_text("Review changes")
    page.keyboard.press("Escape")
    _publish_status(base_url, session_id, "idle", background_task_count=0)
    expect(bar.get_by_test_id("background-task-pill")).to_have_count(0)
    page.unroute_all(behavior="wait")


@pytest.mark.parametrize("width", [390, 768, 1440, 3200])
def test_long_model_and_permission_remain_single_row(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path, width: int
) -> None:
    base_url, session_id = seeded_session
    model = "system.ai.claude-opus-4-8[1m]"
    display_name = "Opus 4.8 (1M context)"
    _patch_session_as_claude_native(
        page,
        session_id,
        llm_model=model,
        permission_mode="bypassPermissions",
        model_options=[{"id": model, "model": model, "displayName": display_name}],
    )
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    label = page.get_by_test_id("composer-agent-config-value")
    expect(label).to_contain_text(display_name, timeout=30_000)
    permission = page.get_by_test_id("composer-permission-chip")
    expect(permission).to_be_visible()
    if width == 3200:
        for text in (
            page.get_by_test_id("composer-agent-model-value"),
            permission.locator("span"),
        ):
            assert text.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
    page.screenshot(path=tmp_path / f"long-model-{width}.png", animations="disabled")
    permission_bounds = permission.bounding_box()
    send_bounds = page.get_by_role("button", name="Send", exact=True).bounding_box()
    assert permission_bounds is not None and send_bounds is not None
    page.get_by_test_id("composer-config-gear").click()
    summary = page.get_by_test_id("composer-agent-model-summary")
    expect(summary).to_contain_text(display_name)
    page.get_by_test_id("composer-agent-menu").screenshot(
        path=tmp_path / f"harness-row-{width}.png", animations="disabled"
    )
    summary_dimensions = summary.evaluate("""element => {
      const range = document.createRange(); range.selectNodeContents(element);
      const rects = [...range.getClientRects()].filter(rect => rect.width > 0);
      return {lines: [...new Set(rects.map(rect => Math.round(rect.top)))],
        width: element.clientWidth, scrollWidth: element.scrollWidth};
    }""")
    results = {
        "toolbar_single_row": abs(permission_bounds["y"] - send_bounds["y"]) < 10,
        "harness_summary_single_line": len(summary_dimensions["lines"]) == 1,
        "send_on_screen": send_bounds["x"] + send_bounds["width"] <= width,
    }
    expect(page.get_by_test_id("composer-agent-model-value")).to_have_attribute(
        "title", display_name
    )
    expect(summary).to_have_attribute("title", display_name)
    page.get_by_test_id("composer-agent-edit").click()
    expect(page.get_by_role("menuitemcheckbox", name=display_name, exact=True)).to_be_visible()
    page.unroute_all(behavior="wait")
    assert all(results.values()), results
