"""Full-stack guard: a session's saved rail state beats the Appearance default."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.workspace_panel_product_default

_COMPOSER = "Send a message…"


def _open_appearance(page: Page, base_url: str) -> None:
    """Navigate to Settings Appearance and wait for the Workspace panel control."""
    page.goto(f"{base_url}/settings/appearance")
    expect(page.get_by_role("radiogroup", name="Workspace panel")).to_be_visible(timeout=30_000)


def _pick_workspace_panel_default(page: Page, value: str) -> None:
    """Pick Open or Collapsed via its Appearance radio card."""
    card = page.get_by_test_id(f"workspace-panel-default-{value}")
    card.click()
    expect(card).to_have_attribute("aria-checked", "true")


def _wait_session_ready(page: Page) -> None:
    """Wait until the session chrome has settled enough to assert rail state.

    The Expand/Collapse toggle only mounts once the rail has content (Agents is
    always available), so its presence is the portable "shell is ready" signal
    whether the rail itself is open or collapsed.
    """
    expect(
        page.locator(
            'button[aria-label="Expand right panel"], button[aria-label="Collapse right panel"]'
        ).first
    ).to_be_visible(timeout=60_000)
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)


def test_saved_session_open_state_wins_over_appearance_default(
    page: Page, seeded_session_pair: tuple[str, str, str]
) -> None:
    """Expanding the rail in a chat sticks even after Appearance is set to Collapsed.

    Opens session A under the product default (Collapsed), expands it so
    ``open: true`` is written, switches Appearance back to Collapsed, and remounts
    session A — the saved open-state must win. Session B (never visited until
    after the preference change) still follows Collapsed, proving the default
    only seeds sessions without saved open-state.
    """
    base_url, session_a, session_b = seeded_session_pair

    # Visit session A under the collapsed product default, then expand it so
    # the per-session store records an explicit open=true.
    page.goto(f"{base_url}/c/{session_a}")
    _wait_session_ready(page)
    expect(page.get_by_role("complementary", name="Workspace")).to_have_count(0)
    page.get_by_role("button", name="Expand right panel").click()
    expect(page.get_by_role("complementary", name="Workspace")).to_be_visible()

    _open_appearance(page, base_url)
    _pick_workspace_panel_default(page, "collapsed")

    # Remount session A: saved open=true beats the Collapsed Appearance default.
    page.goto(f"{base_url}/c/{session_a}")
    _wait_session_ready(page)
    expect(page.get_by_role("complementary", name="Workspace")).to_be_visible()
    expect(page.get_by_role("button", name="Collapse right panel")).to_be_visible()

    # A never-visited session still follows Collapsed.
    page.goto(f"{base_url}/c/{session_b}")
    _wait_session_ready(page)
    expect(page.get_by_role("complementary", name="Workspace")).to_have_count(0)
    expect(page.get_by_role("button", name="Expand right panel")).to_be_visible()
