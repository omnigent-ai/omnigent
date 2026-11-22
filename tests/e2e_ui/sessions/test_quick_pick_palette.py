"""E2E: the ``quick_pick_palette`` flag picks which ⌘K palette the app shows.

With the flag off, ⌘/Ctrl+K opens today's cmdk palette. With it on, the same
chord opens the QuickPick palette, which lists the same Actions, finds one from
its initials, reopens empty after Escape and runs the row with Enter.
Session rows, search groups and paging are covered by the shared Vitest cases
that run against both palettes (``CommandPalette.quickPick.test.tsx``).

On an open session, the QuickPick palette also previews themes as the user moves
through Select theme…, hands focus to the session switcher and back, and runs
the session's own actions.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

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
from tests.e2e_ui._quick_pick_palette import enable_quick_pick_palette, run_palette_command
from tests.e2e_ui.conftest import (
    _BUILD_OUTPUT,
    _REPO_ROOT,
    _ensure_runner_online,
    _server_state,
)

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
