"""The composer previews each linked PR and prioritizes actionable states."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import stub_session_pull_requests
from tests.e2e_ui.github.test_github_tab import _INFO


@pytest.mark.parametrize(
    ("viewport_width", "touch"),
    [
        pytest.param(1280, False, id="desktop"),
        pytest.param(390, False, id="narrow-pointer"),
        pytest.param(
            390, True, id="mobile-touch", marks=pytest.mark.browser_context_args(has_touch=True)
        ),
    ],
)
def test_composer_pr_status_preview(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    viewport_width: int,
    touch: bool,
) -> None:
    base_url, session_id = seeded_session
    pull_requests = [
        {
            "url": f"https://github.com/acme/app/pull/{number}",
            "host": "github.com",
            "provider": "github",
            "repository": "acme/app",
            "number": number,
            "title": title,
            "relationship": "created",
            "state": state,
        }
        for number, title, state in [
            (41, "Improve search results", "MERGED"),
            (42, "Previous navigation experiment", "CLOSED"),
            (43, "Add keyboard shortcuts", "OPEN"),
            (44, "Resolve composer layout", "OPEN"),
        ]
    ]

    def respond(route: Route) -> None:
        selected_url = parse_qs(urlsplit(route.request.url).query).get(
            "pr_url", [pull_requests[0]["url"]]
        )[0]
        selected = next(pr for pr in pull_requests if pr["url"] == selected_url)
        route.fulfill(
            json={
                **_INFO,
                "tracking_available": True,
                "selected_pr_url": selected_url,
                "prs": pull_requests,
                "pr": {
                    **_INFO["pr"],
                    **selected,
                    "merge_state_status": "DIRTY" if selected["number"] == 44 else "CLEAN",
                    "checks": {
                        "passing": 3,
                        "failing": 0,
                        "pending": 0,
                        "total": 3,
                        "runs": [],
                    },
                },
            }
        )

    page.route(re.compile(r"/resources/github(?:\?|$)"), respond)
    stub_session_pull_requests(page, lambda: pull_requests, lambda _url: None)
    page.set_viewport_size({"width": viewport_width, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    indicator = page.get_by_test_id("composer-pr-link")
    expect(indicator).to_have_accessible_name("4 PRs", timeout=30_000)
    expect(indicator).to_have_attribute("data-pr-status", "conflict")
    preview = page.get_by_test_id("composer-pr-preview")
    if not touch:
        indicator.hover()
        expect(preview).to_be_visible()
        page.mouse.move(0, 0, steps=10)
        expect(preview).to_be_hidden()

    composer = page.get_by_placeholder("Send a message…")
    composer.focus()
    composer.press("Shift+Tab")
    expect(indicator).to_be_focused()
    expect(preview).to_be_visible()
    preview_id = preview.get_attribute("id")
    assert preview_id
    expect(indicator).to_have_attribute("aria-describedby", preview_id)
    rows = preview.get_by_role("listitem")
    expect(rows).to_have_count(4)
    assert rows.evaluate_all("rows => rows.map(row => row.dataset.prStatus)") == [
        "conflict",
        "ready",
        "merged",
        "closed",
    ]
    for row, title, label in zip(
        rows.all(),
        [pr["title"] for pr in [pull_requests[3], pull_requests[2], *pull_requests[:2]]],
        ["Merge conflicts", "Ready to merge", "Merged", "Closed"],
        strict=True,
    ):
        expect(row).to_contain_text(title)
        expect(row.get_by_text(label, exact=True)).to_be_visible()
        expect(row.locator("svg")).to_have_count(1)
    box = preview.bounding_box()
    assert box and box["x"] >= 16 and box["x"] + box["width"] <= viewport_width - 16
    page.screenshot(path=tmp_path / "composer-pr-preview.png", animations="disabled")
    indicator.press("Escape")
    expect(preview).to_be_hidden()
    if touch:
        page.screenshot(path=tmp_path / "composer-pr-touch.png", animations="disabled")
        indicator.tap()
    else:
        indicator.click()
    panel = (
        page.get_by_test_id("github-panel-drawer")
        if viewport_width < 768
        else page.get_by_role("complementary", name="Workspace")
    )
    expect(panel.get_by_role("list", name="Pull requests")).to_be_visible()
    expect(
        panel.get_by_role("button", name="acme/app #44 — Resolve composer layout", exact=True)
    ).to_be_visible()
    page.screenshot(path=tmp_path / "composer-pr-panel.png", animations="disabled")
    if viewport_width >= 768:
        page.mouse.move(0, 0, steps=10)
        indicator.hover()
        expect(preview).to_be_visible()
        page.screenshot(path=tmp_path / "composer-pr-panel-preview.png", animations="disabled")
