"""Sidebar view options and the session tooltip's GitHub rows, with ``sidebar_grouping_ui`` on.

The session list and GitHub resource are stubbed with ``page.route`` so the
grouping is deterministic; everything else comes from the live server.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from time import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.sync_api import Locator, Page, Route, WebSocketRoute, expect

from tests._helpers.polling import poll_until
from tests.e2e_ui.collaboration._multi_user_server import create_user_session

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


@pytest.mark.parametrize("width", [1440, 390], ids=["desktop", "narrow"])
def test_harness_grouping_persists_across_reload(
    page: Page, stubbed_sidebar: str, monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    """Both sidebar layouts group stubbed native/SDK rows and restore Default."""
    for row, harness in zip(_ROWS, ["codex-native", "claude-native", "codex"], strict=True):
        monkeypatch.setitem(row, "harness", harness)
    page.set_viewport_size({"width": width, "height": 900})
    page.reload()
    if width < 768:
        page.get_by_role("button", name="Open sidebar", exact=True).click()
    _open_view_submenu(page, "session-grouping-menu")
    page.get_by_role("menuitemradio", name="Harness", exact=True).click()
    page.keyboard.press("Escape")
    claude = page.locator("section").filter(has=_section_header(page, "Claude Code"))
    codex = page.locator("section").filter(has=_section_header(page, "Codex"))
    expect(claude.locator('a[href^="/c/"]')).to_have_text(["Awaiting session"])
    expect(codex.locator('a[href^="/c/"]')).to_have_text(["Running session", "Idle session"])
    expect(claude.get_by_test_id("session-filter")).to_be_visible()
    expect(_section_header(page, "Projects")).to_have_count(0)
    _section_header(page, "Codex").click()
    expect(codex.locator('a[href^="/c/"]')).to_have_count(0)
    _section_header(page, "Codex").click()

    page.reload()
    if width < 768:
        page.get_by_role("button", name="Open sidebar", exact=True).click()
    expect(codex.locator('a[href^="/c/"]')).to_have_text(["Running session", "Idle session"])
    _open_view_submenu(page, "session-grouping-menu")
    expect(page.get_by_role("menuitemradio", name="Harness", exact=True)).to_be_checked()
    page.get_by_role("menuitemradio", name="Default", exact=True).click()
    page.keyboard.press("Escape")
    expect(_section_header(page, "Projects")).to_be_visible()
    sessions = page.locator("section").filter(has=_section_header(page, "Sessions"))
    expect(sessions.locator('a[href^="/c/"]')).to_have_text(
        ["Running session", "Awaiting session", "Idle session"]
    )


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


@dataclass(frozen=True)
class _NamedSessions:
    base_url: str
    project: str
    # Session ids keyed by title; the untitled session is keyed by "".
    folder: dict[str, str]
    unfiled: dict[str, str]


def _seed_session(base_url: str, title: str, project: str | None) -> str:
    metadata: dict[str, object] = {"title": title} if title else {}
    if project:
        metadata["labels"] = {"omni_project": project}
    return create_user_session(base_url, headers={}, metadata=metadata)


_UNFILED_TITLES = ["!mango plan", "!Zebra notes", "!lemon tasks"]
_UNFILED_A_TO_Z = ["!lemon tasks", "!mango plan", "!Zebra notes"]


@pytest.fixture
def named_sessions(request: pytest.FixtureRequest) -> Iterator[_NamedSessions]:
    """Seed live sessions with mixed-case titles in a folder and unfiled, newest last."""
    base_url = request.config.getoption("--ui-base-url") or request.getfixturevalue("live_server")
    project = f"Sorting {uuid.uuid4().hex[:4]}"
    folder: dict[str, str] = {}
    unfiled: dict[str, str] = {}
    try:
        # Created in this order, so Updated lists each group roughly reversed.
        for title in ["date list", "Banana bread", "", "kiwi trip", "apple pie", "Cherry jam"]:
            folder[title] = _seed_session(base_url, title, project)
        # Unfiled rows share the server with other tests; "!" keeps them on
        # the first A to Z page while "Z" vs "l" still checks case folding.
        for title in _UNFILED_TITLES:
            unfiled[title] = _seed_session(base_url, title, None)
        yield _NamedSessions(base_url, project, folder, unfiled)
    finally:
        for session_id in [*folder.values(), *unfiled.values()]:
            httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)


def _pause_for_recording(page: Page, milliseconds: int) -> None:
    """Hold a step on screen long enough to read, only while recording a demo."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(milliseconds)


