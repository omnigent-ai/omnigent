"""E2E: the ``workspace_sidebar_v2`` flag shows the workspace sidebar as tabs.

With the flag on, a desktop client's sidebar is the tabs panel: a New Tab page, Files with
the tree and editor, each pull request as its own page, and Back and Forward through them.
A Files row's menu copies its path and a link to it.
A narrow desktop window shows the tabs in its Workspace drawer. A touch client, such as a
phone, keeps the classic drawers with the flag on, as every client does with the flag off;
there the tabs code never loads.

The flag is turned on by answering ``/v1/info`` with it added to the server's own features;
the server side of the flag is covered by ``tests/server/test_feature_flags.py``. Pull
requests come from the same stubs as ``test_github_tab.py``, so no ``gh`` runs. The Changes
journey runs on its own server and host, with ``workspace_api_server_access`` on, so its Git
reads go through the workspace API; there, Files shows a chain of only-child folders as one row
before any of them is opened. A reopened shell tab says it's connecting until the session
lists its shell, then shows its terminal.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, Request, Route, expect

from tests._helpers.polling import poll_until
from tests._helpers.pull_request_ui import open_session_pull_requests
from tests._helpers.server_runner import server_runner
from tests.e2e_ui.conftest import _BUILD_OUTPUT, open_right_rail
from tests.e2e_ui.files.test_file_autosave import (
    _cleanup_session_workdir,
    _seed_file,
    _wait_for_persisted,
)
from tests.e2e_ui.github.test_github_tab import _PR_NUMBER, _stub_github

_VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    # Below the shell's 1181px breakpoint a desktop window shows the sidebar as a drawer.
    "narrow-desktop": {"width": 700, "height": 900},
    "phone": {"width": 390, "height": 844},
}
# The tabs sidebar's lazily loaded chunks, including its ⌘K rows; the classic sidebar and
# touch and native mobile clients never request them.
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

    # The PR's page and ⌘K both show the session's list in a tab of its own.
    list_tab = sidebar.get_by_role("tab", name=re.compile(r"^Pull requests"))
    sidebar.get_by_role("button", name="All pull requests").click()
    expect(list_tab).to_have_attribute("aria-selected", "true", timeout=30_000)
    pr_tab.click()
    page.keyboard.press("ControlOrMeta+k")
    palette = page.get_by_test_id("command-palette-input")
    expect(palette).to_be_focused(timeout=10_000)
    palette.fill("Show all pull requests")
    page.get_by_role("option", name=re.compile("Show all pull requests")).click()
    expect(list_tab).to_have_attribute("aria-selected", "true")
    expect(list_tab).to_have_count(1)

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


def test_all_tabs_groups_the_tabs_and_closes_them_by_row_and_by_group(
    page: Page, workspace_session: tuple[str, str]
) -> None:
    """A file tab, a PR tab, then a new tab → All tabs lists them under Pull requests, Files
    and Other, the new one marked Current → ⌘⌫ closes the file → Close all closes the PR's tab.
    """
    base_url, session_id = workspace_session
    _stub_github(page)
    _set_flag(page, True)
    _open_session(page, base_url, session_id, "desktop")
    open_right_rail(page)
    sidebar = page.get_by_role("complementary", name="Workspace")
    expect(sidebar.get_by_role("group", name="Workspace history")).to_be_visible(timeout=60_000)

    _open_app(page, sidebar, "Files")
    sidebar.get_by_role("treeitem", name=re.compile(re.escape(_FILE))).click()
    expect(sidebar.locator(".cm-content")).to_contain_text("print('hello')", timeout=30_000)
    file_tab = sidebar.get_by_role("tab", name=re.compile(rf"^{re.escape(_FILE)}"))
    _open_app(page, sidebar, "Pull Requests")
    sidebar.get_by_role("button", name=re.compile(rf"#{_PR_NUMBER}\b")).first.click()
    pr_tab = sidebar.get_by_role("tab", name=re.compile(rf"#{_PR_NUMBER}"))
    expect(pr_tab).to_have_attribute("aria-selected", "true", timeout=30_000)
    sidebar.get_by_role("button", name="New tab", exact=True).click()
    expect(sidebar.get_by_role("combobox", name="Open an app in this tab")).to_be_focused()

    sidebar.get_by_role("button", name="All tabs").click()
    tab_list = page.get_by_role("listbox", name="Search tabs")
    other = tab_list.get_by_role("group", name=re.compile(r"^Other"))
    expect(other.get_by_role("option", name=re.compile("Current"))).to_have_count(1)
    files = tab_list.get_by_role("group", name=re.compile(r"^Files 1"))
    file_row = files.get_by_role("option", name=re.compile(re.escape(_FILE)))
    expect(file_row).to_be_visible()
    pulls = tab_list.get_by_role("group", name=re.compile(r"^Pull requests 1"))
    pr_row = pulls.get_by_role("option", name=re.compile(rf"#{_PR_NUMBER} {_PR_TITLE}"))
    expect(pr_row).to_contain_text("octocat")
    expect(pr_row).to_contain_text("Checks failing")

    # The pointer picks the file's row; the query keeps the keys, and ⌘⌫ closes that tab.
    file_row.hover()
    expect(file_row).to_have_attribute("aria-selected", "true")
    page.keyboard.press("ControlOrMeta+Backspace")
    expect(file_tab).to_have_count(0)
    expect(files).to_have_count(0)

    page.get_by_role("button", name="Close all pull request tabs").click()
    expect(pr_tab).to_have_count(0)
    expect(pulls).to_have_count(0)
    # The list stays open on what's left, with the new tab on screen.
    expect(other.get_by_role("option", name=re.compile("Current"))).to_have_count(1)


def test_files_menu_copies_the_path_and_a_link_to_the_file(
    page: Page, workspace_session: tuple[str, str]
) -> None:
    """A Files row's menu puts the file's path, then a link into the session, on the clipboard."""
    base_url, session_id = workspace_session
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    _set_flag(page, True)
    _open_session(page, base_url, session_id, "desktop")
    open_right_rail(page)
    sidebar = page.get_by_role("complementary", name="Workspace")
    expect(sidebar.get_by_role("group", name="Workspace history")).to_be_visible(timeout=60_000)
    _open_app(page, sidebar, "Files")
    file_row = sidebar.get_by_role("treeitem", name=re.compile(re.escape(_FILE)))
    expect(file_row).to_be_visible(timeout=30_000)

    file_row.click(button="right")
    page.get_by_role("menuitem", name="Copy path", exact=True).click()
    expect(page.get_by_text("Copied path", exact=True)).to_be_visible(timeout=10_000)
    copied = page.evaluate("() => navigator.clipboard.readText()")
    assert copied.startswith("/") and copied.endswith(f"/{_FILE}"), copied

    file_row.click(button="right")
    page.get_by_role("menuitem", name="Copy link to file", exact=True).click()
    expect(page.get_by_text("Link copied", exact=True)).to_be_visible(timeout=10_000)
    link = page.evaluate("() => navigator.clipboard.readText()")
    assert re.search(rf"/c/{re.escape(session_id)}\?(?:.*&)?file={re.escape(_FILE)}", link), link


def test_file_link_opens_its_comment_in_a_full_screen_sidebar(
    page: Page, workspace_session: tuple[str, str]
) -> None:
    """A ``?file=`` link with ``comment`` and ``fullscreen=1`` shows that thread, full screen."""
    base_url, session_id = workspace_session
    anchor = "hello"
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/comments",
        json={
            "path": _FILE,
            "body": "Say goodbye too",
            "start_index": "print('hello')".index(anchor),
            "end_index": "print('hello')".index(anchor) + len(anchor),
            "anchor_content": anchor,
        },
        timeout=10.0,
    )
    response.raise_for_status()
    comment_id = response.json()["id"]
    _set_flag(page, True)
    page.set_viewport_size(_VIEWPORTS["desktop"])
    page.goto(f"{base_url}/c/{session_id}?file={_FILE}&comment={comment_id}&fullscreen=1")

    sidebar = page.get_by_role("complementary", name="Workspace")
    viewer = sidebar.get_by_test_id("file-viewer")
    expect(viewer).to_contain_text("Say goodbye too", timeout=60_000)
    expect(page.locator("[data-maximized]")).to_have_count(1)
    # The link is handed to the tab, so a reload doesn't open it again.
    expect(page).not_to_have_url(re.compile(r"[?&](?:file|comment|fullscreen)="))


def test_tabs_sidebar_shell_tab_connects_then_shows_its_terminal(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """A reopened shell tab spins and says Connecting… until its shell is listed, then shows it."""
    base_url, session_id = terminal_session
    _set_flag(page, True)
    _open_session(page, base_url, session_id, "desktop")
    open_right_rail(page)
    sidebar = page.get_by_role("complementary", name="Workspace")
    expect(sidebar.get_by_role("group", name="Workspace history")).to_be_visible(timeout=60_000)
    _open_app(page, sidebar, "Terminal")
    terminal = sidebar.get_by_test_id("terminal-view").last
    expect(terminal).to_have_attribute("data-state", "connected", timeout=60_000)

    # Reopen the session while its shell list is held: the restored tab can't know its shell yet.
    # The page's clock stands still meanwhile, so the list read's deadline can't fire however
    # long the hold takes.
    held: list[Route] = []
    holding = True

    def hold_list(route: Route) -> None:
        if holding and route.request.method == "GET":
            held.append(route)
        else:
            route.continue_()

    page.route(re.compile(r"/v1/sessions/[^/]+/resources/terminals(?:\?.*)?$"), hold_list)
    page.clock.install()
    page.clock.pause_at(datetime.now(UTC) + timedelta(seconds=1))
    try:
        page.reload()
        open_right_rail(page)
        connecting = sidebar.get_by_test_id("terminal-tab-connecting")
        expect(connecting).to_contain_text("Connecting…", timeout=60_000)
        selected = sidebar.get_by_role("tab", selected=True)
        expect(selected.get_by_role("status", name="Loading")).to_be_visible()
        expect(sidebar.get_by_test_id("terminal-view")).to_have_count(0)
        assert held, "the shell list was never requested"
    finally:
        page.clock.resume()
        holding = False
        for route in held:
            route.continue_()

    expect(terminal).to_have_attribute("data-state", "connected", timeout=30_000)
    expect(connecting).to_have_count(0)
    expect(selected.get_by_role("status", name="Loading")).to_have_count(0)


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


def _commit(repository: Path, path: str, subject: str, extra_lines: int = 0) -> None:
    (repository / path).write_text(
        f"{subject}\n" + "".join(f"line {index}\n" for index in range(extra_lines))
    )
    for args in (("add", path), ("commit", "-q", "-m", subject)):
        subprocess.run(
            ["git", "-c", "user.name=Example", "-c", "user.email=dev@example.com", *args],
            cwd=repository,
            check=True,
        )


@pytest.fixture
def git_session(built_spa: None, tmp_path: Path) -> Iterator[tuple[str, str]]:
    """A session on a real host whose workspace is a repository on a feature branch.

    ``main`` has one commit, tagged ``v1.0``, and is ``origin``'s default branch;
    ``feature`` adds two more, the last with the longer file.
    """
    repository = tmp_path / "repository"
    repository.mkdir()
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
    # Otherwise the host's untracked-cache probe briefly writes mtime-test-*/ files into the
    # repository, and a listing that lands then shows a file that's gone when its diff loads.
    subprocess.run(["git", "config", "core.untrackedCache", "true"], cwd=repository, check=True)
    _commit(repository, "base.txt", "Add the base")
    subprocess.run(["git", "tag", "v1.0"], cwd=repository, check=True)
    # All branch changes compares with the merge base of origin's default branch.
    for args in (
        ("remote", "add", "origin", str(origin)),
        ("push", "-q", "origin", "main"),
        ("remote", "set-head", "origin", "main"),
    ):
        subprocess.run(["git", *args], cwd=repository, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repository, check=True)
    _commit(repository, "feature_first.txt", "Add the first feature file")
    _commit(repository, "feature_last.txt", "Add the last feature file", extra_lines=5)
    server_env = {
        "OMNIGENT_FEATURES": "workspace_api_server_access",
        "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
    }
    with (
        server_runner(tmp_path / "stack", server_env=server_env) as stack,
        httpx.Client(base_url=stack.base_url, timeout=60.0, trust_env=False) as client,
    ):
        stack.start_host()
        agents = client.get("/v1/agents", params={"limit": 100})
        agents.raise_for_status()
        # An SDK agent needs no CLI on the host, and no turn runs here.
        agent_id = next(
            agent["id"] for agent in agents.json()["data"] if agent.get("harness") == "claude-sdk"
        )
        created = client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_id,
                "host_id": stack.host_id,
                "workspace": str(repository),
            },
        )
        created.raise_for_status()
        yield stack.base_url, created.json()["id"]


def test_changes_compare_branches_and_tags_through_the_workspace_api(
    page: Page, git_session: tuple[str, str]
) -> None:
    """The picker suggests branches and tags; each comparison lists its files and is a step.

    Diff options then sorts the files by size, and the order outlasts a reload.
    """
    base_url, session_id = git_session
    _set_flag(page, True)
    _open_session(page, base_url, session_id, "desktop")
    open_right_rail(page)
    sidebar = page.get_by_role("complementary", name="Workspace")
    expect(sidebar.get_by_role("group", name="Workspace history")).to_be_visible(timeout=60_000)
    _open_app(page, sidebar, "Diffs")

    def compare(choice: str) -> Locator:
        sidebar.get_by_role("button", name="Change compared revisions").click()
        picker = page.get_by_role("dialog", name="Compared revisions")
        picker.get_by_role("group", name="Compare").get_by_role("button", name=choice).click()
        expect(picker).to_have_count(0)
        return sidebar.get_by_role("button", name="Change compared revisions")

    def changed(name: str) -> Locator:
        return sidebar.get_by_role("button", name=f"Copy path {name}", exact=True)

    # The branch's two commits, against where it left main, each with its diff.
    expect(compare("All branch changes")).to_contain_text("All branch changes", timeout=30_000)
    for name in ("feature_first.txt", "feature_last.txt"):
        expect(changed(name)).to_be_visible(timeout=30_000)
    expect(changed("base.txt")).to_have_count(0)
    expect(sidebar.get_by_text("Add the first feature file", exact=True)).to_be_visible(
        timeout=30_000
    )
    expect(sidebar.get_by_text("Couldn't load this diff.", exact=False)).to_have_count(0)

    # Base and Head suggest the repository's branches and tag.
    sidebar.get_by_role("button", name="Change compared revisions").click()
    picker = page.get_by_role("dialog", name="Compared revisions")
    expect(picker.get_by_role("combobox", name="Base revision")).to_be_visible()
    suggested = picker.locator("datalist").first
    for name in ("feature", "main", "v1.0"):
        expect(suggested.locator(f'option[value="{name}"]')).to_have_count(1, timeout=30_000)
    page.keyboard.press("Escape")

    # The last commit alone.
    expect(compare("Last commit")).to_contain_text("Last commit", timeout=30_000)
    expect(changed("feature_last.txt")).to_be_visible(timeout=30_000)
    expect(changed("feature_first.txt")).to_have_count(0)
    expect(sidebar.get_by_text("Add the last feature file", exact=True)).to_be_visible(
        timeout=30_000
    )
    expect(sidebar.get_by_text("Not available on this host yet.")).to_have_count(0)

    # Each pick is a step: Back shows All branch changes again, and Forward the last commit.
    history = sidebar.get_by_role("group", name="Workspace history")
    trigger = sidebar.get_by_role("button", name="Change compared revisions")
    history.get_by_role("button", name="Back").click()
    expect(trigger).to_contain_text("All branch changes", timeout=30_000)
    expect(changed("feature_first.txt")).to_be_visible(timeout=30_000)
    history.get_by_role("button", name="Forward").click()
    expect(trigger).to_contain_text("Last commit", timeout=30_000)
    expect(changed("feature_first.txt")).to_have_count(0, timeout=30_000)

    # A reload keeps the visit and what it compares.
    page.reload()
    open_right_rail(page)
    expect(trigger).to_contain_text("Last commit", timeout=60_000)
    history.get_by_role("button", name="Back").click()
    expect(trigger).to_contain_text("All branch changes", timeout=30_000)

    # Diff options sorts by size: the longer file moves above the one before it by path.
    def shows_above(name: str, other: str) -> bool:
        first = changed(name).bounding_box()
        second = changed(other).bounding_box()
        return first is not None and second is not None and first["y"] < second["y"]

    def expect_first(name: str, other: str) -> None:
        try:
            poll_until(lambda: shows_above(name, other), timeout_s=10, interval_s=0.1)
        except AssertionError:
            raise AssertionError(f"{name}'s diff never showed above {other}'s") from None

    expect_first("feature_first.txt", "feature_last.txt")
    sidebar.get_by_role("button", name="Diff options").click()
    page.get_by_role("menuitemradio", name="By size, largest first").click()
    expect(page.get_by_role("menu", name="Diff options")).to_have_count(0)
    expect_first("feature_last.txt", "feature_first.txt")
    page.reload()
    open_right_rail(page)
    expect(changed("feature_first.txt")).to_be_visible(timeout=60_000)
    expect_first("feature_last.txt", "feature_first.txt")


def test_files_shows_a_folder_chain_as_one_row_through_the_workspace_api(
    page: Page, git_session: tuple[str, str], tmp_path: Path
) -> None:
    """The host reports ``com/example/app`` with the root listing, so no folder needs opening."""
    base_url, session_id = git_session
    chain = tmp_path / "repository" / "com" / "example" / "app"
    chain.mkdir(parents=True)
    (chain / "Main.java").write_text("class Main {}\n")
    _set_flag(page, True)
    _open_session(page, base_url, session_id, "desktop")
    open_right_rail(page)
    sidebar = page.get_by_role("complementary", name="Workspace")
    # The tabs sidebar loads lazily; a cold start can outlast the click's default timeout.
    expect(sidebar.get_by_role("group", name="Workspace history")).to_be_visible(timeout=60_000)
    _open_app(page, sidebar, "Files")

    merged = sidebar.get_by_role("treeitem", name="com / example / app", exact=True)
    expect(merged).to_be_visible(timeout=30_000)
    expect(sidebar.get_by_role("treeitem", name="com", exact=True)).to_have_count(0)
    merged.click()
    expect(sidebar.get_by_role("treeitem", name="Main.java", exact=True)).to_be_visible(
        timeout=15_000
    )


def test_files_lists_git_ignored_files_marked_as_ignored_only_once_switched_on(
    page: Page, git_session: tuple[str, str], tmp_path: Path
) -> None:
    """Files leaves out what Git ignores; the options switch lists it, marked, until it's off."""
    base_url, session_id = git_session
    repository = tmp_path / "repository"
    (repository / ".gitignore").write_text("build/\n")
    (repository / "build").mkdir()
    (repository / "build" / "app.bin").write_text("x")
    _set_flag(page, True)
    _open_session(page, base_url, session_id, "desktop")
    open_right_rail(page)
    sidebar = page.get_by_role("complementary", name="Workspace")
    expect(sidebar.get_by_role("group", name="Workspace history")).to_be_visible(timeout=60_000)
    _open_app(page, sidebar, "Files")
    # A full-screen workspace docks the tree, with its options beside the toggle.
    page.get_by_role("button", name="Full screen", exact=True).click()
    build = sidebar.get_by_role("treeitem", name=re.compile(r"^build\b"))
    expect(sidebar.get_by_role("treeitem", name="base.txt", exact=True)).to_be_visible(
        timeout=30_000
    )
    expect(build).to_have_count(0)

    def switch_ignored_files() -> None:
        sidebar.get_by_role("button", name="Files tree options", exact=True).click()
        page.get_by_role("menuitemcheckbox", name="Show git-ignored files").click()

    switch_ignored_files()
    expect(build).to_have_attribute("data-item-git-status", "ignored", timeout=30_000)
    expect(sidebar.get_by_role("treeitem", name="base.txt", exact=True)).not_to_have_attribute(
        "data-item-git-status", "ignored"
    )

    switch_ignored_files()
    expect(build).to_have_count(0, timeout=30_000)
