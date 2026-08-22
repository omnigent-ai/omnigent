"""E2E: the Files rail "Working folder" header — never a collapse toggle, and
a browse control when the session has somewhere else to go.

The desktop Workspace rail renders ``FilesPanel`` in its ``frameless``
(inline) mode. Three behaviours are pinned here:

1. The header never collapses the panel. The file list is the whole point of
   the panel, so there is nothing to collapse to. This guards against
   reintroducing the collapse chevron: the header once doubled as a collapse
   toggle carrying ``aria-expanded``, which made no sense here. A session with
   nowhere else to browse (no bound host) keeps the plain, unclickable label.
2. The header's eye shows hidden files by default and reads as state: plain
   while dot-prefixed entries are listed, slashed once they are filtered out.
3. When the session IS host-bound and unconfined, the working-folder path
   becomes a button that opens the workspace directory browser, and picking a
   directory re-roots the file tree there — through the real server, runner,
   and filesystem, not a stub.
4. Opening that browser does not flash a tooltip over the listing: the
   popover focuses its first icon button, and only a hover may label it.

None of them send a message — the header is rail state, not a function of any
turn — so they all stay fast, LLM-free checks.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import open_right_rail


def test_files_rail_working_folder_header_is_a_static_label(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The rail's "Working folder" header is a static label (no toggle button),
    and the file list is always visible."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    # The rail defaults open but is remembered per session; ensure it is open
    # so the Files panel header below is reachable. Scope every lookup to the
    # desktop "Workspace" rail so it never matches the hidden mobile drawer
    # that mirrors the same markup.
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    # Files is the default rail tab; click it explicitly so the assertion does
    # not depend on the remembered tab from a prior session.
    rail.get_by_role("tab", name=re.compile("^Files")).click()

    # The header text is present, but it is NOT a button — there is no collapse
    # toggle. substring-matching "Working folder" tolerates the trailing
    # working-directory basename the header also renders.
    expect(rail.get_by_text("Working folder")).to_be_visible(timeout=30_000)
    expect(rail.get_by_role("button", name=re.compile("Working folder"))).to_have_count(0)

    # The content is always shown: the Files (tree) search box is visible with
    # no toggle needed. Scope is the rail tab now (Files vs Changes), not an
    # in-panel switch.
    expect(rail.get_by_role("searchbox", name="Search all files")).to_be_visible()


def test_files_rail_shows_hidden_files_until_the_eye_is_clicked(
    page: Page,
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Dot-prefixed entries are listed without touching the eye, and the eye
    reports the current state rather than the pending action: unslashed while
    hidden files are visible, slashed once they are filtered out.

    Agents write to ``.claude/``, ``.github/`` and friends constantly, so a
    workspace whose dotfiles are invisible by default hides much of what a
    turn actually changed.
    """
    base_url, session_id = seeded_session

    env = page.request.get(f"{base_url}/v1/sessions/{session_id}/resources/environments/default")
    assert env.status == 200, env.text()
    root = Path(env.json()["metadata"]["root"])
    hidden = root / ".hidden-demo"
    hidden.mkdir(parents=True, exist_ok=True)
    (hidden / "inside.txt").write_text("proof the dot-directory listed\n")
    # The workspace is a real checkout shared with every other test in the
    # shard, so this fixture directory must not outlive the test.
    request.addfinalizer(lambda: shutil.rmtree(hidden, ignore_errors=True))

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()

    row = rail.get_by_role("button", name=".hidden-demo/", exact=True)
    expect(row).to_be_visible(timeout=30_000)

    # Visible state → plain eye. The class regex is anchored on whitespace
    # because "lucide-eye" is a prefix of "lucide-eye-off".
    toggle = rail.get_by_role("button", name="Hide hidden files")
    expect(toggle.locator("svg")).to_have_class(re.compile(r"(^|\s)lucide-eye(\s|$)"))

    toggle.click()

    expect(row).to_have_count(0)
    expect(rail.get_by_role("button", name="Show hidden files").locator("svg")).to_have_class(
        re.compile(r"(^|\s)lucide-eye-off(\s|$)")
    )


