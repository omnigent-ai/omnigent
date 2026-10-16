"""Sidebar view options and the session tooltip's GitHub rows, with ``sidebar_grouping_ui`` on.

The session list and GitHub resource are stubbed with ``page.route`` so the
grouping is deterministic; everything else comes from the live server.
"""

from __future__ import annotations

import json
from time import time
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Locator, Page, Route, WebSocketRoute, expect

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
    _row(_RUNNING, "Running session", status="running", created_at=10, updated_at=300),
    _row(
        _AWAITING, "Awaiting session", pending_elicitations_count=1, created_at=20, updated_at=200
    ),
    _row(
        _IDLE,
        "Idle session",
        created_at=30,
        updated_at=100,
        git_branch="fix/payment-retries",
        workspace="/tmp/sidebar-view-options",
        labels={"omnigent.sandbox.repo.0": "https://github.com/omnigent-ai/omnigent#main"},
    ),
]


def _serve_sessions(route: Route) -> None:
    params = parse_qs(urlparse(route.request.url).query)
    data = (
        []
        if "pinned" in params
        else sorted(
            _ROWS,
            key=lambda row: (row[params.get("sort_by", ["created_at"])[0]], row["id"]),
            reverse=True,
        )
    )
    route.fulfill(
        json={
            "data": data,
            "has_more": False,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
        }
    )


def _enable_grouping_ui(page: Page, enabled: bool = True) -> None:
    """Advertise the ``sidebar_grouping_ui`` release flag on the live server's info."""

    def serve_info(route: Route) -> None:
        response = route.fetch()
        info = response.json()
        info["features"] = {**info.get("features", {}), "sidebar_grouping_ui": enabled}
        route.fulfill(response=response, json=info)

    page.route("**/v1/info", serve_info)


@pytest.fixture
def stubbed_sidebar(page: Page, request: pytest.FixtureRequest) -> str:
    """Open the home page with the stubbed session list; return the base URL."""
    base_url = request.config.getoption("--ui-base-url") or request.getfixturevalue("live_server")
    _enable_grouping_ui(page)
    page.route_web_socket("**/v1/sessions/updates*", lambda _socket: None)
    page.route("**/v1/sessions?*", _serve_sessions)
    page.goto(base_url)
    expect(page.get_by_text("Idle session", exact=True)).to_be_visible(timeout=30_000)
    return base_url


def _open_view_submenu(page: Page, submenu_test_id: str) -> None:
    page.get_by_test_id("session-filter").press("ArrowDown")
    page.get_by_test_id(submenu_test_id).click()


def _section_header(page: Page, title: str) -> Locator:
    return page.get_by_role("button", name=title, exact=True)


