"""Cold-load request ownership and bounded sidebar pagination in the built UI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Route, expect


def test_scope_requests_and_bounded_automatic_pagination(
    page: Page, request: pytest.FixtureRequest
) -> None:
    base_url = request.config.getoption("--ui-base-url") or request.getfixturevalue("live_server")
    requests: list[dict[str, list[str]]] = []
    hold_refresh = False
    held_refreshes: list[Route] = []

    def sessions(route: Route) -> None:
        params = parse_qs(urlparse(route.request.url).query)
        requests.append(params)
        if (
            hold_refresh
            and params.get("visibility") == ["mine"]
            and "pinned" not in params
            and "after" not in params
        ):
            held_refreshes.append(route)
            return
        respond(route)

    def respond(route: Route) -> None:
        params = parse_qs(urlparse(route.request.url).query)
        rows = []
        has_more = params.get("visibility") == ["mine"] and "pinned" not in params
        if has_more:
            after = int(params.get("after", ["0"])[0], 16)
            rows = [
                {
                    "id": f"{after + i:032x}",
                    "object": "conversation",
                    "title": f"Filed session {after + i}",
                    "created_at": 1,
                    "updated_at": 10000 - after - i,
                    "permission_level": None,
                    "labels": {"omni_project": "Filed sessions"},
                }
                for i in range(1, int(params.get("limit", ["30"])[0]) + 1)
            ]
        route.fulfill(
            json={
                "data": rows,
                "has_more": has_more,
                "first_id": rows[0]["id"] if rows else None,
                "last_id": rows[-1]["id"] if rows else None,
            }
        )

    page.route_web_socket("**/v1/sessions/updates*", lambda _socket: None)
    page.route("**/v1/sessions?*", sessions)
    page.route(
        "**/v1/sessions/projects",
        lambda route: route.fulfill(json=[{"id": None, "name": "Filed sessions"}]),
    )
    page.clock.install()
    page.goto(base_url)
    load_more = page.get_by_role("button", name="Load more", exact=True)
    expect(load_more).to_be_visible()
    page.wait_for_load_state("networkidle")

    def mine():
        return [q for q in requests if q.get("visibility") == ["mine"] and "pinned" not in q]

    assert len(mine()) == 4  # Initial page and three automatic loads.
    assert sum(q.get("visibility") == ["shared"] and "pinned" not in q for q in requests) == 1
    assert sum("pinned" in q for q in requests) == 2
    assert all(q.get("limit") == ["30"] and "visibility" in q for q in requests)
    assert all(q.get("kind") != ["any"] for q in requests)
    load_more.click()
    page.wait_for_load_state("networkidle")
    assert len(mine()) == 5
    expect(load_more).to_be_visible()

    # Five loaded pages must still cost one request per scope timer tick.
    page.clock.pause_at(datetime.now(timezone.utc) + timedelta(seconds=5))
    start_count = len(requests)
    for _ in range(3):
        page.clock.fast_forward(60_000)
        page.wait_for_load_state("networkidle")
    polls = requests[start_count:]
    assert sum(q.get("visibility") == ["mine"] for q in polls) == 3
    assert sum(q.get("visibility") == ["shared"] for q in polls) == 1
    assert all("after" not in q and "pinned" not in q for q in polls)
    assert all(q["limit"] == ["100"] for q in polls if q["visibility"] == ["mine"])
    load_more.click()
    page.wait_for_load_state("networkidle")
    assert mine()[-1]["after"] == [f"{150:032x}"]

    for _ in range(2):
        load_more.click()
        page.wait_for_load_state("networkidle")
    page.clock.run_for(100)
    start_count = len(requests)
    page.clock.fast_forward(60_000)
    page.wait_for_load_state("networkidle")
    assert len(requests[start_count:]) == 1
    assert requests[-1]["limit"] == ["100"]
    load_more.click()
    page.wait_for_load_state("networkidle")
    assert mine()[-1]["after"] == [f"{240:032x}"]

    # A manual click during refresh queues one page without consuming another click.
    hold_refresh = True
    start_count = len(mine())
    page.clock.run_for(100)
    with page.expect_request(
        lambda req: (
            "/v1/sessions?" in req.url
            and parse_qs(urlparse(req.url).query).get("visibility") == ["mine"]
            and "after" not in parse_qs(urlparse(req.url).query)
        )
    ):
        page.clock.fast_forward(60_000)
    expect(load_more).to_be_visible()
    expect(load_more).to_be_enabled()
    assert len(held_refreshes) == 1
    load_more.click()
    page.clock.run_for(100)
    expect(page.get_by_role("button", name="Loading…", exact=True)).to_be_visible()
    assert len(mine()) == start_count + 1
    hold_refresh = False
    respond(held_refreshes.pop())
    page.wait_for_load_state("networkidle")
    page.clock.run_for(100)
    assert len(mine()) == start_count + 2
    assert mine()[-1]["after"] == [f"{270:032x}"]
    assert mine()[-1]["limit"] == ["30"]
    page.clock.resume()
    expect(load_more).to_be_visible()


@pytest.mark.parametrize("permission_level", [None, 4], ids=["ownership-only", "admin"])
def test_mine_filters_a_mixed_visibility_response(
    page: Page, request: pytest.FixtureRequest, permission_level: int | None
) -> None:
    base_url = request.config.getoption("--ui-base-url") or request.getfixturevalue("live_server")
    page.route("**/v1/me", lambda route: route.fulfill(json={"user_id": "alice@example.test"}))
    rows = [
        {
            "id": f"{i:032x}",
            "object": "conversation",
            "title": title,
            "owner": owner,
            "permission_level": permission_level,
            "created_at": 1,
            "updated_at": 100 - i,
            "labels": {},
        }
        for i, (title, owner) in enumerate(
            [
                ("Owned from mixed response", "alice@example.test"),
                ("Shared from mixed response", "bob@example.test"),
            ],
            start=1,
        )
    ]

    def mixed_sessions(route: Route) -> None:
        params = parse_qs(urlparse(route.request.url).query)
        data = [] if "pinned" in params else rows
        route.fulfill(
            json={
                "data": data,
                "has_more": False,
                "first_id": data[0]["id"] if data else None,
                "last_id": data[-1]["id"] if data else None,
            }
        )

    page.route_web_socket("**/v1/sessions/updates*", lambda _socket: None)
    page.route("**/v1/sessions?*", mixed_sessions)
    page.goto(base_url)
    expect(page.get_by_text("Owned from mixed response", exact=True)).to_be_visible()
    expect(page.get_by_text("Shared from mixed response", exact=True)).to_have_count(0)
    page.get_by_test_id("session-filter").click()
    page.get_by_test_id("session-filter-all").click()
    expect(page.get_by_text("Owned from mixed response", exact=True)).to_be_visible()
    expect(page.get_by_text("Shared from mixed response", exact=True)).to_be_visible()


@pytest.mark.parametrize("fail_shared", [False, True], ids=["loaded", "retry"])
def test_shared_loading_keeps_pins_and_projects_mounted(
    page: Page, request: pytest.FixtureRequest, fail_shared: bool
) -> None:
    base_url = request.config.getoption("--ui-base-url") or request.getfixturevalue("live_server")
    held: list[Route] = []
    hold_shared = True
    project_requests = 0

    def row(number: int, title: str, labels: dict[str, str], level: int = 4):
        return {
            "id": f"{number:032x}",
            "object": "conversation",
            "title": title,
            "labels": labels,
            "owner": "alice@example.test" if level == 4 else "bob@example.test",
            "permission_level": level,
            "created_at": 1,
            "updated_at": 100 - number,
        }

    pinned = row(1, "Stable pinned session", {"omnigent.pinned": "1"})
    filed = row(2, "Stable project session", {"omni_project": "Stable project"})
    shared = row(3, "New shared session", {}, 1)

    def respond(route: Route) -> None:
        nonlocal project_requests
        params = parse_qs(urlparse(route.request.url).query)
        if "project" in params:
            project_requests += 1
            rows = [filed]
        elif "pinned" in params:
            rows = [] if params.get("visibility") == ["shared"] else [pinned]
        elif params.get("visibility") == ["shared"]:
            if hold_shared:
                held.append(route)
                return
            if fail_shared:
                route.fulfill(status=503, body="Shared unavailable")
                return
            rows = [shared]
        else:
            rows = [pinned, filed]
        route.fulfill(json={"data": rows, "has_more": False})

    page.route("**/v1/me", lambda route: route.fulfill(json={"user_id": "alice@example.test"}))
    page.route_web_socket("**/v1/sessions/updates*", lambda _socket: None)
    page.route("**/v1/sessions?*", respond)
    page.route(
        "**/v1/sessions/projects",
        lambda route: route.fulfill(json=[{"id": None, "name": "Stable project"}]),
    )
    page.goto(base_url, wait_until="domcontentloaded")
    pin = page.get_by_text("Stable pinned session", exact=True)
    expect(pin).to_be_visible()
    page.get_by_role("button", name="Stable project", exact=True).click()
    folder_row = page.get_by_text("Stable project session", exact=True)
    expect(folder_row).to_be_visible()
    pin_element = pin.element_handle()
    folder_element = folder_row.element_handle()
    assert pin_element is not None and folder_element is not None

    # All needs the pending Shared cache even on a loopback-only test server.
    page.get_by_test_id("session-filter").click()
    page.get_by_test_id("session-filter-all").click()
    sessions = page.locator("section").filter(
        has=page.get_by_role("button", name="Sessions", exact=True)
    )
    expect(sessions.get_by_role("status")).to_have_text("Loading…")
    expect(pin).to_be_visible()
    expect(folder_row).to_be_visible()
    expect(page.get_by_test_id("session-filter")).to_be_visible()
    assert pin_element.evaluate("el => el.isConnected")
    assert folder_element.evaluate("el => el.isConnected")

    hold_shared = False
    assert len(held) == 1
    respond(held.pop())
    if fail_shared:
        expect(sessions.get_by_role("button", name="Retry")).to_be_visible(timeout=15000)
        expect(pin).to_be_visible()
        expect(folder_row).to_be_visible()
        fail_shared = False
        sessions.get_by_role("button", name="Retry").click()
    expect(page.get_by_text("New shared session", exact=True)).to_be_visible()
    expect(sessions.get_by_role("status")).to_have_count(0)
    assert pin_element.evaluate("el => el.isConnected")
    assert folder_element.evaluate("el => el.isConnected")
    assert project_requests == 1


def test_templates_load_before_mine_and_discovery_reuses_its_request(
    page: Page, request: pytest.FixtureRequest
) -> None:
    base_url = request.config.getoption("--ui-base-url") or request.getfixturevalue("live_server")
    held_mine: list[Route] = []
    agent_requests: list[str] = []

    def sessions(route: Route) -> None:
        params = parse_qs(urlparse(route.request.url).query)
        if params.get("visibility") == ["mine"] and "pinned" not in params:
            held_mine.append(route)
        else:
            route.fulfill(json={"data": [], "has_more": False})

    def agents(route: Route) -> None:
        agent_requests.append(route.request.url)
        route.fulfill(
            json={
                "data": [
                    {
                        "id": "ag_immediate",
                        "name": "immediate-template",
                        "builtin": False,
                        "created_at": 1,
                    }
                ],
                "has_more": False,
            }
        )

    page.route_web_socket("**/v1/sessions/updates*", lambda _socket: None)
    page.route("**/v1/sessions?*", sessions)
    page.route("**/v1/agents", agents)
    page.route("**/v1/agents?*", agents)
    page.route(
        "**/v1/hosts",
        lambda route: route.fulfill(
            json={"hosts": [{"host_id": "test-host", "name": "Test host", "status": "online"}]}
        ),
    )
    page.goto(base_url, wait_until="domcontentloaded")
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    expect(picker).to_contain_text("Immediate-template")
    expect(picker).to_be_enabled()
    assert len(held_mine) == 1
    held_mine[0].fulfill(
        json={
            "data": [
                {
                    "id": "recent_mine",
                    "agent_id": "ag_recent",
                    "agent_name": "recent-custom",
                    "title": "Recent owned session",
                    "created_at": 2,
                    "updated_at": 2,
                    "permission_level": 4,
                    "labels": {},
                }
            ],
            "has_more": False,
        }
    )
    page.wait_for_load_state("networkidle")
    picker.click()
    page.get_by_test_id("new-chat-landing-custom-agents").click()
    expect(page.get_by_test_id("new-chat-landing-agent-ag_recent")).to_be_visible()
    expect(page.get_by_test_id("new-chat-landing-agent-ag_immediate")).to_be_visible()
    assert len(held_mine) == 1
    assert len(agent_requests) == 1
    assert not urlparse(agent_requests[0]).query


def test_archived_refreshes_on_entry_without_polling(
    page: Page, request: pytest.FixtureRequest
) -> None:
    base_url = request.config.getoption("--ui-base-url") or request.getfixturevalue("live_server")
    archived_requests: list[str] = []
    title = "Original archived title"

    def sessions(route: Route) -> None:
        params = parse_qs(urlparse(route.request.url).query)
        data = []
        if params.get("visibility") == ["archived"]:
            archived_requests.append(route.request.url)
            data = [
                {
                    "id": "archived-row",
                    "title": title,
                    "created_at": 1,
                    "updated_at": 1,
                    "archived": True,
                    "labels": {},
                    "permission_level": 4,
                }
            ]
        route.fulfill(json={"data": data, "has_more": False})

    def select(scope: str) -> None:
        page.get_by_test_id("session-filter").click()
        option = page.get_by_test_id(f"session-filter-{scope}")
        option.click()
        expect(option).to_have_count(0)
        page.wait_for_load_state("networkidle")

    page.route_web_socket("**/v1/sessions/updates*", lambda _socket: None)
    page.route("**/v1/sessions?*", sessions)
    page.clock.install()
    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    assert len(archived_requests) == 0
    select("archived")
    expect(page.get_by_text(title, exact=True)).to_be_visible()
    assert len(archived_requests) == 1
    before_poll_window = len(archived_requests)
    page.clock.fast_forward(180_000)
    page.wait_for_load_state("networkidle")
    assert len(archived_requests) == before_poll_window
    select("mine")
    expect(page.get_by_text(title, exact=True)).to_have_count(0)
    title = "Archived title changed remotely"
    select("archived")
    expect(page.get_by_text(title, exact=True)).to_be_visible()
    assert len(archived_requests) == 2
    before_poll_window = len(archived_requests)
    page.clock.fast_forward(180_000)
    page.wait_for_load_state("networkidle")
    assert len(archived_requests) == before_poll_window


def test_directory_warning_reuses_loaded_mine_sessions(
    page: Page, request: pytest.FixtureRequest
) -> None:
    base_url = request.config.getoption("--ui-base-url") or request.getfixturevalue("live_server")
    list_requests: list[dict[str, list[str]]] = []
    session_id = "00000000000000000000000000000001"

    def sessions(route: Route) -> None:
        params = parse_qs(urlparse(route.request.url).query)
        list_requests.append(params)
        rows = []
        if params.get("visibility") == ["mine"] and "pinned" not in params:
            rows = [
                {
                    "id": session_id,
                    "object": "conversation",
                    "title": "Cached directory session",
                    "host_id": "directory-host",
                    "workspace": "/repo",
                    "runner_online": True,
                    "permission_level": 4,
                    "created_at": 1,
                    "updated_at": 1,
                    "labels": {},
                }
            ]
        route.fulfill(json={"data": rows, "has_more": False})

    page.route_web_socket("**/v1/sessions/updates*", lambda _socket: None)
    page.route("**/v1/sessions?*", sessions)
    page.route(
        "**/v1/hosts",
        lambda route: route.fulfill(
            json={
                "hosts": [
                    {
                        "host_id": "directory-host",
                        "name": "Directory host",
                        "status": "online",
                    }
                ]
            }
        ),
    )
    page.route(
        "**/v1/hosts/directory-host/filesystem**",
        lambda route: route.fulfill(
            json={
                "data": [{"name": "src", "path": "/repo/src", "type": "directory"}],
                "has_more": False,
            }
        ),
    )
    page.route(
        "**/v1/hosts/directory-host/worktrees?*", lambda route: route.fulfill(json={"data": []})
    )
    page.route(
        "**/health?*",
        lambda route: route.fulfill(
            json={
                "sessions": {
                    session_id: {"runner_online": True, "host_online": True},
                }
            }
        ),
    )
    page.goto(base_url)
    expect(page.get_by_text("Cached directory session", exact=True)).to_be_visible()
    expect(page.get_by_test_id("new-chat-landing-host-chip")).to_be_enabled()
    page.wait_for_load_state("networkidle")
    initial_requests = len(list_requests)
    assert all("visibility" in params for params in list_requests)
    page.get_by_test_id("new-chat-landing-workspace-chip").click()
    page.get_by_test_id("new-chat-landing-workspace-open-folder").click()
    expect(page.get_by_test_id("workspace-picker-conflict")).to_contain_text("1 other agent is")
    page.wait_for_load_state("networkidle")
    assert len(list_requests) == initial_requests
    page.get_by_test_id("workspace-picker-close").click()
    page.get_by_test_id("new-chat-landing-workspace-chip").click()
    page.get_by_test_id("new-chat-landing-workspace-open-folder").click()
    expect(page.get_by_test_id("workspace-picker-conflict")).to_be_visible()
    page.wait_for_load_state("networkidle")
    assert len(list_requests) == initial_requests


@pytest.mark.parametrize("operation", ["archive", "delete"])
def test_opaque_cursor_survives_removing_last_visible_session(
    page: Page, request: pytest.FixtureRequest, operation: str
) -> None:
    base_url = request.config.getoption("--ui-base-url") or request.getfixturevalue("live_server")
    target = "00000000000000000000000000000abc01"
    afters: list[str | None] = []
    tokens = [f"opaque/+page={i}==" for i in range(4)]

    def sessions(route: Route) -> None:
        params = parse_qs(urlparse(route.request.url).query)
        if params.get("visibility") != ["mine"] or "pinned" in params:
            route.fulfill(json={"data": [], "has_more": False})
            return
        after = params.get("after", [None])[0]
        afters.append(after)
        index = 0 if after is None else tokens.index(after) + 1
        rows = []
        if index >= 3:
            rows = [
                {
                    "id": target if index == 3 else "00000000000000000000000000000abc02",
                    "object": "conversation",
                    "title": "Remove this session" if index == 3 else "Next opaque page",
                    "permission_level": 4,
                    "created_at": 1,
                    "updated_at": 1,
                    "labels": {},
                }
            ]
        route.fulfill(
            json={
                "data": rows,
                "last_id": tokens[index] if index < 4 else None,
                "has_more": index < 4,
            }
        )

    page.route_web_socket("**/v1/sessions/updates*", lambda _socket: None)
    page.route("**/v1/sessions?*", sessions)
    page.route(
        f"**/v1/sessions/{target}/events", lambda route: route.fulfill(json={"queued": True})
    )
    page.route(
        f"**/v1/sessions/{target}",
        lambda route: route.fulfill(
            json={
                "id": target,
                "archived": operation == "archive",
                "deleted": operation == "delete",
            }
        ),
    )
    page.goto(base_url)
    row = page.locator("li").filter(has=page.get_by_text("Remove this session", exact=True))
    expect(row).to_be_visible()
    page.wait_for_load_state("networkidle")
    assert afters == [None, *tokens[:3]]
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id(f"{operation}-conversation").click()
    if operation == "delete":
        page.get_by_role("dialog").get_by_role("button", name="Delete", exact=True).click()
    expect(row).to_have_count(0)
    page.get_by_role("button", name="Load more", exact=True).click()
    expect(page.get_by_text("Next opaque page", exact=True)).to_be_visible()
    assert afters[-1] == tokens[-1]


def test_admin_identity_resolves_after_shared_sessions_and_pins(
    page: Page, request: pytest.FixtureRequest
) -> None:
    base_url = request.config.getoption("--ui-base-url") or request.getfixturevalue("live_server")
    identity_requests: list[Route] = []
    list_requests: list[str] = []
    list_responses: list[str] = []

    def sessions(route: Route) -> None:
        list_requests.append(route.request.url)
        params = parse_qs(urlparse(route.request.url).query)
        shared = params.get("visibility") == ["shared"]
        pinned = "pinned" in params
        number = 1 + int(shared) + 2 * int(pinned)
        row = {
            "id": f"{number:032x}",
            "object": "conversation",
            "title": ("Shared" if shared else "Owned")
            + (" admin pin" if pinned else " admin session"),
            "owner": "bob@example.test" if shared else "alice@example.test",
            "permission_level": 4,
            "created_at": 1,
            "updated_at": 1,
            "labels": {"omnigent.pinned": "1"} if pinned else {},
        }
        route.fulfill(json={"data": [row], "has_more": False})

    page.route("**/v1/me", lambda route: identity_requests.append(route))
    page.route("**/v1/sessions?*", sessions)
    page.route_web_socket("**/v1/sessions/updates*", lambda _socket: None)
    page.on(
        "response",
        lambda response: (
            list_responses.append(response.url) if "/v1/sessions?" in response.url else None
        ),
    )
    page.add_init_script("localStorage.setItem('omnigent:session-filter', 'all')")
    page.clock.install()
    with page.expect_response(
        lambda response: (
            "/v1/sessions?" in response.url
            and "visibility=shared" in response.url
            and "pinned=true" in response.url
        )
    ):
        page.goto(base_url, wait_until="domcontentloaded")
    expect(page.get_by_text("Loading sessions…", exact=True)).to_be_visible()
    for title in [
        "Owned admin session",
        "Owned admin pin",
        "Shared admin session",
        "Shared admin pin",
    ]:
        expect(page.get_by_text(title, exact=True)).to_have_count(0)
    assert len(list_responses) == 4
    assert len(identity_requests) == 1
    before_identity = len(list_requests)
    identity_requests[0].fulfill(json={"user_id": "alice@example.test", "is_admin": True})
    expect(page.get_by_text("Shared admin session", exact=True)).to_be_visible()
    expect(page.get_by_text("Shared admin pin", exact=True)).to_be_visible()
    expect(page.get_by_text("Owned admin session", exact=True)).to_be_visible()
    expect(page.get_by_text("Owned admin pin", exact=True)).to_be_visible()
    page.wait_for_load_state("networkidle")
    assert len(list_requests) == before_identity
