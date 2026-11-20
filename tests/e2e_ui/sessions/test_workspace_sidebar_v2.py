"""E2E: the ``workspace_sidebar_v2`` flag shows the workspace sidebar as tabs.

With the flag on, a desktop client's sidebar is the tabs panel: a New Tab page, Files with
the tree and editor, each pull request as its own page, and Back and Forward through them.
A narrow desktop window shows the tabs in its Workspace drawer. A touch client, such as a
phone, keeps the classic drawers with the flag on, as every client does with the flag off;
there the tabs code never loads.

The flag is turned on by answering ``/v1/info`` with it added to the server's own features;
the server side of the flag is covered by ``tests/server/test_feature_flags.py``. Pull
requests come from the same stubs as ``test_github_tab.py``, so no ``gh`` runs.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Locator, Page, Request, Route, expect

from tests._helpers.pull_request_ui import open_session_pull_requests
from tests.e2e_ui.conftest import open_right_rail
from tests.e2e_ui.files.test_file_autosave import (
    _cleanup_session_workdir,
    _seed_file,
    _wait_for_persisted,
)
from tests.e2e_ui.github.test_github_tab import _PR_NUMBER, _stub_github

_VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    # Below the shell's 1181px breakpoint a desktop window gets the mobile layout too.
    "narrow-desktop": {"width": 700, "height": 900},
    "phone": {"width": 390, "height": 844},
}
# The tabs sidebar's lazily loaded chunks, including its ⌘K rows; the classic sidebar and
# the mobile layout never request them.
_TABS_CHUNK = re.compile(
    r"/assets/(?:WorkspaceTabsHost|workspaceTabsSessions|featurePaletteRows)-[^/]*\.js"
)
# Text only the tabs sidebar's code carries: its CodeMirror token class, history group,
# New Tab picker and shortcut list. A chunk rule that pulled that code into an eager chunk,
# or a lazy branch that loaded it, would bring one of them here.
_TABS_CODE_MARKERS = (
    "cm-shiki-token",
    "Workspace history",
    "Open an app in this tab",
    "Reopen closed workspace tab",
)
_FILE = "tabs_sidebar_notes.py"
_EDIT = "# saved from the tabs sidebar"
_PR_TITLE = "Add the GitHub tab"


@pytest.fixture
def workspace_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """The seeded session with one file in its workspace."""
    base_url, session_id = seeded_session
    _seed_file(base_url, session_id, _FILE, "print('hello')\n")
    try:
        yield seeded_session
    finally:
        _cleanup_session_workdir(session_id)


def _set_flag(page: Page, enabled: bool) -> None:
    """Answer ``/v1/info`` with the server's own payload and the flag set to ``enabled``."""

    def answer(route: Route) -> None:
        response = route.fetch()
        info = response.json()
        info["features"] = {**(info.get("features") or {}), "workspace_sidebar_v2": enabled}
        route.fulfill(response=response, json=info)

    page.route("**/v1/info", answer)


def _record_scripts(page: Page) -> list[str]:
    """Collect the URL of every script the page loads, eager or lazy."""
    scripts: list[str] = []

    def note(request: Request) -> None:
        if request.resource_type == "script" and request.url not in scripts:
            scripts.append(request.url)

    page.on("request", note)
    return scripts


def _script_bodies(page: Page, scripts: list[str]) -> dict[str, str]:
    """Each script's text, fetched again: a reload drops the bodies the page received."""
    assert scripts, "no scripts were recorded"
    return {url: page.request.get(url).text() for url in scripts}


def _expect_no_tabs_code(page: Page, scripts: list[str]) -> None:
    """No script the page loaded is, or contains, the tabs sidebar's code."""
    for url, body in _script_bodies(page, scripts).items():
        assert not _TABS_CHUNK.search(url), url
        for marker in _TABS_CODE_MARKERS:
            assert marker not in body, f"{url} carries {marker!r}"


def _open_session(page: Page, base_url: str, session_id: str, viewport: str) -> None:
    # These journeys check routing, saving and script loading, which don't vary by theme.
    page.set_viewport_size(_VIEWPORTS[viewport])
    page.goto(f"{base_url}/c/{session_id}")


def _expect_within(item: Locator, container: Locator) -> None:
    """``item`` is fully inside ``container`` and the window once any slide-in settles."""
    page = item.page
    viewport = page.viewport_size
    assert viewport is not None
    for _ in range(50):
        item_box = item.bounding_box()
        container_box = container.bounding_box()
        assert item_box is not None and container_box is not None
        container_right = container_box["x"] + container_box["width"]
        if (
            item_box["x"] >= container_box["x"] - 1
            and item_box["x"] + item_box["width"] <= container_right + 1
            and container_right <= viewport["width"] + 1
        ):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"{item_box} spills out of {container_box} in {viewport}")


