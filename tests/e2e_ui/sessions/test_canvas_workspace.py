"""Canvas keeps the project board beside a usable, interactive session."""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm
from tests.e2e_ui.sessions.test_canvas_page import _stub_server_info


@pytest.fixture
def canvas_project(
    seeded_session_pair: tuple[str, str, str],
) -> Iterator[tuple[str, str, str, str]]:
    base_url, first, second = seeded_session_pair
    created = httpx.post(
        f"{base_url}/v1/projects",
        json={"name": f"Canvas review {uuid.uuid4().hex[:6]}"},
        timeout=10.0,
    )
    created.raise_for_status()
    project_id = created.json()["id"]
    try:
        for session_id, title in [(first, "Review navigation"), (second, "Inspect mobile layout")]:
            httpx.patch(
                f"{base_url}/v1/sessions/{session_id}",
                json={"title": title, "project_id": project_id},
                timeout=10.0,
            ).raise_for_status()
        yield base_url, first, second, project_id
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{base_url}/v1/projects/{project_id}", timeout=10.0).raise_for_status()


@pytest.mark.parametrize("width", [1440, 720])
def test_canvas_controls_clear_the_macos_titlebar(
    page: Page, canvas_project: tuple[str, str, str, str], width: int
) -> None:
    """Exercise macOS shell CSS in Chromium; native traffic lights are not rendered."""
    base_url, first, _second, project_id = canvas_project
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(
        """
        Object.defineProperty(navigator, 'userAgent', {
          value: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)', configurable: true,
        });
        window.omnigentDesktop = {
          kind: 'electron',
          setBadgeCount() {},
          notify: () => Promise.resolve(false),
          onNotificationActivated: () => () => {},
          getServerPicker: () => Promise.resolve(null),
          switchServer: () => Promise.resolve(),
          openServerSetup() {},
        };
        """
    )
    _stub_server_info(page, canvas=True)
    page.goto(f"{base_url}/canvas?canvas={project_id}")
    expect(page.locator("html")).to_have_attribute("data-electron-mac", "true")
    heading = page.get_by_role("heading", name="Canvas", exact=True)
    expect(heading).to_be_visible()
    strip = page.locator(".electron-drag-strip").bounding_box()
    assert strip is not None
    titlebar_bottom = strip["y"] + strip["height"]
    expand = page.get_by_role("button", name="Open sidebar", exact=True)
    expect(expand).to_have_count(1)
    expect(page.get_by_role("navigation", name="Collapsed sidebar")).to_have_count(0)
    for control in (expand, heading) if width < 768 else (heading,):
        bounds = control.bounding_box()
        assert bounds is not None and bounds["y"] >= titlebar_bottom, (
            f"Canvas control overlaps the native titlebar: {bounds}, bottom={titlebar_bottom}"
        )
    if width >= 768:
        expect(
            page.locator(".electron-sidebar-header-actions").get_by_role(
                "button", name="Open sidebar", exact=True
            )
        ).to_be_visible()
        bounds = expand.bounding_box()
        assert bounds and bounds["y"] + bounds["height"] <= titlebar_bottom
    expand.click()
    expect(page.get_by_test_id("canvas-nav")).to_be_visible()
    if width < 768:
        page.get_by_test_id("sidebar-scrim").click(position={"x": width - 28, "y": 100})
    else:
        page.locator(".electron-sidebar-header-actions").get_by_role(
            "button", name="Close sidebar", exact=True
        ).click()
    card = page.locator(f'.react-flow__node[data-id="{first}"]').get_by_test_id("session-card")
    card.click()
    expect(expand).to_have_count(1)
    if width < 760:
        restore = page.get_by_role("button", name="Back to canvas", exact=True)
    else:
        page.get_by_role("button", name="Focus conversation", exact=True).click()
        restore = page.get_by_role("button", name="Show canvas beside conversation", exact=True)
    expect(restore).to_be_visible()
    expect(expand).to_have_count(1)
    expand.click()
    expect(page.get_by_test_id("canvas-nav")).to_be_visible()
    if width < 768:
        page.get_by_test_id("sidebar-scrim").click(position={"x": width - 28, "y": 100})
    else:
        page.locator(".electron-sidebar-header-actions").get_by_role(
            "button", name="Close sidebar", exact=True
        ).click()
    bounds = restore.bounding_box()
    assert bounds is not None and bounds["y"] >= titlebar_bottom
    restore.click()
    expect(heading).to_be_visible()


