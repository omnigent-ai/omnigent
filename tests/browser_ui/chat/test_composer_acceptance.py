from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests._helpers.workspace_geometry import workspace_bar_needs_collapse
from tests.browser_ui.chat.session_contract import ChatSessionContract

_MONITOR_TASK = {
    "id": "monitor-ci",
    "type": "shell",
    "status": "running",
    "description": "Watch PR checks and review comments",
    "command": "gh pr checks 123 --watch",
}


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(
    ("viewport_width", "font_size", "pr_number", "font_family"),
    [
        pytest.param(1280, 13, 1234, None, id="desktop-default"),
        pytest.param(
            390,
            18,
            1234,
            None,
            marks=pytest.mark.browser_context_args(has_touch=True),
            id="mobile-crowded",
        ),
        pytest.param(
            375,
            18,
            1234,
            None,
            marks=pytest.mark.browser_context_args(has_touch=True),
            id="mobile-narrow-crowded",
        ),
        pytest.param(
            375,
            18,
            1234567,
            None,
            marks=pytest.mark.browser_context_args(has_touch=True),
            id="mobile-long-pr",
        ),
        pytest.param(
            375,
            18,
            1234,
            "Verdana",
            marks=pytest.mark.browser_context_args(has_touch=True),
            id="mobile-wide-font",
        ),
    ],
)
def test_pr_context_and_background_tasks_share_workspace_bar(
    page: Page,
    chat_session_contract: ChatSessionContract,
    tmp_path: Path,
    theme: str,
    viewport_width: int,
    font_size: int,
    pr_number: int,
    font_family: str | None,
) -> None:
    chat = chat_session_contract
    session_id = chat.session_id
    page.add_init_script("localStorage.setItem('omnigent:default-workspace-panel', 'open')")
    is_mobile = viewport_width < 768
    chat.contract.json("/v1/sessions/acceptance-child/child_sessions", {"data": []})
    chat.contract.json(
        f"/v1/sessions/{session_id}/resources/environments/default",
        {
            "metadata": {"root": "/work/repo", "home": "/home/browser"},
        },
    )

    chat.contract.json(
        f"/v1/sessions/{session_id}/resources/environments/default/filesystem", {"data": []}
    )
    chat.contract.json(
        f"/v1/sessions/{session_id}/resources/environments/default/changes", {"data": []}
    )
    chat.contract.json(f"/v1/sessions/{session_id}/resources/github/changes", {"data": []})
    chat.contract.json(f"/v1/sessions/{session_id}/resources/github/diff", {"patch": ""})
    chat.update_session(
        workspace="/work/repo",
        git_branch="old",
        context_window=1_000_000,
        last_total_tokens=1_000_000,
        background_task_count=1,
        background_tasks=[_MONITOR_TASK],
    )
    page.route(
        f"**/v1/hosts/{chat.host_id}/worktrees?*",
        lambda route: route.fulfill(
            json={
                "data": [
                    {
                        "path": "/work/repo",
                        "branch": "live-branch",
                        "is_main": False,
                        "detached": False,
                    }
                ]
            }
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
                    "number": pr_number,
                    "title": "Acceptance PR",
                    "url": f"https://github.com/example/repo/pull/{pr_number}",
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
    if font_family is not None:
        page.add_init_script(
            f"localStorage.setItem('omnigent:ui-font-family', JSON.stringify('{font_family}'))"
        )
    page.set_viewport_size({"width": viewport_width, "height": 844 if is_mobile else 900})
    page.goto(chat.url)
    chat.wait_for_stream()
    bar = page.get_by_test_id("composer-workspace-controls")
    pr_link = bar.get_by_test_id("composer-pr-link")
    pr_label = pr_link.locator("span").last
    expect(pr_label).to_have_text(f"#{pr_number}", timeout=30_000)
    expect(pr_link).to_have_accessible_name(f"#{pr_number}")
    context = bar.get_by_test_id("composer-context-ring")
    expect(context).to_have_accessible_name("100% of context used")
    if font_family is not None:
        applied_family = context.evaluate("el => getComputedStyle(el).fontFamily")
        assert applied_family.split(",")[0].strip("\"' ") == font_family
    expect(bar.get_by_test_id("background-task-pill")).to_have_text("1")
    subagent = bar.get_by_test_id("subagent-task-pill")
    expect(subagent).to_have_text("1")
    expect(subagent).to_have_accessible_name("1 sub-agent: 1 active")
    expect(bar).to_contain_text("live-branch")
    expect(bar).not_to_contain_text("pr-head-not-checkout")
    bounds = bar.bounding_box()
    assert bounds is not None
    status_ids = (
        "composer-pr-link",
        "background-task-pill",
        "subagent-task-pill",
        "composer-context-ring",
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
    measured_font = context.evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
    print(
        f"Status bar ({viewport_width}px, {font_size}px preference, {theme}): "
        f"bar={bounds}, controls={control_bounds}, icons={icon_bounds}, "
        f"font={measured_font}, family={font_family or 'system'}"
    )
    bar.screenshot(path=tmp_path / f"status-bar-{theme}.png", animations="disabled")
    page.screenshot(path=tmp_path / f"status-page-{theme}.png", animations="disabled")
    directory_icon = icon_bounds["composer-workspace-dir"][0]
    center_y = directory_icon["y"] + directory_icon["height"] / 2
    expected_bar_height = 28 if is_mobile else 37
    expected_center_offset = 14 if is_mobile else 19
    assert bounds["height"] == pytest.approx(expected_bar_height, abs=0.5)
    assert center_y == pytest.approx(bounds["y"] + expected_center_offset, abs=0.5)
    trailing = control_bounds[status_ids[-1]]
    # The docked tray's content inset matches the card's shared inset
    # (1px border + 12px padding).
    assert trailing["x"] + trailing["width"] == pytest.approx(
        bounds["x"] + bounds["width"] - 13, abs=0.5
    )
    background = control_bounds["background-task-pill"]
    subagent = control_bounds["subagent-task-pill"]
    context_ring = control_bounds["composer-context-ring"]
    assert background["x"] + background["width"] == pytest.approx(subagent["x"], abs=0.5)
    assert subagent["x"] + subagent["width"] + 4 == pytest.approx(context_ring["x"], abs=0.5)
    # A label that would have to truncate collapses the whole bar to icons
    # instead — the full value stays in the title — and a bar with room shows
    # every label untruncated. Neither state may show an ellipsis.
    collapsed = bar.get_attribute("data-labels") == "collapsed"
    assert collapsed == workspace_bar_needs_collapse(bar), (viewport_width, font_size, pr_number)
    expect(pr_label).to_have_attribute("title", f"#{pr_number}")
    # The PR number stays visible and the context ring stays accessible; a
    # crowded bar collapses only the directory and branch text to their icons.
    expect(pr_label).to_be_visible()
    expect(context).to_have_accessible_name("100% of context used")
    for chip in ("composer-workspace-dir", "composer-git-branch"):
        chip_label = bar.get_by_test_id(chip).locator("[data-workspace-collapse-label]")
        if collapsed:
            expect(chip_label).to_be_hidden()
        else:
            expect(chip_label).to_be_visible()
    # From 390px up the number shows in full. A 375px bar with the large font
    # setting is the one place it may still ellipsize even with the directory
    # and branch text gone (CI's fonts run wider than macOS's, so it is
    # font-dependent there); its full value stays in the title.
    if viewport_width >= 390:
        assert pr_label.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
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
    ordered_icons = [
        (test_id, icon)
        for test_id in (
            "composer-workspace-dir",
            "composer-git-branch",
            "composer-pr-link",
            "background-task-pill",
            "subagent-task-pill",
            "composer-context-ring",
        )
        for icon in icon_bounds[test_id]
    ]
    for (left_id, left), (right_id, right) in pairwise(ordered_icons):
        assert left["x"] + left["width"] <= right["x"] + 0.5, (
            left_id,
            left,
            right_id,
            right,
        )
    if is_mobile and (pr_number == 1234567 or font_family is not None):
        pr_link.tap()
        panel = page.get_by_test_id("github-panel-drawer")
        expect(panel).to_have_attribute("data-state", "open")
        expect(panel).to_be_visible()
        panel.screenshot(path=tmp_path / "truncated-pr-open.png", animations="disabled")
        panel.get_by_role("button", name="Close", exact=True).tap()
        expect(panel).to_have_attribute("data-state", "closed")
        expect(pr_link).to_be_in_viewport()
    chat.emit(
        {
            "event": "session.status",
            "data": {
                "conversation_id": session_id,
                "status": "idle",
                "background_task_count": 0,
            },
        }
    )
    expect(bar.get_by_test_id("background-task-pill")).to_have_count(0)


@pytest.mark.parametrize("width", [390, 768, 1440, 3200])
def test_long_model_and_permission_remain_single_row(
    page: Page, chat_session_contract: ChatSessionContract, tmp_path: Path, width: int
) -> None:
    chat = chat_session_contract
    page.add_init_script("localStorage.setItem('omnigent:default-workspace-panel', 'open')")
    model = "system.ai.claude-opus-4-8[1m]"
    display_name = "Opus 4.8 (1M context)"
    chat.set_catalog(
        harness="claude",
        selected_model=model,
        models=[{"id": model, "model": model, "displayName": display_name}],
    )
    chat.update_session(
        labels={
            "omnigent.wrapper": "claude-code-native-ui",
            "omnigent.claude_native.permission_mode": "bypassPermissions",
        }
    )
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(chat.url)
    label = page.get_by_test_id("composer-agent-config-value")
    expect(label).to_contain_text("Opus 4.8 1M", timeout=30_000)
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
        "title", "Opus 4.8 1M"
    )
    expect(summary).to_have_attribute("title", display_name)
    page.get_by_test_id("composer-agent-edit").click()
    expect(page.get_by_role("menuitemcheckbox", name=display_name, exact=True)).to_be_visible()
    assert all(results.values()), results