def test_files_panel_double_click_opens_a_folder_as_the_working_folder(
    page: Page,
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
) -> None:
    """Double-clicking a folder in the tree re-roots the panel onto it.

    Finder's contract, driven through the real server, runner, and filesystem:
    the panel asks for the folder, the runner lists it, and the tree redraws at
    the new root. A single click must still only expand the row, or the tree
    would be unusable.

    The wire form this navigation uses (relative, so a non-owner is not refused)
    is pinned separately, by the collaborator test in
    ``collaboration/test_browse_outside_workspace.py`` -- an owner is allowed
    either form, so this test cannot tell them apart.
    """
    base_url, session_id = seeded_session

    env = page.request.get(f"{base_url}/v1/sessions/{session_id}/resources/environments/default")
    assert env.status == 200, env.text()
    root = Path(env.json()["metadata"]["root"])
    folder = root / "double-click-demo"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "inside.txt").write_text("proof the tree re-rooted\n")
    # The workspace is a real checkout shared with every other test in the
    # shard, so this fixture directory must not outlive the test.
    request.addfinalizer(lambda: shutil.rmtree(folder, ignore_errors=True))

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()

    row = rail.get_by_role("button", name="double-click-demo/", exact=True)
    expect(row).to_be_visible(timeout=30_000)

    # Single click = expand in place. The row stays, so we are still rooted at
    # the workspace. Click again to collapse, leaving the pre-double-click
    # state identical to the pre-single-click one.
    row.click()
    expect(row).to_have_attribute("aria-expanded", "true")
    row.click()
    expect(row).to_have_attribute("aria-expanded", "false")

    row.dblclick()

    # Re-rooted: the folder's own contents are the top level now, the folder
    # row is gone, and the header names where we are.
    expect(rail.get_by_text("inside.txt")).to_be_visible(timeout=30_000)
    expect(row).to_have_count(0)
    expect(rail.get_by_text("double-click-demo")).to_be_visible()


_FAKE_HOST_ID = "host_files_panel_browse"


@pytest.fixture
def _drop_routes(page: Page) -> Iterator[None]:
    """Drop this module's route handlers before the page closes.

    A handler that calls ``route.fetch()`` as the page tears down raises
    ``TargetClosedError``, which would surface as an error in the NEXT test's
    setup. Mirrors the same guard in ``sessions/test_host_badge.py``.

    :param page: Playwright page fixture.
    :returns: Iterator yielding once, then unrouting.
    """
    yield
    page.unroute_all(behavior="ignoreErrors")


def _bind_host_with_listing(page: Page, session_id: str, *, entry: Path) -> None:
    """Patch the browser's view of ``session_id`` into a host-bound shape and
    stub the host's directory listing to offer exactly ``entry``.

    Only the HOST binding is faked. The session's reach still comes from the
    real server (the e2e agent declares ``sandbox: {type: none}``, so it
    reports ``unconfined``), and the re-rooted file tree is served by the real
    runner off the real filesystem — so this exercises the absolute-path
    browse end to end rather than asserting against a mock.

    :param page: Playwright page, before navigation.
    :param session_id: Session to patch, e.g. ``"conv_abc123"``.
    :param entry: Real directory the stubbed listing offers as its one
        navigable entry.
    """

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        payload["host_id"] = _FAKE_HOST_ID
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_hosts(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/v1/hosts":
            route.continue_()
            return
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {"hosts": [{"host_id": _FAKE_HOST_ID, "name": "e2e-host", "status": "online"}]}
            ),
        )

    def _patch_host_fs(route: Route) -> None:
        # Every directory the picker opens answers with the same single entry,
        # so the test never depends on the runner's real cwd layout.
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "name": entry.name,
                            "path": str(entry),
                            "type": "directory",
                            "bytes": None,
                            "modified_at": None,
                        }
                    ],
                    "has_more": False,
                }
            ),
        )

    # Regex, not glob: these URLs carry query strings (the session snapshot is
    # fetched with ``?refresh_state=true``, the listing with ``?limit=``), and a
    # "**/path" glob does not match a URL with a query.
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route(re.compile(r"/v1/hosts(\?|$)"), _patch_hosts)
    page.route(re.compile(rf"/v1/hosts/{re.escape(_FAKE_HOST_ID)}/filesystem"), _patch_host_fs)


