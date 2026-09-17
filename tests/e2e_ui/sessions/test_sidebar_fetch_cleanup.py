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

    def sessions(route: Route) -> None:
        params = parse_qs(urlparse(route.request.url).query)
        requests.append(params)
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
    assert all(q["limit"] == ["150"] for q in polls if q["visibility"] == ["mine"])
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
    assert requests[-1]["limit"] == ["200"]
    load_more.click()
    page.wait_for_load_state("networkidle")
    assert mine()[-1]["after"] == [f"{240:032x}"]


def test_mine_filters_a_mixed_visibility_response(
    page: Page, request: pytest.FixtureRequest
) -> None:
    base_url = request.config.getoption("--ui-base-url") or request.getfixturevalue("live_server")
    page.route("**/v1/me", lambda route: route.fulfill(json={"user_id": "alice@example.test"}))
    rows = [
        {
            "id": f"{i:032x}",
            "object": "conversation",
            "title": title,
            "owner": owner,
            "permission_level": None,
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
