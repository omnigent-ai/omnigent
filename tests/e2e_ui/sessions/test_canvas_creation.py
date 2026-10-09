"""Canvas navigation stays consistent while creating sessions and expanding the rail."""

from __future__ import annotations

import contextlib
import re
import uuid
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests._helpers.session import bind_session_runner
from tests.e2e_ui.conftest import configure_mock_llm
from tests.e2e_ui.sessions.test_canvas_page import _stub_server_info
from tests.e2e_ui.sessions.test_canvas_workspace import canvas_project as canvas_project


@pytest.mark.parametrize("usage", [False, True])
def test_canvas_rail_matches_expanded_navigation(
    page: Page, canvas_project: tuple[str, str, str, str], usage: bool
) -> None:
    base_url, _first, _second, project_id = canvas_project
    page.set_viewport_size({"width": 1440, "height": 900})
    _stub_server_info(page, canvas=True, usage=usage)
    page.goto(f"{base_url}/canvas")
    project = page.get_by_role("tab", name=re.compile("Canvas review"))
    project.click()
    rail = page.get_by_role("navigation", name="Collapsed sidebar")
    expect(rail).to_be_visible()
    collapsed = rail.locator("button, a").evaluate_all(
        "elements => elements.map(element => ({"
        "label: element.getAttribute('aria-label') || element.textContent.trim(),"
        "href: element.getAttribute('href'),"
        "icon: element.querySelector('svg')?.innerHTML}))"
    )
    rail.get_by_role("button", name="Expand sidebar", exact=True).click()
    primary = page.get_by_test_id("sidebar-primary-nav")
    expect(primary).to_be_visible()
    expanded = primary.locator("a").evaluate_all(
        "elements => elements.map(element => ({"
        "label: [...element.childNodes].filter(node => node.nodeType === Node.TEXT_NODE)"
        ".map(node => node.textContent).join('').trim(),"
        "href: element.getAttribute('href'),"
        "icon: element.querySelector('svg')?.innerHTML}))"
    )
    header = (
        page.get_by_test_id("sidebar-header-actions")
        .locator("button, a")
        .evaluate_all("elements => elements.map(element => element.getAttribute('aria-label'))")
    )
    header = ["Expand sidebar"] + [label for label in header if label != "Close sidebar"]
    primary.get_by_role("link", name="Canvas", exact=True).click()
    expect(page).to_have_url(re.compile(rf"/canvas\?canvas={project_id}$"))
    expect(project).to_have_attribute("aria-selected", "true")
    page.reload()
    expect(project).to_have_attribute("aria-selected", "true")
    expect(page).to_have_url(re.compile(rf"/canvas\?canvas={project_id}$"))
    assert [item["label"] for item in collapsed] == header + [item["label"] for item in expanded]
    assert collapsed[len(header) :] == expanded
    assert ("Usage" in [item["label"] for item in expanded]) is usage


@pytest.mark.parametrize("project_canvas", [False, True], ids=["main", "project"])
def test_stale_canvas_creation_url_returns_to_its_board(
    page: Page, canvas_project: tuple[str, str, str, str], project_canvas: bool
) -> None:
    base_url, _first, _second, project_id = canvas_project
    _stub_server_info(page, canvas=True)
    query = f"?canvas={project_id}" if project_canvas else ""
    page.goto(f"{base_url}/canvas/c/temp%3Aexpired{query}")
    expect(page).to_have_url(re.compile(r"/canvas" + re.escape(query) + "$"))
    tab_name = re.compile("Canvas review") if project_canvas else "Main"
    expect(page.get_by_role("tab", name=tab_name, exact=not project_canvas)).to_have_attribute(
        "aria-selected", "true"
    )


def test_stale_regular_creation_url_returns_to_new_session(page: Page, live_server: str) -> None:
    _stub_server_info(page, canvas=True)
    page.goto(f"{live_server}/c/temp%3Aexpired")
    expect(page).to_have_url(f"{live_server}/")
    expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible()