def test_files_panel_browses_to_a_directory_outside_the_workspace(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    _drop_routes: None,
) -> None:
    """The working-folder path opens the directory browser, and picking a
    directory re-roots the file tree there — including outside the workspace.

    The sentinel file lives in a directory the session did NOT start in, so
    seeing it in the tree proves the whole path worked: the panel asked for an
    absolute location, the server authorized it, and the runner listed it.
    """
    base_url, session_id = seeded_session

    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    (outside / "sentinel.txt").write_text("proof the tree re-rooted\n")

    _bind_host_with_listing(page, session_id, entry=outside)
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    # The Files tab is the folder tree (not the changed-files list) -- the
    # tree is what re-roots.
    rail.get_by_role("tab", name=re.compile("^Files")).click()

    # The header path is the control: it shows where we are and opens the
    # browser. (The test above is this same header with the host binding
    # absent, where it stays a plain, unclickable label.)
    path_button = rail.get_by_test_id("browse-location-path")
    expect(path_button).to_be_visible(timeout=30_000)
    path_button.click()

    # Same directory browser the new-session flow uses to pick a workspace.
    picker = page.get_by_test_id("workspace-picker")
    expect(picker).to_be_visible(timeout=15_000)
    picker.get_by_test_id(f"workspace-picker-entry-{outside.name}").click()

    # Re-rooted: the header names the new directory and the tree lists its
    # contents, served by the real runner from outside the workspace.
    expect(path_button).to_contain_text(outside.name, timeout=30_000)
    expect(rail.get_by_text("sentinel.txt")).to_be_visible(timeout=30_000)

    # Search follows the tree. A tree showing one directory while search
    # reports on another is the incoherence this feature exists to avoid, so
    # the search route gets the same absolute scoping (and its own code path
    # on the server).
    page.keyboard.press("Escape")
    rail.get_by_role("searchbox", name="Search all files").fill("sentinel")
    expect(rail.get_by_text("sentinel.txt")).to_be_visible(timeout=30_000)
    rail.get_by_role("searchbox", name="Search all files").fill("")

    # One click back to where the agent actually is.
    path_button.click()
    page.get_by_test_id("workspace-picker-workspace").click()
    expect(rail.get_by_text("sentinel.txt")).to_have_count(0, timeout=30_000)


def test_opening_the_directory_browser_does_not_flash_a_header_tooltip(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    _drop_routes: None,
) -> None:
    """Opening the browser leaves its header tooltips hidden; hovering an icon
    still shows one.

    The picker's popover focuses its first tabbable child — the Up button — so
    a tooltip that opens on any focus would cover the listing the click was
    meant to reveal. Only a deliberate hover should surface the label, which is
    a real-browser distinction (focus rings and hover delays), hence an e2e
    test rather than a jsdom one.
    """
    base_url, session_id = seeded_session

    outside = tmp_path / "outside-workspace"
    outside.mkdir()

    _bind_host_with_listing(page, session_id, entry=outside)
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()

    path_button = rail.get_by_test_id("browse-location-path")
    expect(path_button).to_be_visible(timeout=30_000)
    path_button.click()

    picker = page.get_by_test_id("workspace-picker")
    expect(picker).to_be_visible(timeout=15_000)

    # The focus that used to trigger the tooltip has landed, so a tooltip that
    # opens on focus would already be on screen. Matching on text (the button
    # carries the same string as its aria-label) finds only the tooltip.
    up = picker.get_by_test_id("workspace-picker-up")
    expect(up).to_be_focused()
    expect(page.get_by_text("Up one level", exact=True)).to_have_count(0)

    # A deliberate hover is still answered.
    up.hover()
    expect(page.get_by_text("Up one level", exact=True)).to_be_visible(timeout=15_000)
