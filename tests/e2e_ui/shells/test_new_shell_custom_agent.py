"""E2E: a custom agent that declares no ``terminals:`` still offers a user shell.

The Workspace rail's "+" menu used to offer Shell only when the agent declared
``terminals:`` (native wrappers always do), so custom-agent sessions had no way
to open a shell. The server now offers such agents a default ``bash`` shell;
this drives the rail on the suite's ``hello_world`` agent and opens it.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail


def test_custom_agent_session_offers_new_shell(request: pytest.FixtureRequest) -> None:
    base_url, session_id = request.getfixturevalue("seeded_session")
    page: Page = request.getfixturevalue("page")

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=60_000)
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    shell_item = page.get_by_role("menuitem", name=re.compile(r"^Shell \(bash\)"))
    expect(shell_item).to_be_visible()

    shell_item.click()
    # The default shell opens as a rail tab (``bash · u-<key>``) whose xterm
    # connects inside the rail; the chat composer stays in place.
    close_tab = rail.get_by_role("button", name=re.compile(r"^Close bash · u-"))
    expect(close_tab).to_be_visible(timeout=60_000)
    terminal_view = rail.get_by_test_id("terminal-view")
    expect(terminal_view.last).to_be_visible(timeout=20_000)
    expect(terminal_view.last).to_have_attribute("data-state", "connected", timeout=20_000)
    expect(page.get_by_label("Message the agent")).to_be_visible()
