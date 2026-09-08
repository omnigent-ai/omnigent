"""The Canvas page shows top-level sessions as cards, one canvas per project."""

from __future__ import annotations

import json
import re
from pathlib import Path

from playwright.sync_api import Page, Route, expect


def _stub_server_info(page: Page, *, canvas: bool) -> None:
    """Advertise one deterministic ``canvas`` release-feature value."""
    body = json.dumps(
        {
            "accounts_enabled": False,
            "single_user": True,
            "login_url": None,
            "needs_setup": False,
            "features": {"canvas": canvas, "usage_page": False, "harness_install": False},
            "harness_install_enabled": False,
            "installable_harnesses": [],
        }
    )
    page.route(
        "**/v1/info",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


def _session(
    session_id: str, title: str, updated_at: int, **overrides: object
) -> dict[str, object]:
    return {
        "id": session_id,
        "object": "conversation",
        "title": title,
        "status": "idle",
        "created_at": 1,
        "updated_at": updated_at,
        "labels": {},
        "permission_level": None,
        "workspace": "/workspace/canvas",
        "git_branch": None,
        "project_id": None,
        "archived": False,
        "parent_session_id": None,
        **overrides,
    }


def _serve_list(sessions: list[dict[str, object]]):
    def serve(route: Route) -> None:
        route.fulfill(
            json={
                "object": "list",
                "data": sessions,
                "first_id": sessions[0]["id"] if sessions else None,
                "last_id": None,
                "has_more": False,
            }
        )

    return serve


def test_canvas_page_is_absent_while_the_feature_is_off(page: Page, live_server: str) -> None:
    """A direct deep link cannot bypass the default-off navigation gate."""
    _stub_server_info(page, canvas=False)

    page.goto(f"{live_server}/canvas")

    expect(page.get_by_role("heading", name="Page not found")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("canvas-nav")).to_have_count(0)


def test_canvas_page_groups_sessions_by_project_and_opens_them(
    page: Page,
    live_server: str,
    tmp_path: Path,
) -> None:
    """Main holds unfiled sessions; a project tab holds its own; a card opens its session."""
    projects = [{"id": "project-release", "name": "Release", "icon": None}]
    sessions = [
        _session(f"main-{index}", f"Main session {index}", 20 - index) for index in range(3)
    ]
    sessions.append(
        _session("project-session", "Review the release", 1, project_id="project-release")
    )
    _stub_server_info(page, canvas=True)
    page.route("**/v1/sessions?*", _serve_list(sessions))
    page.route("**/v1/sessions/projects", lambda route: route.fulfill(json=projects))

    page.goto(live_server)
    expect(page.get_by_text("Main session 0", exact=True).first).to_be_visible()
    page.get_by_test_id("canvas-nav").click()

    expect(page).to_have_url(re.compile(r"/canvas$"))
    expect(page.get_by_role("heading", name="Canvas", exact=True)).to_be_visible()
    expect(page.get_by_text("3 sessions", exact=True)).to_be_visible()
    expect(page.get_by_role("status", name="Loading sessions")).to_have_count(0)
    cards = page.get_by_test_id("session-card")
    expect(cards).to_have_count(3)
    expect(page.get_by_test_id("canvas-flow")).to_contain_text("Main session 2")
    expect(page.get_by_test_id("canvas-flow")).not_to_contain_text("Review the release")
    page.screenshot(path=str(tmp_path / "canvas-main.png"))

    page.get_by_role("tab", name="Release", exact=True).click()
    expect(page.get_by_text("1 session", exact=True)).to_be_visible()
    expect(cards).to_have_count(1)
    expect(cards).to_contain_text("Review the release")
    page.screenshot(path=str(tmp_path / "canvas-project.png"))

    # The selected canvas lives in the URL, so a reload lands on the same tab.
    expect(page).to_have_url(re.compile(r"/canvas\?canvas=project-release$"))
    page.reload()
    expect(page.get_by_role("tab", name="Release", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    expect(page.get_by_text("1 session", exact=True)).to_be_visible()
    expect(cards).to_have_count(1)

    cards.dblclick()
    expect(page).to_have_url(re.compile(r"/c/project-session$"))


def test_canvas_page_remembers_a_dragged_card_across_reloads(
    page: Page,
    live_server: str,
) -> None:
    """A card dropped elsewhere keeps its spot after a reload; Reset layout regrids it."""
    sessions = [_session("only", "Only session", 1)]
    _stub_server_info(page, canvas=True)
    page.route("**/v1/sessions?*", _serve_list(sessions))
    page.route("**/v1/sessions/projects", lambda route: route.fulfill(json=[]))

    page.goto(f"{live_server}/canvas")
    card = page.get_by_test_id("session-card")
    expect(card).to_be_visible()
    before = card.bounding_box()
    assert before is not None

    page.mouse.move(before["x"] + 20, before["y"] + 20)
    page.mouse.down()
    page.mouse.move(before["x"] + 140, before["y"] + 120, steps=8)
    page.mouse.up()
    page.wait_for_function(
        "() => Object.keys(JSON.parse(localStorage.getItem("
        "`omnigent:canvas-layout:${location.origin}`) ?? '{}').positions ?? {}).length === 1"
    )
    moved = card.bounding_box()
    assert moved is not None
    assert abs(moved["x"] - before["x"]) > 60

    page.reload()
    expect(page.get_by_test_id("session-card")).to_be_visible()
    restored = page.get_by_test_id("session-card").bounding_box()
    assert restored is not None
    assert abs(restored["x"] - moved["x"]) < 2 and abs(restored["y"] - moved["y"]) < 2

    page.get_by_role("button", name="Reset layout").click()
    page.wait_for_function(
        "() => !(JSON.parse(localStorage.getItem("
        "`omnigent:canvas-layout:${location.origin}`) ?? '{}').positions ?? {}).only"
    )