def test_grouping_and_show_toggles_persist_across_reload(
    page: Page, stubbed_sidebar: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Status holds idle transitions; all grouping choices and Show persist."""
    sockets: list[WebSocketRoute] = []
    page.route_web_socket("**/v1/sessions/updates*", lambda socket: sockets.append(socket))
    _open_view_submenu(page, "session-grouping-menu")
    page.get_by_role("menuitemradio", name="Status", exact=True).click()
    page.keyboard.press("Escape")
    page.reload()
    working = page.locator("section").filter(has=_section_header(page, "Working"))
    done = page.locator("section").filter(has=_section_header(page, "Done"))
    running_link = f'a[href="/c/{_RUNNING}"]'
    expect(working.locator(running_link)).to_be_visible()
    expect(done.locator(f'a[href="/c/{_IDLE}"]')).to_be_visible()
    running_badge = (
        page.locator("li")
        .filter(has=page.locator(running_link))
        .get_by_test_id("session-state-badge")
    )
    expect(running_badge).to_have_attribute("data-state", "running")
    page.get_by_text("What should we build?", exact=True).hover()
    assert sockets, "Session updates socket never connected"
    monkeypatch.setitem(_ROWS[0], "status", "idle")
    sockets[-1].send(json.dumps({"type": "changed", "items": [_ROWS[0]]}))
    expect(running_badge).to_have_count(0)
    expect(working.locator(running_link)).to_be_visible()
    expect(done.locator(running_link)).to_be_visible(timeout=15_000)
    expect(working.get_by_text("No sessions", exact=True)).to_be_visible()
    monkeypatch.setitem(_ROWS[0], "status", "running")
    sockets[-1].send(json.dumps({"type": "changed", "items": [_ROWS[0]]}))
    expect(working.locator(running_link)).to_be_visible(timeout=2_000)
    expect(working.get_by_text("No sessions", exact=True)).to_have_count(0)

    today, yesterday = page.evaluate("""() => {
        const day = new Date();
        day.setHours(0, 0, 0, 0);
        const today = day.getTime() / 1000;
        day.setDate(day.getDate() - 1);
        return [today, day.getTime() / 1000];
    }""")
    for row, created_at, updated_at in [
        (_ROWS[0], yesterday, today),
        (_ROWS[1], 0, yesterday),
        (_ROWS[2], today, today),
    ]:
        monkeypatch.setitem(row, "created_at", created_at)
        monkeypatch.setitem(row, "updated_at", updated_at)
    page.reload()
    expect(_section_header(page, "Working")).to_be_visible()
    filter_button = page.get_by_test_id("session-filter")
    expect(filter_button).to_have_accessible_name("Group, sort, and filter sessions")
    filter_button.hover()
    expect(page.get_by_role("tooltip")).to_have_text("Group, sort, and filter sessions")
    before = filter_button.bounding_box()

    _open_view_submenu(page, "session-grouping-menu")
    expect(page.get_by_role("menuitemradio", name="Status", exact=True)).to_be_checked()
    expect(page.get_by_role("menuitemradio", name="Created", exact=True)).to_be_visible()
    page.get_by_test_id("session-grouping-updated").click()
    page.keyboard.press("Escape")
    expect(_section_header(page, "Updated today")).to_be_visible()
    updated_yesterday = page.locator("section").filter(has=_section_header(page, "Yesterday"))
    expect(updated_yesterday.locator(f'a[href="/c/{_AWAITING}"]')).to_be_visible()
    expect(_section_header(page, "Sessions")).to_have_count(0)
    # The first group takes the Projects header's place, so the filter stays put.
    expect(_section_header(page, "Projects")).to_have_count(0)
    first_group = page.locator("section").filter(has=_section_header(page, "Updated today"))
    expect(first_group.get_by_test_id("session-filter")).to_be_visible()
    after = filter_button.bounding_box()
    assert before is not None and after is not None
    assert abs(after["y"] - before["y"]) < 1

    idle_row = page.locator("li").filter(has=page.locator(f'a[href="/c/{_IDLE}"]'))
    expect(idle_row.get_by_test_id("session-row-meta")).to_have_count(0)
    _open_view_submenu(page, "session-show-menu")
    # Checkbox items keep the menu open, so later toggles need no reopen.
    page.get_by_test_id("session-show-branch").click()
    page.get_by_test_id("session-show-environment").click()
    page.get_by_test_id("session-show-repo").click()
    page.keyboard.press("Escape")
    # Environment is icon-only visually; its label remains available to screen readers.
    meta = "Local machine·omnigent·fix/payment-retries"
    expect(idle_row.get_by_test_id("session-row-meta")).to_have_text(meta)
    expect(idle_row.get_by_text("Local machine", exact=True)).to_have_class("sr-only")

    page.reload()
    expect(_section_header(page, "Updated today")).to_be_visible(timeout=30_000)
    # Every selected field survives the reload, not just some metadata.
    expect(idle_row.get_by_test_id("session-row-meta")).to_have_text(meta)

    _open_view_submenu(page, "session-grouping-menu")
    page.get_by_role("menuitemradio", name="Created", exact=True).click()
    page.keyboard.press("Escape")
    for title, session_id in [
        ("Created today", _IDLE),
        ("Yesterday", _RUNNING),
        ("Earlier", _AWAITING),
    ]:
        section = page.locator("section").filter(has=_section_header(page, title))
        expect(section.locator(f'a[href="/c/{session_id}"]')).to_be_visible()
    page.reload()
    expect(_section_header(page, "Created today")).to_be_visible(timeout=30_000)
    expect(idle_row.get_by_test_id("session-row-meta")).to_have_text(meta)


def test_tooltip_fetches_repo_and_pr_only_when_opened(
    page: Page, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Branch resolves without hovering; repo and PR still load only on hover."""
    base_url = request.config.getoption("--ui-base-url") or request.getfixturevalue("live_server")
    github_requests: list[str] = []
    worktree_requests: list[str] = []
    monkeypatch.setitem(_ROWS[2], "git_branch", None)
    monkeypatch.setitem(_ROWS[2], "host_id", "sidebar-host")

    def serve_worktrees(route: Route) -> None:
        worktree_requests.append(route.request.url)
        route.fulfill(
            json={
                "object": "list",
                "data": [
                    {
                        "path": "/tmp/sidebar-view-options",
                        "branch": "fix/payment-retries",
                        "is_main": True,
                        "detached": False,
                    }
                ],
            }
        )

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
    _enable_grouping_ui(page)
    page.route_web_socket("**/v1/sessions/updates*", lambda _socket: None)
    page.route("**/v1/sessions?*", _serve_sessions)
    page.route("**/v1/sessions/*/resources/github*", serve_github)
    page.route("**/v1/hosts/sidebar-host/worktrees?*", serve_worktrees)
    page.goto(base_url)
    expect(page.get_by_text("Idle session", exact=True)).to_be_visible(timeout=30_000)
    page.wait_for_load_state("networkidle")
    assert github_requests == []
    assert worktree_requests == []

    _open_view_submenu(page, "session-show-menu")
    page.get_by_test_id("session-show-branch").click()
    page.keyboard.press("Escape")
    row = page.locator("li").filter(has=page.locator(f'a[href="/c/{_IDLE}"]'))
    expect(row.get_by_test_id("session-row-meta")).to_have_text("fix/payment-retries")
    assert worktree_requests
    assert parse_qs(urlparse(worktree_requests[0]).query)["path"] == ["/tmp/sidebar-view-options"]
    assert github_requests == []

    page.locator(f'a[href="/c/{_IDLE}"]').hover()
    tooltip = page.get_by_test_id("session-tooltip-content")
    expect(tooltip.get_by_test_id("session-tooltip-repo")).to_have_text("Repoomnigent")
    expect(tooltip.get_by_test_id("session-tooltip-pr")).to_have_text("PR#419 · Draft")
    expect(tooltip.get_by_test_id("session-tooltip-branch")).to_have_text(
        "Branchfix/payment-retries"
    )
    assert github_requests
    assert all(f"/v1/sessions/{_IDLE}/" in url for url in github_requests)


def test_created_ordering_persists_and_flag_off_uses_updated(
    page: Page, stubbed_sidebar: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The selected order reaches the API, survives reload, and is ignored with the flag off."""
    requests: list[str] = []
    page.on(
        "request",
        lambda request: requests.append(request.url) if "/v1/sessions?" in request.url else None,
    )
    links = page.locator('section a[href^="/c/"]')
    expect(links).to_have_text(["Running session", "Awaiting session", "Idle session"])
    _open_view_submenu(page, "session-ordering-menu")
    expect(page.get_by_test_id("session-ordering-status")).to_have_count(0)
    page.get_by_role("menuitemradio", name="Created", exact=True).click()
    page.keyboard.press("Escape")
    expect(links).to_have_text(["Idle session", "Awaiting session", "Running session"])
    assert any("sort_by=created_at" in url for url in requests)
    # Change the API row while the old Updated snapshot is still fresh.
    monkeypatch.setitem(_ROWS[2], "updated_at", int(time()))
    requests.clear()
    pending: list[Route] = []

    def defer_sessions(route: Route) -> None:
        pending.append(route)

    page.route("**/v1/sessions?*", defer_sessions)
    _open_view_submenu(page, "session-ordering-menu")
    page.get_by_role("menuitemradio", name="Updated", exact=True).click()
    page.keyboard.press("Escape")
    try:
        expect(links).to_have_count(0)
        expect(page.get_by_text("Loading…", exact=True)).to_be_visible()
    finally:
        page.unroute("**/v1/sessions?*", defer_sessions)
        for route in pending:
            _serve_sessions(route)
    # Leaving the sidebar releases its hover-order freeze.
    page.get_by_text("What should we build?", exact=True).hover()
    expect(links).to_have_text(["Idle session (unread)", "Running session", "Awaiting session"])
    assert any("sort_by=updated_at" in url for url in requests)
    page.locator(f'a[href="/c/{_IDLE}"]').click(button="right")
    page.get_by_test_id("mark-read-conversation").click()
    _open_view_submenu(page, "session-show-menu")
    page.get_by_test_id("session-show-updated").click()
    page.keyboard.press("Escape")
    idle_row = page.locator("li").filter(has=page.locator(f'a[href="/c/{_IDLE}"]'))
    expect(idle_row.get_by_test_id("session-row-updated")).to_have_text("now")
    _open_view_submenu(page, "session-ordering-menu")
    page.get_by_role("menuitemradio", name="Created", exact=True).click()
    page.keyboard.press("Escape")
    page.reload()
    expect(links).to_have_text(["Idle session", "Awaiting session", "Running session"])
    _enable_grouping_ui(page, enabled=False)
    requests.clear()
    page.reload()
    expect(links).to_have_text(["Idle session", "Running session", "Awaiting session"])
    assert requests and all("sort_by=updated_at" in url for url in requests)
    filter_button = page.get_by_role("button", name="Filter sessions", exact=True)
    filter_button.hover()
    expect(page.get_by_role("tooltip")).to_have_text("Filter sessions")
    filter_button.click()
    expect(page.get_by_test_id("session-ordering-menu")).to_have_count(0)