def _open_app(page: Page, sidebar: Locator, app: str) -> None:
    """Open ``app`` in a new tab from the strip's "+", as a person would."""
    sidebar.get_by_role("button", name="New tab", exact=True).click()
    picker = sidebar.get_by_role("combobox", name="Open an app in this tab")
    expect(picker).to_be_focused(timeout=10_000)
    sidebar.get_by_role("option", name=re.compile(rf"^{re.escape(app)}", re.IGNORECASE)).click()


def test_tabs_sidebar_files_history_and_pull_request(
    page: Page, workspace_session: tuple[str, str]
) -> None:
    """Files edits save and reopen; Back and Forward step, also from ⌘K; a PR opens as a tab."""
    base_url, session_id = workspace_session
    _stub_github(page)
    _set_flag(page, True)
    scripts = _record_scripts(page)
    _open_session(page, base_url, session_id, "desktop")
    open_right_rail(page)
    sidebar = page.get_by_role("complementary", name="Workspace")
    history = sidebar.get_by_role("group", name="Workspace history")
    expect(history).to_be_visible(timeout=60_000)
    # The classic rail's own tabs are gone.
    expect(sidebar.get_by_role("tab", name="Pull Requests", exact=True)).to_have_count(0)

    # Files: open the file from the tree, edit it, and the edit saves as it goes.
    _open_app(page, sidebar, "Files")
    file_row = sidebar.get_by_role("treeitem", name=re.compile(re.escape(_FILE)))
    expect(file_row).to_be_visible(timeout=30_000)
    file_row.click()
    editor = sidebar.locator(".cm-content")
    expect(editor).to_contain_text("print('hello')", timeout=30_000)
    editor.click()
    page.keyboard.press("ControlOrMeta+End")
    page.keyboard.type(f"\n{_EDIT}")
    _wait_for_persisted(base_url, session_id, _FILE, _EDIT)
    file_tab = sidebar.get_by_role("tab", name=re.compile(rf"^{re.escape(_FILE)}"))
    expect(file_tab).to_have_attribute("aria-selected", "true")
    _expect_within(file_tab, sidebar)

    # Back returns to the tree the file opened from; Forward to the file again.
    history.get_by_role("button", name="Back").click()
    expect(file_row).to_be_visible()
    expect(editor).to_have_count(0)
    history.get_by_role("button", name="Forward").click()
    expect(editor).to_contain_text(_EDIT)

    # ⌘K runs the same Back.
    page.keyboard.press("ControlOrMeta+k")
    palette = page.get_by_test_id("command-palette-input")
    expect(palette).to_be_focused(timeout=10_000)
    palette.fill("Go back in the workspace")
    page.get_by_role("option", name=re.compile("Go back in the workspace")).click()
    expect(palette).to_have_count(0)
    expect(file_row).to_be_visible()
    expect(editor).to_have_count(0)
    history.get_by_role("button", name="Forward").click()
    expect(editor).to_contain_text(_EDIT)

    # Reopening the session brings the tab back with the saved text.
    page.reload()
    open_right_rail(page)
    expect(sidebar.locator(".cm-content")).to_contain_text(_EDIT, timeout=30_000)

    # Pull requests: the list, then the pull request as its own tab.
    _open_app(page, sidebar, "Pull Requests")
    sidebar.get_by_role("button", name=re.compile(rf"#{_PR_NUMBER}\b")).first.click()
    pr_tab = sidebar.get_by_role("tab", name=re.compile(rf"#{_PR_NUMBER}"))
    expect(pr_tab).to_have_attribute("aria-selected", "true", timeout=30_000)
    _expect_within(pr_tab, sidebar)
    expect(sidebar.get_by_text(_PR_TITLE)).to_be_visible(timeout=30_000)
    sections = sidebar.get_by_role("tablist", name="Pull request")
    sections.get_by_role("tab", name="Comments 1", exact=True).click()
    expect(sidebar.get_by_text("Nice work!")).to_be_visible()
    sections.get_by_role("tab", name="Changes").click()
    expect(sidebar.get_by_role("button", name="src/app", exact=True)).to_be_visible()

    # Back steps through the pull request's sections.
    history.get_by_role("button", name="Back").click()
    expect(sections.get_by_role("tab", name="Comments 1", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )

    # From another tab, the composer's link to its one PR shows that PR's tab again; it
    # waits for the selection to move, so the click's effect has landed.
    file_tab.click()
    expect(pr_tab).to_have_attribute("aria-selected", "false")
    open_tabs = sidebar.locator("[data-workspace-tab-id]").count()
    page.get_by_test_id("composer-pr-link").click()
    expect(pr_tab).to_have_attribute("aria-selected", "true")
    expect(sidebar.locator("[data-workspace-tab-id]")).to_have_count(open_tabs)

    # The chunks and markers the other journeys look for are in what the tabs sidebar loads.
    for chunk in ("WorkspaceTabsHost", "workspaceTabsSessions", "featurePaletteRows"):
        assert any(f"/assets/{chunk}-" in url for url in scripts), chunk
    bodies = _script_bodies(page, scripts).values()
    for marker in _TABS_CODE_MARKERS:
        assert any(marker in body for body in bodies), marker


def _press_palette(page: Page) -> Locator:
    """Open ⌘K once the session's page is up, and search it for the tabs' Back row."""
    actions = page.get_by_role("button", name=re.compile(r"^(?:Conversation|Session) actions$"))
    expect(actions).to_be_visible(timeout=30_000)
    page.keyboard.press("ControlOrMeta+k")
    palette = page.get_by_test_id("command-palette-input")
    expect(palette).to_be_focused(timeout=10_000)
    palette.fill("Go back in the workspace")
    return page.get_by_role("option", name=re.compile("Go back in the workspace"))


def test_flag_on_shows_the_tabs_in_a_narrow_windows_drawer(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """A narrow desktop window lists the tabs' ⌘K rows and opens them in the Workspace drawer."""
    base_url, session_id = seeded_session
    _stub_github(page)
    _set_flag(page, True)
    scripts = _record_scripts(page)
    _open_session(page, base_url, session_id, "narrow-desktop")

    expect(_press_palette(page)).to_have_count(1, timeout=10_000)
    page.keyboard.press("Escape")

    open_session_pull_requests(page, mobile=True)
    drawer = page.get_by_test_id("workspace-tabs-drawer")
    expect(drawer).to_have_attribute("data-state", "open", timeout=30_000)
    expect(page.get_by_test_id("github-panel-drawer")).not_to_have_attribute("data-state", "open")
    sidebar = drawer.get_by_role("complementary", name="Workspace")
    expect(sidebar.get_by_text(_PR_TITLE)).to_be_visible(timeout=30_000)
    _expect_within(sidebar.get_by_text(_PR_TITLE), drawer)
    for chunk in ("WorkspaceTabsHost", "workspaceTabsSessions", "featurePaletteRows"):
        assert any(f"/assets/{chunk}-" in url for url in scripts), chunk


@pytest.mark.browser_context_args(viewport=_VIEWPORTS["phone"], has_touch=True, is_mobile=True)
def test_flag_on_keeps_the_classic_drawers_on_a_touch_phone(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """A touch phone opens ⌘K and the PR drawer as before, and never loads the tabs."""
    base_url, session_id = seeded_session
    _stub_github(page)
    _set_flag(page, True)
    scripts = _record_scripts(page)
    _open_session(page, base_url, session_id, "phone")
    assert page.evaluate("matchMedia('(pointer: coarse)').matches"), "not a touch client"

    expect(_press_palette(page)).to_have_count(0)
    page.keyboard.press("Escape")
    expect(page.get_by_test_id("command-palette-input")).to_have_count(0)

    open_session_pull_requests(page, mobile=True)
    drawer = page.get_by_test_id("github-panel-drawer")
    expect(drawer).to_have_attribute("data-state", "open", timeout=30_000)
    expect(drawer.get_by_text(_PR_TITLE)).to_be_visible(timeout=30_000)
    _expect_within(drawer.get_by_text(_PR_TITLE), drawer)
    expect(page.get_by_test_id("workspace-tabs-drawer")).to_have_count(0)
    _expect_no_tabs_code(page, scripts)


@pytest.mark.parametrize("viewport", ["desktop", "phone"])
def test_flag_off_keeps_the_classic_sidebar(
    page: Page, seeded_session: tuple[str, str], viewport: str
) -> None:
    """Without the flag the sidebar is unchanged at both widths, and the tabs never load."""
    base_url, session_id = seeded_session
    _stub_github(page)
    _set_flag(page, False)
    scripts = _record_scripts(page)
    _open_session(page, base_url, session_id, viewport)

    if viewport == "phone":
        open_session_pull_requests(page, mobile=True)
        drawer = page.get_by_test_id("github-panel-drawer")
        expect(drawer).to_have_attribute("data-state", "open", timeout=30_000)
        expect(drawer.get_by_text(_PR_TITLE)).to_be_visible(timeout=30_000)
    else:
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")
        rail.get_by_role("tab", name="Pull Requests").click()
        expect(rail.get_by_text(_PR_TITLE)).to_be_visible(timeout=30_000)
        expect(rail.get_by_role("group", name="Workspace history")).to_have_count(0)
    _expect_no_tabs_code(page, scripts)
