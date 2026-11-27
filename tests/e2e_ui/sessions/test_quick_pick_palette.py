"""E2E: the ``quick_pick_palette`` flag picks which ⌘K palette the app shows.

With the flag off, ⌘/Ctrl+K opens today's cmdk palette. With it on, the same
chord opens the QuickPick palette, which lists the same Actions, finds one from
its initials, reopens empty after Escape and runs the row with Enter.
Session rows, search groups and paging are covered by the shared Vitest cases
that run against both palettes (``CommandPalette.quickPick.test.tsx``).

On an open session, the QuickPick palette also previews themes as the user moves
through Select theme…, hands focus to the session switcher and back, and runs
the session's own actions. ⌘↵ on another session's row lists that session's
actions, which archive it without leaving the open one, and a verb-first row
such as Archive a Session… asks which session first. On the Inbox, Mark all as
read clears an unread session and tells the server, and the Inbox filter setting
picks a tab.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests._helpers.compat import (
    COMPAT_SERVER_VERSION_ENV,
    compat_server_cwd,
    compat_server_python,
    meets_min_server_version,
    server_executable,
    server_pythonpath,
)
from tests._helpers.live_server import isolated_local_server
from tests._helpers.polling import poll_until
from tests.e2e_ui._claude_sdk_session import create_claude_sdk_session
from tests.e2e_ui._quick_pick_palette import (
    enable_quick_pick_palette,
    open_quick_pick_palette,
    run_palette_command,
)
from tests.e2e_ui.conftest import (
    _BUILD_OUTPUT,
    _REPO_ROOT,
    _ensure_runner_online,
    _server_state,
)
from tests.e2e_ui.sessions._sidebar_helpers import (
    patch_session,
    session_archived,
    sidebar_row,
    unread_dot,
)
from tests.e2e_ui.sessions.test_pinned_session_hotkeys import _pin, _pinned_slot_ids

_PALETTE_INPUT = "command-palette-input"
# "Go to Settings" navigates to /settings, which redirects to the general section.
_SETTINGS_URL_SUFFIX = "/settings/general"
# First release whose server recognizes the quick_pick_palette feature name.
_FLAG_MIN_SERVER_VERSION = "0.23.0"


def _palette_server(directory: Path, features: str) -> Iterator[str]:
    with isolated_local_server(
        directory,
        env={
            "OMNIGENT_FEATURES": features,
            "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
            "PYTHONPATH": server_pythonpath(_REPO_ROOT),
        },
        executable=server_executable(),
        cwd=compat_server_cwd(),
        health_timeout=180.0,
    ) as base_url:
        yield base_url


@pytest.fixture(scope="module")
def quick_pick_server(built_spa: None, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A server with ``OMNIGENT_FEATURES=quick_pick_palette``."""
    compat_version = os.environ.get(COMPAT_SERVER_VERSION_ENV) or "0"
    if compat_server_python() is not None and not meets_min_server_version(
        compat_version, _FLAG_MIN_SERVER_VERSION
    ):
        pytest.skip(f"pinned server {compat_version} predates the quick_pick_palette flag")
    yield from _palette_server(tmp_path_factory.mktemp("quick_pick_on"), "quick_pick_palette")


