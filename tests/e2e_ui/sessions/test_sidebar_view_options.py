"""Sidebar view options (Grouping / Show) and the session tooltip's GitHub rows.

The session list and GitHub resource are stubbed with ``page.route`` so the
grouping is deterministic; everything else comes from the live server.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Locator, Page, Route, expect

_RUNNING = f"{1:032x}"
_AWAITING = f"{2:032x}"
_IDLE = f"{3:032x}"


def _row(session_id: str, title: str, **extra: object) -> dict[str, object]:
    return {
        "id": session_id,
        "object": "conversation",
        "title": title,
        "owner": None,
        "permission_level": None,
        "created_at": 1,
        "updated_at": 100,
        "labels": {},
        "status": "idle",
        **extra,
    }


_ROWS = [
    _row(_RUNNING, "Running session", status="running", updated_at=300),
    _row(_AWAITING, "Awaiting session", pending_elicitations_count=1, updated_at=200),
    _row(
        _IDLE,
        "Idle session",
        updated_at=100,
        git_branch="fix/payment-retries",
        workspace="/tmp/sidebar-view-options",
    ),
]


def _serve_sessions(route: Route) -> None:
    params = parse_qs(urlparse(route.request.url).query)
    data = [] if "pinned" in params else _ROWS
    route.fulfill(
        json={
            "data": data,
            "has_more": False,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
        }
    )


@pytest.fixture
def stubbed_sidebar(page: Page, request: pytest.FixtureRequest) -> str:
    """Open the home page with the stubbed session list; return the base URL."""
    base_url = request.config.getoption("--ui-base-url") or request.getfixturevalue("live_server")
    page.route_web_socket("**/v1/sessions/updates*", lambda _socket: None)
    page.route("**/v1/sessions?*", _serve_sessions)
    page.goto(base_url)
    expect(page.get_by_text("Idle session", exact=True)).to_be_visible(timeout=30_000)
    return base_url


def _open_view_submenu(page: Page, submenu_test_id: str) -> None:
    page.get_by_test_id("session-filter").click()
    page.get_by_test_id(submenu_test_id).click()


def _section_header(page: Page, title: str) -> Locator:
    return page.get_by_role("button", name=title, exact=True)


def test_status_grouping_and_show_toggles_persist_across_reload(
    page: Page, stubbed_sidebar: str
) -> None:
    """Status grouping titles the list by state, Show adds the branch line, and both persist."""
    expect(_section_header(page, "Sessions")).to_be_visible()

    _open_view_submenu(page, "session-grouping-menu")
    page.get_by_test_id("session-grouping-status").click()
    for title in ("Needs attention", "Working", "Done"):
        expect(_section_header(page, title)).to_be_visible()
    expect(_section_header(page, "Sessions")).to_have_count(0)
    # Grouped views fold project sessions into the groups.
    expect(_section_header(page, "Projects")).to_have_count(0)

    idle_row = page.locator("li").filter(has=page.locator(f'a[href="/c/{_IDLE}"]'))
    expect(idle_row.get_by_test_id("session-row-meta")).to_have_count(0)
    _open_view_submenu(page, "session-show-menu")
    # Checkbox items keep the menu open, so a second toggle needs no reopen.
    page.get_by_test_id("session-show-branch").click()
    page.get_by_test_id("session-show-environment").click()
    page.keyboard.press("Escape")
    expect(idle_row.get_by_test_id("session-row-meta")).to_have_text(
        "Local machine·fix/payment-retries"
    )

    page.reload()
    expect(_section_header(page, "Needs attention")).to_be_visible(timeout=30_000)
    expect(idle_row.get_by_test_id("session-row-meta")).to_be_visible()


def test_tooltip_fetches_repo_and_pr_only_when_opened(
    page: Page, request: pytest.FixtureRequest
) -> None:
    """Hovering a workspace session loads its repo and PR into the tooltip on demand."""
    base_url = request.config.getoption("--ui-base-url") or request.getfixturevalue("live_server")
    github_requests: list[str] = []

    def serve_github(route: Route) -> None:
        github_requests.append(route.request.url)
        route.fulfill(
            json={
                "object": "session.github.info",
                "available": True,
                "branch": "fix/payment-retries",
                "repo": {"name_with_owner": "omnigent-ai/omnigent"},
                "pr": {"number": 419, "state": "OPEN", "is_draft": True},
            }
        )

    # Register every route BEFORE navigating, so an eager fetch during mount is
    # counted — otherwise the empty-request assertion can't detect it.
    page.route_web_socket("**/v1/sessions/updates*", lambda _socket: None)
    page.route("**/v1/sessions?*", _serve_sessions)
    page.route("**/v1/sessions/*/resources/github*", serve_github)
    page.goto(base_url)
    expect(page.get_by_text("Idle session", exact=True)).to_be_visible(timeout=30_000)
    page.wait_for_load_state("networkidle")
    assert github_requests == []

    page.locator(f'a[href="/c/{_IDLE}"]').hover()
    tooltip = page.get_by_test_id("session-tooltip-content")
    expect(tooltip.get_by_test_id("session-tooltip-repo")).to_have_text("Repoomnigent")
    expect(tooltip.get_by_test_id("session-tooltip-pr")).to_have_text("PR#419 · Draft")
    expect(tooltip.get_by_test_id("session-tooltip-branch")).to_have_text(
        "Branchfix/payment-retries"
    )
    assert all(f"/v1/sessions/{_IDLE}/" in url for url in github_requests)