def _listed_titles(page: Page, ids: dict[str, str]) -> list[str]:
    """Titles of the seeded sessions in their on-screen order."""
    titles = {session_id: title for title, session_id in ids.items()}
    hrefs = page.locator('aside a[href^="/c/"]').evaluate_all(
        "links => links.map((link) => link.getAttribute('href').slice(3))"
    )
    return [titles[href] for href in hrefs if href in titles]


def _wait_for_order(page: Page, ids: dict[str, str], expected: list[str]) -> None:
    """Wait until the seeded sessions in *ids* are listed as *expected*, leading."""
    try:
        poll_until(lambda: _listed_titles(page, ids)[: len(expected)] == expected, timeout_s=15)
    except AssertionError:
        assert _listed_titles(page, ids) == expected
        raise


_NAME_ORDER_VIEWPORT = {"width": 1280, "height": 800}


@pytest.mark.browser_context_args(
    viewport=_NAME_ORDER_VIEWPORT, record_video_size=_NAME_ORDER_VIEWPORT
)
def test_name_ordering_lists_live_sessions_a_to_z_and_persists(
    request: pytest.FixtureRequest, named_sessions: _NamedSessions
) -> None:
    """Name orders the server's sessions and folders A to Z, untitled last, across reloads."""
    # Request the page after seeding so a recording starts at the user's first view.
    page: Page = request.getfixturevalue("page")
    requests: list[str] = []
    page.on(
        "request",
        lambda req: requests.append(req.url) if "/v1/sessions?" in req.url else None,
    )
    _enable_grouping_ui(page)
    page.goto(named_sessions.base_url)
    folder_header = page.locator(f'button[data-project-order-name="{named_sessions.project}"]')
    expect(folder_header).to_be_visible(timeout=30_000)
    folder_header.click()
    expect(page.get_by_test_id("project-show-more")).to_be_visible()
    poll_until(lambda: len(_listed_titles(page, named_sessions.unfiled)) == 3, timeout_s=15)
    _pause_for_recording(page, 1_000)

    _open_view_submenu(page, "session-ordering-menu")
    _pause_for_recording(page, 700)
    page.get_by_role("menuitemradio", name="Name (A to Z)", exact=True).click()
    page.keyboard.press("Escape")
    _wait_for_order(
        page,
        named_sessions.folder,
        ["apple pie", "Banana bread", "Cherry jam", "date list", "kiwi trip"],
    )
    _wait_for_order(page, named_sessions.unfiled, _UNFILED_A_TO_Z)
    assert any(
        parse_qs(urlparse(url).query).get("sort_by") == ["title"]
        and parse_qs(urlparse(url).query).get("order") == ["asc"]
        for url in requests
    )
    _pause_for_recording(page, 1_500)
    page.get_by_test_id("project-show-more").click()
    _wait_for_order(
        page,
        named_sessions.folder,
        ["apple pie", "Banana bread", "Cherry jam", "date list", "kiwi trip", ""],
    )
    untitled = page.locator(f'aside a[href="/c/{named_sessions.folder[""]}"]')
    expect(untitled).to_have_text("New session")
    _pause_for_recording(page, 1_500)

    page.reload()
    expect(folder_header).to_be_visible(timeout=30_000)
    _wait_for_order(page, named_sessions.unfiled, _UNFILED_A_TO_Z)
    _wait_for_order(
        page,
        named_sessions.folder,
        ["apple pie", "Banana bread", "Cherry jam", "date list", "kiwi trip"],
    )
    _open_view_submenu(page, "session-ordering-menu")
    expect(page.get_by_role("menuitemradio", name="Name (A to Z)", exact=True)).to_be_checked()
    _pause_for_recording(page, 2_000)