@pytest.fixture(scope="module")
def default_palette_server(
    built_spa: None, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    """A server with no release features beyond the defaults."""
    yield from _palette_server(tmp_path_factory.mktemp("quick_pick_off"), "")


def test_flag_off_keeps_the_current_palette(page: Page, default_palette_server: str) -> None:
    """Without the flag, ⌘K opens the cmdk palette and runs an action as before."""
    page.goto(f"{default_palette_server}/")
    expect(page.get_by_role("heading", name="What should we build?")).to_be_visible(timeout=30_000)
    page.keyboard.press("ControlOrMeta+k")
    palette_input = page.get_by_test_id(_PALETTE_INPUT)
    expect(palette_input).to_be_focused(timeout=10_000)
    dialog = page.get_by_role("dialog")
    expect(dialog.locator("[cmdk-root]")).to_have_count(1)
    expect(dialog.locator('[data-slot="quick-pick"]')).to_have_count(0)

    palette_input.fill("settings")
    page.keyboard.press("Enter")
    expect(page).to_have_url(f"{default_palette_server}{_SETTINGS_URL_SUFFIX}", timeout=10_000)


def test_flag_on_runs_the_quick_pick_palette(page: Page, quick_pick_server: str) -> None:
    """With the flag, ⌘K opens QuickPick; initials, Escape and Enter work."""
    page.goto(f"{quick_pick_server}/")
    expect(page.get_by_role("heading", name="What should we build?")).to_be_visible(timeout=30_000)
    page.keyboard.press("ControlOrMeta+k")
    palette_input = page.get_by_role("combobox", name="Command palette")
    expect(palette_input).to_be_focused(timeout=10_000)
    dialog = page.get_by_role("dialog")
    expect(dialog.locator('[data-slot="quick-pick"]')).to_have_count(1)
    expect(dialog.locator("[cmdk-root]")).to_have_count(0)
    actions = dialog.get_by_role("group", name="Actions")
    expect(actions.get_by_role("option", name="Go to Settings")).to_be_visible()

    # Initials find the action that today's substring filter would miss.
    palette_input.fill("gts")
    first = actions.get_by_role("option").first
    expect(first).to_contain_text("Go to Settings")
    expect(first.locator("mark").first).to_be_visible()
    expect(palette_input).to_have_attribute(
        "aria-activedescendant", first.get_attribute("id") or ""
    )

    # Escape closes it; reopening starts from an empty query.
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    page.keyboard.press("ControlOrMeta+k")
    expect(palette_input).to_have_value("")

    # Enter runs the top match and closes the palette.
    palette_input.fill("settings")
    page.keyboard.press("Enter")
    expect(page).to_have_url(f"{quick_pick_server}{_SETTINGS_URL_SUFFIX}", timeout=10_000)
    expect(page.get_by_test_id(_PALETTE_INPUT)).to_have_count(0)


def test_quick_pick_on_an_open_session(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    """Theme preview and selection, Go to Session focus, and a session action."""
    base_url, session_id = seeded_session
    page: Page = request.getfixturevalue("page")
    enable_quick_pick_palette(page)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible(timeout=30_000)
    root = page.locator("html")
    expect(root).not_to_have_attribute("data-theme", "dracula")
    original_theme = root.get_attribute("data-theme")

    # Moving through Select theme… previews each palette; Escape restores the original.
    run_palette_command(page, "select theme", "Select theme…")
    theme_input = page.get_by_role("combobox", name="Select theme")
    expect(theme_input).to_be_focused(timeout=10_000)
    theme_input.fill("dracula")
    expect(root).to_have_attribute("data-theme", "dracula")
    theme_input.fill("")
    theme_input.fill("nord")
    expect(root).to_have_attribute("data-theme", "nord")
    page.keyboard.press("Escape")
    expect(theme_input).to_have_count(0)
    if original_theme is None:
        # The default palette has no data-theme at all, so any value is a wrong restore.
        expect(root).not_to_have_attribute("data-theme", re.compile(".*"))
    else:
        expect(root).to_have_attribute("data-theme", original_theme)

    # Enter keeps the chosen palette, across a reload.
    run_palette_command(page, "select theme", "Select theme…")
    expect(theme_input).to_be_focused(timeout=10_000)
    theme_input.fill("nord")
    expect(root).to_have_attribute("data-theme", "nord")
    page.keyboard.press("Enter")
    expect(theme_input).to_have_count(0)
    expect(root).to_have_attribute("data-theme", "nord")
    page.reload()
    expect(composer).to_be_visible(timeout=30_000)
    expect(root).to_have_attribute("data-theme", "nord")

    # Go to Session… hands focus to the switcher, and Escape returns it to the composer.
    composer.focus()
    run_palette_command(page, "go to session", "Go to Session…")
    switcher = page.get_by_role("combobox", name="Switch session")
    expect(switcher).to_be_focused(timeout=10_000)
    page.keyboard.press("Escape")
    expect(switcher).to_have_count(0)
    expect(composer).to_be_focused()

    # The open session's actions run from ⌘K: Rename opens its dialog, focused on the name.
    run_palette_command(page, "rename session", "Rename session")
    rename = page.get_by_role("dialog", name="Rename session")
    expect(rename).to_be_visible(timeout=10_000)
    expect(rename.get_by_role("textbox", name="Session name")).to_be_focused()


def test_quick_pick_sets_the_open_sessions_reasoning_effort(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """⌘K's Reasoning Effort row opens its levels, and picking one changes the session."""
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    session_id: str | None = None
    try:
        session_id, _agent_name = create_claude_sdk_session(
            live_server,
            str(_server_state["runner_id"]),
            mock_llm_server_url,
            name_prefix="quick-pick-effort",
        )
        enable_quick_pick_palette(page)
        page.goto(f"{live_server}/c/{session_id}")
        expect(page.get_by_placeholder("Send a message…")).to_be_visible(timeout=30_000)
        expect(page.get_by_test_id("composer-config-gear")).to_be_enabled(timeout=30_000)

        run_palette_command(page, "reasoning effort", "Reasoning Effort")
        heading = page.locator('[data-slot="quick-pick-heading"]')
        expect(heading).to_have_text("Reasoning Effort")
        # The values page names its query after the setting, so find it by role alone.
        page.get_by_role("dialog").get_by_role("combobox").fill("max")
        expect(page.get_by_role("dialog").get_by_role("option").first).to_contain_text("Max")
        page.keyboard.press("Enter")
        expect(heading).to_have_count(0)

        def effort() -> object:
            response = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            response.raise_for_status()
            return response.json().get("reasoning_effort") == "max"

        poll_until(effort, timeout_s=15.0)
    finally:
        try:
            if session_id is not None:
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        finally:
            # A respawned runner must not outlive the test, even if the delete fails.
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


def test_quick_pick_archives_another_session_from_its_row(
    request: pytest.FixtureRequest, seeded_session_pair: tuple[str, str, str]
) -> None:
    """⌘↵ on another session's row lists its actions, and Archive archives that one."""
    base_url, open_id, other_id = seeded_session_pair
    patch_session(base_url, other_id, title="Kestrel notes")
    page: Page = request.getfixturevalue("page")
    enable_quick_pick_palette(page)
    page.goto(f"{base_url}/c/{open_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible(timeout=30_000)

    palette_input = open_quick_pick_palette(page)
    palette_input.fill("kestrel")
    dialog = page.get_by_role("dialog")
    sessions = dialog.get_by_role("group", name="Sessions")
    expect(sessions.get_by_role("option").first).to_contain_text("Kestrel notes")
    page.keyboard.press("ControlOrMeta+Enter")
    expect(dialog.locator('[data-slot="quick-pick-heading"]')).to_have_text(
        "Actions for “Kestrel notes”", timeout=10_000
    )
    # The actions page names its field for what it lists.
    actions_input = dialog.get_by_role("combobox", name="Session actions")
    expect(actions_input).to_be_focused()

    actions_input.fill("archive")
    expect(dialog.get_by_role("option").first).to_contain_text("Archive")
    page.keyboard.press("Enter")
    poll_until(lambda: session_archived(base_url, other_id), timeout_s=15.0)
    # The open session stays open; only the other one was archived.
    expect(page).to_have_url(f"{base_url}/c/{open_id}")
    assert not session_archived(base_url, open_id)


def _open_one_then_the_other(page: Page, base_url: str, first: str, second: str) -> None:
    """Open *first*, then *second* from its sidebar row, so *first* is the previous visit."""
    page.goto(f"{base_url}/c/{first}")
    expect(page.locator(f'li[data-sidebar-session-id="{second}"]').first).to_be_visible(
        timeout=30_000
    )
    page.locator(f'li[data-sidebar-session-id="{second}"] a').first.click()
    expect(page).to_have_url(re.compile(rf"/c/{second}$"), timeout=10_000)
    expect(page).to_have_title(re.compile("Kestrel"), timeout=10_000)


def test_quick_pick_archiving_the_open_session_opens_the_previous_visit(
    request: pytest.FixtureRequest, seeded_session_pair: tuple[str, str, str]
) -> None:
    """Archiving the open session opens the session ⌃[ would, not New session."""
    base_url, first, second = seeded_session_pair
    patch_session(base_url, first, title="Heron notes")
    patch_session(base_url, second, title="Kestrel notes")
    page: Page = request.getfixturevalue("page")
    enable_quick_pick_palette(page)
    _open_one_then_the_other(page, base_url, first, second)

    run_palette_command(page, "archive session", "Archive session", closes=True)
    poll_until(lambda: session_archived(base_url, second), timeout_s=15.0)
    expect(page).to_have_url(re.compile(rf"/c/{first}$"), timeout=10_000)
    expect(page).to_have_title(re.compile("Heron"), timeout=10_000)


def _viewer_unread(base_url: str, session_id: str) -> bool:
    """Read the server's ``viewer_unread`` for one session from ``GET /v1/sessions``."""
    response = httpx.get(f"{base_url}/v1/sessions", params={"visibility": "all"}, timeout=10.0)
    response.raise_for_status()
    for item in response.json()["data"]:
        if item["id"] == session_id:
            return bool(item.get("viewer_unread"))
    raise AssertionError(f"session {session_id} not present in /v1/sessions")


def test_quick_pick_marks_all_read_and_filters_the_inbox(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    """Mark all as read clears an unread dot and tells the server; the Inbox filter picks a tab."""
    base_url, session_id = seeded_session
    page: Page = request.getfixturevalue("page")
    enable_quick_pick_palette(page)
    page.goto(f"{base_url}/c/{session_id}")
    row = sidebar_row(page, session_id)
    expect(row).to_be_visible(timeout=30_000)

    def read_state_put(unread: bool):
        def matches(candidate) -> bool:
            return (
                candidate.request.method == "PUT"
                and candidate.url.endswith(f"/v1/sessions/{session_id}/read-state")
                and json.loads(candidate.request.post_data or "{}").get("unread") is unread
            )

        return matches

    row.hover()
    row.get_by_test_id("conversation-actions").click()
    # The dot lights at once; wait for the server's copy, so leaving the page can't cancel it.
    with page.expect_response(read_state_put(True), timeout=10_000):
        page.get_by_test_id("mark-unread-conversation").click()
    expect(unread_dot(row)).to_be_visible()

    # From the Inbox no session is open, and the sidebar row keeps its dot.
    page.goto(f"{base_url}/inbox")
    row = sidebar_row(page, session_id)
    expect(unread_dot(row)).to_be_visible(timeout=30_000)

    # The server holds the unread mark, so reading it back later shows Mark all's write.
    poll_until(lambda: _viewer_unread(base_url, session_id), timeout_s=10.0)

    palette_input = open_quick_pick_palette(page)
    palette_input.fill("mark all as read")
    # Wait for the list to settle: rows from before the query can linger for a moment.
    options = page.get_by_role("dialog").get_by_role("option")
    expect(options).to_have_count(1)
    expect(options.first).to_contain_text("Mark 1 unread session as read")
    expect(options.first).to_have_attribute("aria-selected", "true")
    with page.expect_response(read_state_put(False), timeout=10_000) as put:
        page.keyboard.press("Enter")
    assert put.value.status == 204
    expect(palette_input).to_have_count(0)
    expect(unread_dot(row)).to_have_count(0)
    poll_until(lambda: not _viewer_unread(base_url, session_id), timeout_s=10.0)

    expect(page.get_by_role("tab", name="All")).to_have_attribute("aria-selected", "true")
    run_palette_command(page, "inbox filter", "Inbox filter")
    heading = page.locator('[data-slot="quick-pick-heading"]')
    expect(heading).to_have_text("Inbox filter")
    page.get_by_role("dialog").get_by_role("combobox").fill("unread")
    expect(page.get_by_role("dialog").get_by_role("option").first).to_contain_text("Unread")
    page.keyboard.press("Enter")
    # Choosing a value returns to the command list; close it to see the page.
    expect(heading).to_have_count(0)
    page.keyboard.press("Escape")
    expect(page.get_by_role("combobox", name="Command palette")).to_have_count(0)
    expect(page.get_by_role("tab", name="Unread")).to_have_attribute(
        "aria-selected", "true", timeout=10_000
    )


@pytest.fixture
def project_names_to_delete() -> Iterator[list[tuple[str, str]]]:
    """(base URL, name) of each project a test makes or renames; deleted even when it fails."""
    tracked: list[tuple[str, str]] = []
    yield tracked
    for base_url in {base for base, _ in tracked}:
        names = {name for base, name in tracked if base == base_url}
        listed = httpx.get(f"{base_url}/v1/projects", timeout=10.0).json()["data"]
        for project in listed:
            if project["name"] in names:
                httpx.delete(f"{base_url}/v1/projects/{project['id']}", timeout=10.0)


@pytest.fixture
def restored_project_order(live_server: str) -> Iterator[None]:
    """Put the caller's project order and sort mode back after a test that moves projects."""
    url = f"{live_server}/v1/projects/order"
    saved = httpx.get(url, timeout=10.0).json()
    yield
    # Put back the remembered list too, or clear it, so the move's IDs don't outlive the test.
    remembered = saved["ordered_project_ids"] or []
    httpx.put(url, json={"ordered_project_ids": remembered}, timeout=10.0)
    if saved["sort_mode"] == "alphabetical":
        httpx.put(url, json={"ordered_project_ids": None}, timeout=10.0)


def _create_project(base_url: str, name: str, tracked: list[tuple[str, str]]) -> str:
    tracked.append((base_url, name))
    response = httpx.post(f"{base_url}/v1/projects", json={"name": name}, timeout=10.0)
    response.raise_for_status()
    return response.json()["id"]


def _project_names(base_url: str) -> set[str]:
    response = httpx.get(f"{base_url}/v1/projects", timeout=10.0)
    response.raise_for_status()
    return {project["name"] for project in response.json()["data"]}


def _project_folder(page: Page, name: str) -> Locator:
    """The sidebar folder header for project ``name``."""
    return page.locator('button[data-slot="context-menu-trigger"]').filter(has_text=name)


def test_quick_pick_renames_the_open_sessions_project(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    project_names_to_delete: list[tuple[str, str]],
) -> None:
    """⌘K names the open session's project and renames it with the folder menu's dialog."""
    base_url, session_id = seeded_session
    # The server is shared, so each run names its own project.
    name = f"Osprey {uuid.uuid4().hex[:6]}"
    project_id = _create_project(base_url, name, project_names_to_delete)
    patch_session(base_url, session_id, project_id=project_id)
    page: Page = request.getfixturevalue("page")
    enable_quick_pick_palette(page)
    page.goto(f"{base_url}/c/{session_id}")
    expect(_project_folder(page, name)).to_be_visible(timeout=30_000)

    run_palette_command(page, f"rename project {name}", f"Rename project {name}…")
    dialog = page.get_by_role("dialog", name="Rename project")
    name_input = dialog.locator("input")
    expect(name_input).to_be_focused(timeout=10_000)
    expect(name_input).to_have_value(name)
    renamed = f"{name} field notes"
    project_names_to_delete.append((base_url, renamed))
    name_input.fill(renamed)
    page.get_by_test_id("rename-project-confirm").click()
    expect(dialog).to_have_count(0)
    expect(_project_folder(page, renamed)).to_be_visible()
    poll_until(lambda: renamed in _project_names(base_url), timeout_s=15.0)


def test_quick_pick_creates_a_project(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    project_names_to_delete: list[tuple[str, str]],
) -> None:
    """⌘K New project… opens the Projects header's dialog and files a new folder."""
    base_url, session_id = seeded_session
    page: Page = request.getfixturevalue("page")
    enable_quick_pick_palette(page)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible(timeout=30_000)

    run_palette_command(page, "new project", "New project…")
    name_input = page.get_by_placeholder("Project name…")
    expect(name_input).to_be_visible(timeout=10_000)
    name = f"Wren {uuid.uuid4().hex[:6]}"
    project_names_to_delete.append((base_url, name))
    name_input.fill(name)
    page.get_by_test_id("new-project-confirm").click()
    expect(page.get_by_test_id("new-project-confirm")).to_have_count(0)
    expect(_project_folder(page, name)).to_be_visible()
    poll_until(lambda: name in _project_names(base_url), timeout_s=15.0)


def test_quick_pick_deletes_the_project_a_new_session_starts_in(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    project_names_to_delete: list[tuple[str, str]],
) -> None:
    """From the new-session view filed under a project, ⌘K deletes it and leaves the view."""
    base_url, session_id = seeded_session
    name = f"Plover {uuid.uuid4().hex[:6]}"
    project_id = _create_project(base_url, name, project_names_to_delete)
    patch_session(base_url, session_id, project_id=project_id)
    page: Page = request.getfixturevalue("page")
    enable_quick_pick_palette(page)
    page.goto(f"{base_url}/?project={quote(name)}")
    expect(_project_folder(page, name)).to_be_visible(timeout=30_000)

    palette_input = open_quick_pick_palette(page)
    palette_input.fill(name)
    palette = page.get_by_role("dialog")
    delete = palette.get_by_role(
        "option", name=re.compile("^" + re.escape(f"Delete project {name}…"))
    )
    expect(delete).to_be_visible()
    # The view already starts a session in the project, so ⌘K doesn't offer it again.
    new_session = re.compile("^" + re.escape(f"New session in {name}"))
    expect(palette.get_by_role("option", name=new_session)).to_have_count(0)
    delete.click()

    confirm = page.get_by_role("dialog", name="Delete project?")
    confirm.get_by_role("button", name="Delete project").click()
    expect(confirm).to_have_count(0)
    expect(_project_folder(page, name)).to_have_count(0)
    expect(page).to_have_url(f"{base_url}/")
    poll_until(lambda: session_archived(base_url, session_id), timeout_s=15.0)
    assert name not in _project_names(base_url)


def test_quick_pick_switches_the_session_lists_display(
    request: pytest.FixtureRequest, seeded_session_pair: tuple[str, str, str]
) -> None:
    """⌘K's Session List: Display lists archived sessions, as the filter menu's choice does."""
    base_url, open_id, archived_id = seeded_session_pair
    patch_session(base_url, archived_id, title="Heron archive", archived=True)
    page: Page = request.getfixturevalue("page")
    enable_quick_pick_palette(page)
    page.goto(f"{base_url}/c/{open_id}")
    archived_row = page.locator(f'li[data-sidebar-session-id="{archived_id}"]')
    expect(page.locator(f'li[data-sidebar-session-id="{open_id}"]').first).to_be_visible(
        timeout=30_000
    )
    expect(archived_row).to_have_count(0)

    run_palette_command(page, "session list display", "Session List: Display")
    page.get_by_role("dialog").get_by_role("option", name="Archived sessions").click()
    expect(archived_row.first).to_be_visible(timeout=10_000)


def test_quick_pick_moves_the_open_pin_up(
    request: pytest.FixtureRequest, seeded_session_pair: tuple[str, str, str]
) -> None:
    """⌘K's Move up in Pinned swaps the open pin with the one above, as a drag would."""
    base_url, first, second = seeded_session_pair
    page: Page = request.getfixturevalue("page")
    enable_quick_pick_palette(page)
    page.goto(f"{base_url}/c/{second}")
    expect(page.locator(f'li[data-sidebar-session-id="{first}"]').first).to_be_visible(
        timeout=30_000
    )
    _pin(page, first)
    _pin(page, second)
    expect(page).to_have_url(re.compile(rf"/c/{second}$"))
    pins = [sid for sid in _pinned_slot_ids(page) if sid in (first, second)]
    assert pins == [first, second], pins

    run_palette_command(page, "move up in pinned", "Move up in Pinned", closes=True)
    poll_until(
        lambda: (
            [sid for sid in _pinned_slot_ids(page) if sid in (first, second)] == [second, first]
        ),
        timeout_s=15.0,
    )


def _folder_names(page: Page) -> list[str]:
    """Project folder header texts in the sidebar, in render order."""
    return page.locator('button[data-slot="context-menu-trigger"]').all_inner_texts()


@pytest.mark.usefixtures("restored_project_order")
def test_quick_pick_moves_the_project_in_view(
    request: pytest.FixtureRequest,
    live_server: str,
    project_names_to_delete: list[tuple[str, str]],
) -> None:
    """⌘K moves the project a new session starts in, as its folder menu's Move does."""
    base_url = live_server
    suffix = uuid.uuid4().hex[:6]
    first, second = f"Wren {suffix} A", f"Wren {suffix} B"
    _create_project(base_url, first, project_names_to_delete)
    _create_project(base_url, second, project_names_to_delete)
    page: Page = request.getfixturevalue("page")
    enable_quick_pick_palette(page)
    page.goto(f"{base_url}/?project={quote(first)}")
    expect(_project_folder(page, second)).to_be_visible(timeout=30_000)

    def order() -> list[str]:
        texts = _folder_names(page)
        return [name for text in texts for name in (first, second) if name in text]

    assert order() == [first, second]
    run_palette_command(
        page, f"move project {first} down", f"Move Project {first} Down", closes=True
    )
    poll_until(lambda: order() == [second, first], timeout_s=15.0)
