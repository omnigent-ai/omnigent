"""E2E: an open shell tab survives navigating away to another session and back.

Regression for the reported bug: open a shell in the workspace rail, switch to
a different session, then return — the shell tab used to be gone. Shell tabs
lived only in transient component state and the conversation-switch effect
cleared them on every navigation, even though the PTYs themselves live on the
server and are re-fetched per session. They now persist per session (like the
open file tabs) in ``sessionWorkspaceState``, so a client-side session switch
restores the tab strip.

The switch is driven via the in-app sidebar link (client-side navigation), NOT
``page.goto`` — a full reload would re-seed everything from storage on cold
load and wouldn't exercise the conversation-switch effect that regressed.

Uses the real ``terminal_session`` fixture (a runner-bound session whose agent
declares a ``zsh`` terminal) so an actual PTY backs the tab, and creates a
second bare session on the same runner to switch to.
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _create_runner_bound_session,
    _server_state,
    open_right_rail,
)


def _open_new_shell(page: Page) -> None:
    """Create a shell via the tab strip's "+" → Shell menu (see test_new_shell)."""
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()


def test_shell_tab_persists_across_session_navigation(
    page: Page, terminal_session: tuple[str, str]
) -> None:
    """Open a shell in A, switch to B via the sidebar, return to A — tab stays.

    Before the fix the tab strip was cleared on the conversation-switch effect,
    so returning to A showed no shell tab. Now the strip is restored from the
    per-session store and the tab (and its xterm) come back.
    """
    base_url, session_a = terminal_session
    # A second, bare session on the same runner to switch to. Cleaned up below.
    runner_id = str(_server_state["runner_id"])
    session_b = _create_runner_bound_session(base_url, runner_id)

    try:
        page.goto(f"{base_url}/c/{session_a}")
        _open_new_shell(page)

        rail = page.get_by_role("complementary", name="Workspace")
        close_tab = rail.get_by_role("button", name=re.compile(r"^Close zsh · u-"))
        expect(close_tab).to_be_visible(timeout=60_000)
        terminal_view = rail.get_by_test_id("terminal-view").last
        expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)

        # Switch to session B via the sidebar link — client-side navigation, so
        # the conversation-switch effect runs (a full reload wouldn't exercise
        # the path that regressed). B is a fresh session, so it has no shell tab.
        page.locator(f'a[href="/c/{session_b}"]').click()
        page.wait_for_url(re.compile(rf"/c/{re.escape(session_b)}"))
        expect(
            page.get_by_role("complementary", name="Workspace").get_by_role(
                "button", name=re.compile(r"^Close zsh · u-")
            )
        ).to_have_count(0)

        # Navigate back to A — the shell tab is restored from the per-session
        # store, and its xterm reconnects to the still-live PTY.
        page.locator(f'a[href="/c/{session_a}"]').click()
        page.wait_for_url(re.compile(rf"/c/{re.escape(session_a)}"))

        rail_back = page.get_by_role("complementary", name="Workspace")
        expect(rail_back.get_by_role("button", name=re.compile(r"^Close zsh · u-"))).to_be_visible(
            timeout=30_000
        )
        expect(rail_back.get_by_test_id("terminal-view").last).to_have_attribute(
            "data-state", "connected", timeout=20_000
        )
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{session_b}", timeout=10.0)
