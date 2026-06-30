"""E2E: the Files rail "Working folder" header is a collapsible button.

The desktop Workspace rail renders ``FilesPanel`` in its ``frameless``
(inline) mode. That must NOT downgrade the working-folder header to a plain
label: it stays an interactive ``button`` carrying ``aria-expanded`` so it is
focusable and toggles the file list. Only the mobile/full-screen drawer
(which has its own X close button) uses a static label header.

This is the regression guard for that distinction: ``frameless`` once folded
into a ``fullScreen`` flag that swapped the button for a ``<span>``, so the
rail header silently stopped being a button. No message is sent — the header
and its collapse state are rail state, not a function of any turn — so this
stays a fast, LLM-free check.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail


def test_files_rail_working_folder_header_is_a_toggle_button(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The rail's "Working folder" header is a button whose chevron collapses
    the file list and flips ``aria-expanded``."""
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

    # The header is a BUTTON (not a label): substring-matching "Working folder"
    # tolerates the trailing working-directory basename the header also renders.
    header = rail.get_by_role("button", name=re.compile("Working folder"))
    expect(header).to_be_visible(timeout=30_000)
    expect(header).to_have_attribute("aria-expanded", "true")

    # Expanded: the file-scope switch (Changed | All) is part of the content.
    scope = rail.get_by_role("radiogroup", name="File scope")
    expect(scope).to_be_visible()

    # Collapsing via the header hides the content and flips aria-expanded.
    header.click()
    expect(header).to_have_attribute("aria-expanded", "false")
    expect(scope).to_have_count(0)

    # Expanding again restores the content — proving the header drives a real
    # collapse toggle, not a one-way no-op.
    header.click()
    expect(header).to_have_attribute("aria-expanded", "true")
    expect(rail.get_by_role("radiogroup", name="File scope")).to_be_visible()