@pytest.mark.parametrize("project_canvas", [False, True], ids=["main", "project"])
@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "mobile"])
def test_canvas_plus_creates_session_on_the_selected_board(
    page: Page,
    canvas_project: tuple[str, str, str, str],
    mock_llm_server_url: str,
    project_canvas: bool,
    mobile: bool,
) -> None:
    """Use a fixture host with real server creation, runner binding, and a mock-backed turn."""
    base_url, first, _second, project_id = canvas_project
    page.set_viewport_size(
        {"width": 390, "height": 844} if mobile else {"width": 1440, "height": 900}
    )
    _stub_server_info(page, canvas=True)
    source_response = httpx.get(f"{base_url}/v1/sessions/{first}", timeout=10)
    source_response.raise_for_status()
    source = source_response.json()
    agent_id = source["agent_id"]
    host_id = "canvas-create-host"
    page.route(
        "**/v1/hosts",
        lambda route: route.fulfill(
            json={
                "hosts": [
                    {
                        "host_id": host_id,
                        "name": "Canvas test host",
                        "status": "online",
                        "owner": "e2e",
                        "configured_harnesses": {source["harness"]: True},
                    }
                ]
            }
        ),
    )
    page.route(
        re.compile(r"/v1/agents(?:\?.*)?$"),
        lambda route: route.fulfill(
            json={
                "data": [
                    {
                        "id": agent_id,
                        "name": "hello_world",
                        "display_name": "Canvas test agent",
                        "harness": source["harness"],
                        "skills": [],
                    }
                ],
                "has_more": False,
            }
        ),
    )
    page.route(
        f"**/v1/hosts/{host_id}/worktrees?*", lambda route: route.fulfill(json={"data": []})
    )
    page.route(
        f"**/v1/hosts/{host_id}/harnesses/*/model-options",
        lambda route: route.fulfill(json={"models": []}),
    )
    page.add_init_script(
        'localStorage.setItem("omnigent:recent-workspaces", '
        'JSON.stringify({"canvas-create-host": ["/tmp"]}));'
    )
    created_ids: list[str] = []
    creation_errors: list[Exception] = []

    def create_on_fixture_runner(route: Route) -> None:
        if route.request.method != "POST":
            route.fallback()
            return
        try:
            payload = route.request.post_data_json
            assert isinstance(payload, dict), "Canvas session POST must contain a JSON object"
            # The shared verification runner has no host daemon. Create through the
            # real API and bind it directly; the rest of the UI and turn are live.
            body = {
                key: payload[key] for key in ("agent_id", "project_id", "labels") if key in payload
            }
            assert payload.get("agent_id") == agent_id, (
                "Canvas creation must use the fixture agent"
            )
            response = httpx.post(f"{base_url}/v1/sessions", json=body, timeout=30)
            response.raise_for_status()
            created = response.json()
            created_ids.append(created["id"])
            bind_session_runner(
                httpx.patch, base_url, created["id"], source["runner_id"], timeout=10
            )
            route.fulfill(status=response.status_code, json=created)
        except Exception as error:
            creation_errors.append(error)
            route.fulfill(status=500, json={"detail": "Canvas fixture runner setup failed"})

    page.route(re.compile(r"/v1/sessions(?:\?.*)?$"), create_on_fixture_runner)
    prompt = f"Create from Canvas {uuid.uuid4().hex[:8]}"
    reply = "This new session stays on the Canvas."
    configure_mock_llm(mock_llm_server_url, [{"text": reply}], match=prompt)
    query = f"?canvas={project_id}" if project_canvas else ""
    try:
        page.goto(f"{base_url}/canvas{query}")
        page.get_by_test_id("canvas-new-session").click()
        # Other tests can leave discoverable native agents on the shared server.
        page.get_by_test_id("new-chat-landing-agent-select").click()
        page.get_by_test_id("new-chat-landing-custom-agents").click()
        page.get_by_test_id(f"new-chat-landing-agent-{agent_id}").click()
        page.get_by_test_id("new-chat-landing-input").fill(prompt)
        with page.expect_response(
            lambda response: (
                response.request.method == "POST" and urlparse(response.url).path == "/v1/sessions"
            ),
            timeout=30_000,
        ) as creation:
            page.get_by_test_id("new-chat-landing-submit").click()
        if creation_errors:
            raise AssertionError(
                "Canvas creation on the fixture runner failed"
            ) from creation_errors[0]
        assert creation.value.ok
        expect(page).to_have_url(
            re.compile(r"/canvas/c/(?!temp)[^/?]+" + re.escape(query) + "$"), timeout=30_000
        )
        assert len(created_ids) == 1
        created_id = created_ids[0]
        expect(page.get_by_test_id("message-bubble").filter(has_text=reply)).to_be_visible(
            timeout=30_000
        )
        if mobile:
            page.get_by_role("button", name="Back to canvas", exact=True).click()
        else:
            expect(
                page.get_by_role("separator", name="Resize canvas and conversation")
            ).to_be_visible()
        card = page.locator(f'.react-flow__node[data-id="{created_id}"]').get_by_test_id(
            "session-card"
        )
        expect(card).to_be_visible()
        if project_canvas:
            expect(page.get_by_role("tab", name=re.compile("Canvas review"))).to_have_attribute(
                "aria-selected", "true"
            )
        else:
            expect(page.get_by_role("tab", name="Main", exact=True)).to_have_attribute(
                "aria-selected", "true"
            )
        snapshot_response = httpx.get(f"{base_url}/v1/sessions/{created_id}", timeout=10)
        snapshot_response.raise_for_status()
        snapshot = snapshot_response.json()
        assert snapshot.get("project_id") == (project_id if project_canvas else None)
        assert urlparse(page.url).path.startswith("/canvas")
    finally:
        for created_id in created_ids:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{base_url}/v1/sessions/{created_id}", timeout=10).raise_for_status()
