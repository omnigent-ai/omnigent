"""GitHub connection and repository controls with intercepted provider responses.

These exercise the browser; OAuth, repository discovery, and disconnect never
reach GitHub or mutate an account. Backend tests cover those API contracts.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry, open_right_rail
from tests.e2e_ui.github.test_github_tab import _INFO, _stub_github


def _enable_github(page: Page, *, managed: bool = False) -> None:
    def info(route: Route) -> None:
        response = fetch_with_retry(route)
        body = response.json()
        body["enabled_connections"] = ["github"]
        if managed:
            body.update(managed_sandboxes_enabled=True, sandbox_provider="modal")
        route.fulfill(response=response, json=body)

    page.route("**/v1/info", info)


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_github_connection_controls(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path, width: int
) -> None:
    base_url, _ = seeded_session
    page.set_viewport_size({"width": width, "height": 900})
    _enable_github(page)
    state = {"connected": False, "disconnects": 0}
    connect_urls: list[str] = []

    def connection(route: Route) -> None:
        path = urlsplit(route.request.url).path
        if path.endswith("/connect"):
            connect_urls.append(route.request.url)
            state["connected"] = True
            route.fulfill(
                status=302, headers={"location": "/settings/integrations?github=connected"}
            )
        elif path.endswith("/disconnect"):
            assert route.request.method == "POST"
            state.update(connected=False, disconnects=state["disconnects"] + 1)
            route.fulfill(json={})
        else:
            route.fulfill(
                json={
                    "enabled": True,
                    "connected": state["connected"],
                    "login": "octocat" if state["connected"] else None,
                    "scopes": None,
                    "connected_at": None,
                    "install_url": None,
                }
            )

    page.route(
        re.compile(r"/v1/connections/github/(status|connect|disconnect)(?:\?|$)"), connection
    )
    page.goto(f"{base_url}/settings/integrations")
    if width < 768:
        page.get_by_test_id("settings-nav-integrations").click()
    page.get_by_role("button", name="Connect GitHub", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("GitHub account connected.")
    if width < 768:
        page.get_by_test_id("settings-nav-integrations").click()
    expect(page.get_by_text(re.compile(r"Connected as octocat\."))).to_be_visible()
    assert parse_qs(urlsplit(connect_urls[0]).query)["return_to"] == ["/settings/integrations"]
    page.screenshot(path=tmp_path / "github-connected.png", animations="disabled")
    page.get_by_role("button", name="Disconnect", exact=True).click()
    expect(page.get_by_role("button", name="Connect GitHub", exact=True)).to_be_visible()
    assert state["disconnects"] == 1
    page.screenshot(path=tmp_path / "github-disconnected.png", animations="disabled")
    page.goto(f"{base_url}/settings/integrations?github=error")
    if width < 768:
        page.get_by_test_id("settings-nav-integrations").click()
    expect(page.get_by_role("alert")).to_contain_text("Couldn't connect your GitHub account.")


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_github_repository_and_branch_picker(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path, width: int
) -> None:
    base_url, _ = seeded_session
    page.set_viewport_size({"width": width, "height": 900})
    _enable_github(page, managed=True)
    page.route(
        "**/v1/connections/github/repos",
        lambda route: route.fulfill(
            json={
                "connected": True,
                "repos": [
                    {
                        "full_name": "octocat/hello",
                        "clone_url": "https://github.com/octocat/hello.git",
                        "default_branch": "main",
                        "private": False,
                        "pushed_at": None,
                    }
                ],
            }
        ),
    )
    page.route(
        "**/v1/connections/github/repos/octocat/hello/branches",
        lambda route: route.fulfill(
            json={"connected": True, "branches": ["main", "feature/example"]}
        ),
    )
    page.goto(base_url)
    chip = page.get_by_test_id("new-chat-landing-repo-chip")
    expect(chip).to_be_visible(timeout=30_000)
    chip.click()
    page.get_by_role("combobox", name="GitHub repository", exact=True).click()
    page.get_by_placeholder("Search repositories…").fill("hello")
    page.get_by_role("option", name="octocat/hello", exact=True).click()
    page.get_by_role("combobox", name="Branch for octocat/hello", exact=True).click()
    page.get_by_role("option", name="feature/example", exact=True).click()
    expect(chip).to_have_attribute("aria-label", "Sandbox repositories: hello#feature/example")
    page.screenshot(path=tmp_path / "github-repository-branch.png", animations="disabled")
    page.get_by_role("button", name="Remove octocat/hello", exact=True).click()
    expect(chip).to_have_attribute("aria-label", "Sandbox repositories: None selected")


@pytest.mark.parametrize("entry", ["desktop-keyboard", "mobile-menu"])
def test_github_alternate_entry_points(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path, entry: str
) -> None:
    base_url, session_id = seeded_session
    mobile = entry == "mobile-menu"
    page.set_viewport_size({"width": 390 if mobile else 1280, "height": 900})
    _stub_github(page)
    page.goto(f"{base_url}/c/{session_id}")
    if mobile:
        page.get_by_role("button", name=re.compile(r"^(Conversation|Session) actions$")).click()
        page.get_by_role("menuitem", name=re.compile(r"(?:GitHub|Pull Requests)$")).click()
        panel = page.get_by_test_id("github-panel-drawer")
        expect(panel).to_have_attribute("data-state", "open")
    else:
        open_right_rail(page)
        panel = page.get_by_role("complementary", name="Workspace")
        tab = panel.get_by_role("tab", name=re.compile(r"^(GitHub|Pull Requests)$"))
        shortcut = tab.get_attribute("aria-keyshortcuts")
        assert shortcut
        panel.get_by_role("tab", name="Files", exact=True).focus()
        page.keyboard.press(shortcut)
        expect(tab).to_have_attribute("aria-selected", "true")
    expect(panel.get_by_text(_INFO["pr"]["title"], exact=True)).to_be_visible()
    page.screenshot(path=tmp_path / f"github-{entry}.png", animations="disabled")