def test_canvas_keeps_board_drafts_and_sidebar_while_switching_sessions(
    page: Page,
    canvas_project: tuple[str, str, str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, first, second, project_id = canvas_project
    page.set_viewport_size({"width": 1440, "height": 900})
    _stub_server_info(page, canvas=True)
    page.goto(f"{base_url}/c/{first}")
    page.get_by_test_id("canvas-nav").click()
    expect(page.get_by_role("button", name="Open sidebar", exact=True)).to_be_visible()
    project = page.get_by_role("tab", name=re.compile("Canvas review"))
    project.click()
    cards = page.get_by_test_id("session-card")
    expect(cards).to_have_count(2)
    first_card = page.locator(f'.react-flow__node[data-id="{first}"]').get_by_test_id(
        "session-card"
    )
    second_card = page.locator(f'.react-flow__node[data-id="{second}"]').get_by_test_id(
        "session-card"
    )
    board = page.get_by_test_id("canvas-flow").element_handle()
    assert board is not None

    first_card.click()
    expect(page).to_have_url(re.compile(rf"/canvas/c/{first}\?canvas={project_id}$"))
    expect(project).to_be_visible()
    expect(first_card).to_have_attribute("aria-pressed", "true")
    page.get_by_test_id("canvas-flow").click(position={"x": 20, "y": 20})
    expect(first_card).to_have_attribute("aria-pressed", "true")
    composer = page.get_by_label("Message the agent", exact=True)
    expect(composer).to_be_visible()
    composer_box = composer.bounding_box()
    assert composer_box is not None and composer_box["width"] > 250

    prompt = "Check the side-by-side Canvas layout"
    reply = "The Canvas board stays beside this conversation."
    configure_mock_llm(mock_llm_server_url, [{"text": reply}], match=prompt)
    composer.fill(prompt)
    composer.press("Enter")
    expect(page.get_by_test_id("message-bubble").filter(has_text=reply)).to_be_visible(
        timeout=30_000
    )
    composer.fill("Keep this draft while I check another session")
    page.get_by_role("button", name="Open sidebar", exact=True).click()
    expect(page.get_by_test_id("canvas-nav")).to_be_visible()
    second_card.click()
    expect(page.get_by_test_id("canvas-nav")).to_be_visible()
    expect(second_card).to_have_attribute("aria-pressed", "true")
    expect(first_card).to_have_attribute("aria-pressed", "false")
    expect(composer).to_have_value("")
    first_card.click()
    expect(composer).to_have_value("Keep this draft while I check another session")
    page.get_by_role("button", name="Close sidebar", exact=True).click()

    divider = page.get_by_role("separator", name="Resize canvas and conversation")
    before = float(divider.get_attribute("aria-valuenow") or "0")
    divider.press("ArrowLeft")
    page.wait_for_function(
        "([divider, before]) => Number(divider.getAttribute('aria-valuenow')) < before",
        arg=[divider.element_handle(), before],
    )
    handle = divider.bounding_box()
    assert handle is not None
    page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2)
    page.mouse.down()
    page.mouse.move(handle["x"] - 70, handle["y"] + handle["height"] / 2, steps=8)
    page.mouse.up()
    page.wait_for_function(
        "([divider, before]) => Number(divider.getAttribute('aria-valuenow')) < before - 60",
        arg=[divider.element_handle(), before],
    )
    page.get_by_role("button", name="Focus conversation", exact=True).click()
    expect(page.get_by_role("region", name="Canvas pane", exact=True)).not_to_be_visible()
    page.get_by_role("button", name="Show canvas beside conversation", exact=True).click()
    expect(first_card).to_be_visible()
    assert board.evaluate("element => element.isConnected")

    expect(composer).to_have_value("Keep this draft while I check another session")

    page.get_by_role("button", name="Close conversation pane", exact=True).click()
    expect(page).to_have_url(re.compile(rf"/canvas\?canvas={project_id}$"))
    expect(divider).to_have_count(0)
    expect(project).to_have_attribute("aria-selected", "true")
    expect(first_card).to_have_attribute("aria-pressed", "false")
    page.go_back()
    expect(first_card).to_have_attribute("aria-pressed", "true")
    expect(composer).to_have_value("Keep this draft while I check another session")
    assert board.evaluate("element => element.isConnected")
    page.set_viewport_size({"width": 1024, "height": 800})
    close_button = page.get_by_role("button", name="Close conversation pane", exact=True)
    expect(close_button).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("width", [1440, 1024])
def test_canvas_stays_visible_when_workspace_opens(
    page: Page, canvas_project: tuple[str, str, str, str], width: int
) -> None:
    base_url, first, _second, project_id = canvas_project
    _stub_server_info(page, canvas=True)
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{base_url}/canvas/c/{first}?canvas={project_id}")
    canvas = page.get_by_role("region", name="Canvas pane", exact=True)
    workspace = page.get_by_role("complementary", name="Workspace", exact=True)
    composer = page.get_by_label("Message the agent", exact=True)
    expect(composer).to_be_visible()
    composer.fill("Keep my draft while I use the workspace")
    board = page.get_by_test_id("canvas-flow").element_handle()
    assert board is not None
    page.get_by_role("button", name="Expand right panel", exact=True).click()
    expect(canvas).to_be_visible()
    expect(workspace).to_be_visible()
    expect(page.get_by_role("button", name="Focus conversation", exact=True)).to_be_visible()
    expect(
        page.get_by_role("button", name="Show canvas beside conversation", exact=True)
    ).to_have_count(0)
    expect(
        page.get_by_role("button", name="Close conversation pane", exact=True)
    ).to_be_in_viewport()
    expect(composer).to_have_value("Keep my draft while I use the workspace")
    expect(page.locator("[data-workspace-panel-animate]")).to_have_count(0)
    canvas_box = canvas.bounding_box()
    workspace_box = workspace.bounding_box()
    composer_box = composer.bounding_box()
    assert canvas_box and workspace_box and composer_box
    assert canvas_box["width"] >= 320
    assert workspace_box["width"] >= 240
    assert composer_box["width"] >= 300
    assert canvas_box["x"] + canvas_box["width"] <= composer_box["x"]
    assert composer_box["x"] + composer_box["width"] <= workspace_box["x"]
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    page.get_by_role("button", name="Focus conversation", exact=True).click()
    expect(canvas).not_to_be_visible()
    page.get_by_role("button", name="Show canvas beside conversation", exact=True).click()
    expect(canvas).to_be_visible()
    expect(workspace).to_be_visible()
    workspace.get_by_role("button", name="Full screen", exact=True).click()
    expect(canvas).to_be_visible()
    workspace.get_by_role("button", name="Exit full screen", exact=True).click()
    expect(canvas).to_be_visible()
    page.get_by_role("button", name="Collapse right panel", exact=True).click()
    expect(workspace).not_to_be_visible()
    expect(canvas).to_be_visible()
    expect(composer).to_have_value("Keep my draft while I use the workspace")
    assert board.evaluate("element => element.isConnected")


@pytest.mark.parametrize("width", [390, 767])
def test_canvas_deep_link_and_mobile_return_keep_the_project(
    page: Page,
    canvas_project: tuple[str, str, str, str],
    width: int,
) -> None:
    base_url, first, _second, project_id = canvas_project
    _stub_server_info(page, canvas=True)
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(f"{base_url}/c/{first}")
    menu = page.get_by_role("button", name="Open sidebar", exact=True)
    expect(menu).to_be_visible()
    ordinary_icon = menu.locator("svg").inner_html()
    ordinary_bounds = menu.bounding_box()
    assert ordinary_bounds is not None

    page.goto(f"{base_url}/canvas/c/{first}?canvas={project_id}")
    expect(page.get_by_label("Message the agent", exact=True)).to_be_visible()
    expect(page.get_by_role("region", name="Canvas pane", exact=True)).not_to_be_visible()
    page.get_by_role("button", name="Back to canvas", exact=True).click()
    expect(page).to_have_url(re.compile(rf"/canvas\?canvas={re.escape(project_id)}$"))
    expect(page.get_by_role("tab", name=re.compile("Canvas review"))).to_have_attribute(
        "aria-selected", "true"
    )
    first_card = page.locator(f'.react-flow__node[data-id="{first}"]').get_by_test_id(
        "session-card"
    )
    expect(first_card).to_be_visible()
    expect(page.get_by_role("navigation", name="Collapsed sidebar")).not_to_be_visible()
    expect(menu).to_be_visible()
    assert menu.locator("svg").inner_html() == ordinary_icon
    board_bounds = menu.bounding_box()
    assert board_bounds is not None
    for key in ("x", "y", "width", "height"):
        assert board_bounds[key] == pytest.approx(ordinary_bounds[key], abs=1)
    menu.click()
    expect(page.get_by_test_id("canvas-nav")).to_be_visible()
    page.get_by_test_id("sidebar-scrim").click(position={"x": width - 28, "y": 100})
    expect(menu).to_be_visible()

    first_card.click()
    expect(page.get_by_role("button", name="Back to canvas", exact=True)).to_be_visible()
    expect(menu).to_be_visible()
    assert menu.locator("svg").inner_html() == ordinary_icon
    conversation_bounds = menu.bounding_box()
    assert conversation_bounds is not None
    for key in ("x", "y", "width", "height"):
        assert conversation_bounds[key] == pytest.approx(ordinary_bounds[key], abs=1)
    menu.click()
    page.get_by_test_id("canvas-nav").click()
    expect(page).to_have_url(re.compile(rf"/canvas\?canvas={re.escape(project_id)}$"))
    expect(menu).to_be_visible()
    first_card.click()
    expect(page.get_by_role("button", name="Back to canvas", exact=True)).to_be_visible()
    page.reload()
    page.get_by_role("button", name="Back to canvas", exact=True).click()
    expect(page.get_by_role("tab", name=re.compile("Canvas review"))).to_have_attribute(
        "aria-selected", "true"
    )
    expect(menu).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
