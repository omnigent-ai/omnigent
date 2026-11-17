"""E2E: the Tile view (``tiles_view``) and its Single agent ⇄ Tiles nav toggle.

Covers what component tests cannot: the Tile view shares the sidebar's live
session cache (a separate query on its key broke the sidebar), the nav toggle
carries the active agent across routes, Cards hand off to Focus, and the
Settings preference hides the nav entry. No LLM turn is involved.

Runs its own server with the flag on, so the shared server's sidebar (and its
visual baselines) stays unchanged.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests._helpers.live_server import isolated_local_server

_REPO_ROOT = Path(__file__).resolve().parents[3]
_EXAMPLES = _REPO_ROOT / "dev" / "omnidev" / "fixtures" / "conversations"


def _import_example(base_url: str, name: str, config_home: Path) -> str:
    """Import a bundled example conversation and return its session id."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "session",
            "import",
            "-i",
            str(_EXAMPLES / name),
            "--server",
            base_url,
        ],
        env={**os.environ, "OMNIGENT_CONFIG_HOME": str(config_home)},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    match = re.search(r"into ([0-9a-f]{32})", result.stdout + result.stderr)
    assert match, f"import printed no session id:\n{result.stdout}\n{result.stderr}"
    return match.group(1)


@pytest.fixture(scope="module")
def tiles_server(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, str]]:
    """A server with ``tiles_view`` on and two recent sessions.

    :returns: ``(base_url, session_a, session_b)``.
    """
    tmp = tmp_path_factory.mktemp("tiles-view")
    with isolated_local_server(tmp, env={"OMNIGENT_FEATURES": "tiles_view"}) as base_url:
        config_home = tmp / "cli-config"
        session_a = _import_example(base_url, "planning-a-feature.jsonl", config_home)
        session_b = _import_example(base_url, "quick-question.jsonl", config_home)
        yield base_url, session_a, session_b


def _tile(page: Page, session_id: str):
    return page.locator(f'[data-testid="agent-tile"][data-session-id="{session_id}"]')


def test_nav_toggle_opens_tiles_and_carries_the_active_agent(
    page: Page,
    tiles_server: tuple[str, str, str],
) -> None:
    """Single agent → Tiles keeps the session active; clicking a tile switches it."""
    base_url, session_a, session_b = tiles_server
    page.goto(f"{base_url}/c/{session_a}")
    toggle = page.get_by_test_id("tiles-toggle-nav")
    expect(toggle).to_have_text("Tiles", timeout=30_000)

    toggle.click()
    expect(page).to_have_url(f"{base_url}/tiles?active={session_a}")
    expect(_tile(page, session_a)).to_have_attribute("data-active", "true", timeout=30_000)
    expect(_tile(page, session_b)).to_be_visible()
    # The Tile view reads the sidebar's rows; the sidebar must still load.
    expect(page.get_by_text("Some sessions could not be loaded")).to_have_count(0)

    # The title opens the tile's details without switching the active agent.
    _tile(page, session_b).get_by_test_id("agent-tile-title").click()
    expect(_tile(page, session_b).get_by_test_id("tile-details")).to_be_visible()
    expect(_tile(page, session_a)).to_have_attribute("data-active", "true")

    # Click the tile itself, not a control, to make it the active agent.
    _tile(page, session_b).get_by_test_id("agent-tile-subline").click()
    expect(_tile(page, session_b)).to_have_attribute("data-active", "true")

    # Full screen shows only that agent; Escape returns to the grid.
    _tile(page, session_b).get_by_role("button", name=re.compile("^Full screen")).click()
    expect(page.get_by_test_id("tiles-full-screen")).to_be_visible()
    expect(_tile(page, session_a)).to_have_count(0)
    page.keyboard.press("Escape")
    expect(page.get_by_test_id("tiles-full-screen")).to_have_count(0)
    expect(_tile(page, session_a)).to_be_visible()
    expect(page).to_have_url(f"{base_url}/tiles?active={session_b}")

    expect(toggle).to_have_text("Single agent")
    toggle.click()
    expect(page).to_have_url(f"{base_url}/c/{session_b}")


def test_cards_open_the_picked_agent_in_focus(
    page: Page,
    tiles_server: tuple[str, str, str],
) -> None:
    """Picking a card switches to Focus with that agent active."""
    base_url, session_a, _session_b = tiles_server
    page.goto(f"{base_url}/tiles?view=cards")
    card = _tile(page, session_a)
    expect(card).to_be_visible(timeout=30_000)

    card.click()
    expect(page).to_have_url(re.compile(rf"view=focus&active={session_a}"))
    expect(page.get_by_role("radio", name="Focus")).to_have_attribute("aria-checked", "true")
    expect(_tile(page, session_a)).to_have_attribute("data-active", "true")


def test_settings_can_hide_tiles_from_the_sidebar(
    page: Page,
    tiles_server: tuple[str, str, str],
) -> None:
    """Settings → General → Tiles removes and restores the nav toggle."""
    base_url = tiles_server[0]
    page.goto(f"{base_url}/settings/general")
    show_in_sidebar = page.get_by_test_id("tiles-show-in-sidebar-toggle")
    expect(show_in_sidebar).to_have_attribute("aria-checked", "true", timeout=30_000)

    show_in_sidebar.click()
    expect(show_in_sidebar).to_have_attribute("aria-checked", "false")
    page.goto(f"{base_url}/")
    expect(page.get_by_test_id("tiles-toggle-nav")).to_have_count(0, timeout=30_000)

    page.goto(f"{base_url}/settings/general")
    page.get_by_test_id("tiles-show-in-sidebar-toggle").click()
    page.goto(f"{base_url}/")
    expect(page.get_by_test_id("tiles-toggle-nav")).to_be_visible(timeout=30_000)
