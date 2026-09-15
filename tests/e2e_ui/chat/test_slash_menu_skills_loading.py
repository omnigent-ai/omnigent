"""The open slash menu settles directly from a host discovery response."""

from __future__ import annotations

import time
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.chat.test_model_flows_contract import _install_stream_controller, _push_sse


def _mock_starting_runner(page: Page, session_id: str) -> None:
    """Keep liveness offline while a freshly created session is launching."""
    page.route_web_socket("**/v1/sessions/updates*", lambda _: None)
    page.route(
        "**/health?session_ids=*",
        lambda route: route.fulfill(
            json={"sessions": {session_id: {"runner_online": False, "host_online": True}}}
        ),
    )

    def session_list(route: Route) -> None:
        if urlparse(route.request.url).path != "/v1/sessions":
            route.fallback()
            return
        response = route.fetch()
        body = response.json()
        for session in body.get("data", []):
            if session["id"] == session_id:
                session.update(runner_online=False, host_online=True, created_at=time.time())
        route.fulfill(response=response, json=body)

    page.route("**/v1/sessions*", session_list)


@pytest.mark.parametrize("empty", [False, True], ids=["skills-found", "no-skills"])
@pytest.mark.parametrize("phase", ["discovery", "runner-starting", "sandbox-starting"])
def test_open_slash_menu_resolves_skills_from_host(
    page: Page,
    seeded_session: tuple[str, str],
    empty: bool,
    phase: str,
) -> None:
    """A delayed catalog updates an already-open menu, including an empty result."""
    base_url, session_id = seeded_session
    session_path = f"/v1/sessions/{session_id}"
    resolved = False
    skill_requests: list[Route] = []
    if phase != "discovery":
        _mock_starting_runner(page, session_id)

    def snapshot(route: Route) -> None:
        if urlparse(route.request.url).path != session_path or route.request.method != "GET":
            route.fallback()
            return
        response = route.fetch()
        body = response.json()
        body.update(host_id="menu-host", workspace="/workspace")
        if phase != "discovery":
            body["created_at"] = time.time()
            body["runner_online"] = False
        if phase == "sandbox-starting":
            body["sandbox_status"] = None if resolved else {"stage": "provisioning"}
        route.fulfill(response=response, json=body)

    page.route(f"**{session_path}*", snapshot)
    page.route(f"**/v1/skills?session_id={session_id}", lambda route: skill_requests.append(route))
    _install_stream_controller(page, session_id)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("/")
    expect(page.get_by_test_id("slash-menu-item-help")).to_be_visible()
    expect(page.get_by_text("Loading skills…", exact=True)).to_be_visible()
    expect(
        page.get_by_text("Skills unavailable while disconnected.", exact=True)
    ).not_to_be_visible()

    # Filter out built-ins: the loading subsection must keep the menu open.
    composer.fill("/review")
    expect(page.get_by_text("Loading skills…", exact=True)).to_be_visible()
    assert len(skill_requests) == 1
    resolved = True
    skill_requests[0].fulfill(
        json={
            "skills": []
            if empty
            else [{"name": "code-review", "description": "Review the current change"}]
        }
    )
    expect(page.get_by_text("Loading skills…", exact=True)).not_to_be_visible(timeout=3_000)
    expect(composer).to_have_value("/review")
    if empty:
        expect(page.get_by_text("No matching skills", exact=True)).to_be_visible()
    else:
        expect(page.get_by_test_id("slash-menu-item-code-review")).to_be_visible()
        composer.press("Tab")
        expect(composer).to_have_value("/code-review ")


def test_read_only_composer_skips_discovery_until_edit_access(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Only an editable composer requests the session's skill catalog."""
    base_url, session_id = seeded_session
    session_path = f"/v1/sessions/{session_id}"
    read_only = True
    skill_requests: list[str] = []

    def snapshot(route: Route) -> None:
        if urlparse(route.request.url).path != session_path or route.request.method != "GET":
            route.fallback()
            return
        response = route.fetch()
        body = response.json()
        body.update(
            host_id="menu-host",
            workspace="/workspace",
            permission_level=1 if read_only else 2,
        )
        route.fulfill(response=response, json=body)

    def skills(route: Route) -> None:
        skill_requests.append(route.request.url)
        route.fulfill(json={"skills": [{"name": "review", "description": "Review changes"}]})

    page.route(f"**{session_path}*", snapshot)
    page.route("**/v1/skills?*", skills)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_disabled()
    assert skill_requests == []

    read_only = False
    page.reload()
    expect(composer).to_be_enabled()
    composer.fill("/")
    expect(page.get_by_test_id("slash-menu-item-review")).to_be_visible()


def test_slash_menu_stops_loading_when_sandbox_launch_fails(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A failed launch must stop the spinner even within the startup grace."""
    base_url, session_id = seeded_session
    session_path = f"/v1/sessions/{session_id}"
    _mock_starting_runner(page, session_id)

    def snapshot(route: Route) -> None:
        if urlparse(route.request.url).path != session_path or route.request.method != "GET":
            route.fallback()
            return
        response = route.fetch()
        body = response.json()
        body.update(
            created_at=time.time(),
            host_id=None,
            workspace=None,
            sandbox_status={"stage": "provisioning"},
        )
        route.fulfill(response=response, json=body)

    page.route(f"**{session_path}*", snapshot)
    _install_stream_controller(page, session_id)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("/")
    expect(page.get_by_text("Loading skills…", exact=True)).to_be_visible()
    _push_sse(
        page,
        "session.sandbox_status",
        {
            "type": "session.sandbox_status",
            "conversation_id": session_id,
            "stage": "failed",
            "error": "Test sandbox could not start",
        },
    )
    expect(page.get_by_text("Loading skills…", exact=True)).not_to_be_visible(timeout=3_000)
    expect(page.get_by_text("Skills unavailable while disconnected.", exact=True)).to_be_visible()
