"""The open slash menu settles directly from a host discovery response."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

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


@pytest.mark.parametrize("trigger", ["$", "/"], ids=["dollar", "slash-alias"])
def test_codex_skill_menu_completes_and_sends_native_skill(
    page: Page,
    seeded_session: tuple[str, str],
    trigger: str,
) -> None:
    """Both triggers show the combined menu and send dollar-prefixed skills."""
    base_url, session_id = seeded_session
    session_path = f"/v1/sessions/{session_id}"
    posted_events: list[dict] = []

    def snapshot(route: Route) -> None:
        if urlparse(route.request.url).path != session_path or route.request.method != "GET":
            route.fallback()
            return
        response = route.fetch()
        body = response.json()
        body.update(
            harness="codex-native",
            host_id="menu-host",
            workspace="/workspace",
        )
        route.fulfill(response=response, json=body)

    def capture_event(route: Route) -> None:
        if route.request.method != "POST":
            route.fallback()
            return
        posted_events.append(route.request.post_data_json)
        route.fulfill(json={})

    page.route(f"**{session_path}*", snapshot)
    page.route(f"**{session_path}/events", capture_event)
    page.route(
        "**/v1/skills?*",
        lambda route: route.fulfill(
            json={"skills": [{"name": "code-review", "description": "Review the current change"}]}
        ),
    )
    _install_stream_controller(page, session_id)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(trigger)
    expect(page.get_by_test_id("slash-menu-item-help")).to_have_text("/help")
    expect(page.get_by_test_id("slash-menu-item-code-review")).to_have_text("$code-review")

    composer.fill(f"{trigger}review")
    composer.press("Tab")
    expect(composer).to_have_value("$code-review ")
    assert posted_events == []
    composer.fill("$code-review focus on tests")
    overlay = page.get_by_test_id("composer-highlight-overlay")
    expect(overlay).to_have_text("$code-review focus on tests")
    expect(overlay.locator(".text-brand-accent")).to_have_text("$code-review")

    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and urlparse(response.url).path == f"{session_path}/events"
        )
    ):
        composer.press("Enter")
    assert len(posted_events) == 1
    assert posted_events[0]["type"] == "message"
    assert posted_events[0]["data"]["content"] == [
        {"type": "input_text", "text": "$code-review focus on tests"}
    ]


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


@pytest.mark.parametrize("harness,prefix", [("claude-sdk", "/"), ("codex-native", "$")])
def test_new_session_menu_uses_the_selected_agents_effective_catalog(
    page: Page, seeded_session: tuple[str, str], harness: str, prefix: str
) -> None:
    """The host request includes the agent, and its result replaces cached suggestions."""
    base_url, session_id = seeded_session
    pending: list[Route] = []
    page.route(
        "**/v1/hosts",
        lambda route: route.fulfill(
            json={
                "hosts": [
                    {
                        "host_id": "preview-host",
                        "name": "Preview host",
                        "owner": "local",
                        "status": "online",
                    }
                ]
            }
        ),
    )
    page.route(
        "**/v1/agents",
        lambda route: route.fulfill(
            json={
                "data": [
                    {
                        "id": "preview-agent",
                        "name": "preview-agent",
                        "harness": harness,
                        "skills": [{"name": "obsolete", "description": "Old bundled suggestion"}],
                    }
                ],
                "has_more": False,
            }
        ),
    )
    page.route(
        "**/v1/hosts/preview-host/harnesses/*/model-options*",
        lambda route: route.fulfill(json={"models": []}),
    )

    def discover(route: Route) -> None:
        if "host_id" in parse_qs(urlparse(route.request.url).query):
            pending.append(route)
        else:
            route.fallback()

    page.route("**/v1/skills?*", discover)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
    page.evaluate("""() => localStorage.setItem(
        'omnigent:recent-workspaces', JSON.stringify({'preview-host': ['/tmp']})
    )""")
    page.get_by_test_id("new-chat-button").click()
    composer = page.get_by_test_id("new-chat-landing-input")
    composer.fill("Hello")
    expect(page.get_by_test_id("new-chat-landing-submit")).to_be_enabled()
    composer.fill("/allow")
    expect(page.get_by_text("Loading skills…", exact=True)).to_be_visible()
    expect(page.get_by_test_id("new-chat-landing-submit")).to_be_disabled()
    composer.press("Tab")
    expect(composer).to_have_value("/allow")
    assert len(pending) == 1
    assert parse_qs(urlparse(pending[0].request.url).query) == {
        "host_id": ["preview-host"],
        "harness": [harness],
        "path": ["/tmp"],
        "agent_id": ["preview-agent"],
    }
    pending[0].fulfill(json={"skills": [{"name": "allowed", "description": "Permitted skill"}]})
    expect(page.get_by_test_id("slash-menu-item-allowed")).to_have_text(f"{prefix}allowed")
    composer.fill("/")
    expect(page.get_by_test_id("slash-menu-item-obsolete")).not_to_be_visible()
    composer.fill("/allow")
    composer.press("Tab")
    expect(composer).to_have_value(f"{prefix}allowed ")
    expect(page.get_by_test_id("new-chat-landing-submit")).to_be_enabled()


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
