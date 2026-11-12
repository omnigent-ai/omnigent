"""E2E: the ``quick_pick_palette`` flag picks which ⌘K palette the app shows.

With the flag off, ⌘/Ctrl+K opens today's cmdk palette. With it on, the same
chord opens the QuickPick palette, which lists the same Actions, finds one from
its initials, reopens empty after Escape and runs the row with Enter.
Session rows, search groups and paging are covered by the shared Vitest cases
that run against both palettes (``CommandPalette.quickPick.test.tsx``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests._helpers.compat import compat_server_cwd, server_executable, server_pythonpath
from tests._helpers.live_server import isolated_local_server
from tests.e2e_ui.conftest import _BUILD_OUTPUT, _REPO_ROOT

_PALETTE_INPUT = "command-palette-input"


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
    page.keyboard.press("ControlOrMeta+k")
    palette_input = page.get_by_test_id(_PALETTE_INPUT)
    expect(palette_input).to_be_focused(timeout=10_000)
    dialog = page.get_by_role("dialog")
    expect(dialog.locator("[cmdk-root]")).to_have_count(1)
    expect(dialog.locator('[data-slot="quick-pick"]')).to_have_count(0)

    palette_input.fill("settings")
    page.keyboard.press("Enter")
    expect(page).to_have_url(f"{default_palette_server}/settings", timeout=10_000)


def test_flag_on_runs_the_quick_pick_palette(page: Page, quick_pick_server: str) -> None:
    """With the flag, ⌘K opens QuickPick; initials, Escape and Enter work."""
    page.goto(f"{quick_pick_server}/")
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
    expect(page).to_have_url(f"{quick_pick_server}/settings", timeout=10_000)
    expect(page.get_by_test_id(_PALETTE_INPUT)).to_have_count(0)
