from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.chat.test_claude_model_picker import _patch_session_as_claude_native
from tests.e2e_ui.chat.test_working_indicator_background_tasks import _publish_status
from tests.e2e_ui.conftest import fetch_with_retry


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_status_counts_and_pr_share_workspace_bar(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path, theme: str
) -> None:
    base_url, session_id = seeded_session

    def snapshot(route):
        response = fetch_with_retry(route)
        body = response.json()
        body.update(workspace="/work/repo", host_id="acceptance-host", git_branch="old")
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
                    "number": 123,
                    "title": "Acceptance PR",
                    "url": "https://github.com/example/repo/pull/123",
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
    _publish_status(base_url, session_id, "idle", background_task_count=2)
    page.goto(f"{base_url}/c/{session_id}")
    bar = page.get_by_test_id("composer-workspace-controls")
    expect(bar.get_by_test_id("composer-pr-link").locator("span").last).to_have_text(
        "#123", timeout=30_000
    )
    expect(bar.get_by_test_id("background-task-pill")).to_have_text("2")
    expect(bar.get_by_test_id("subagent-task-pill")).to_have_text("1")
    expect(bar).to_contain_text("live-branch")
    expect(bar).not_to_contain_text("pr-head-not-checkout")
    bounds = bar.bounding_box()
    assert bounds is not None
    for test_id in ("composer-pr-link", "background-task-pill", "subagent-task-pill"):
        rect = bar.get_by_test_id(test_id).bounding_box()
        assert rect is not None
        assert rect["x"] > bounds["x"] + bounds["width"] / 2
        assert bounds["y"] <= rect["y"] < bounds["y"] + bounds["height"]
    bar.screenshot(path=tmp_path / f"status-bar-{theme}.png", animations="disabled")
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
