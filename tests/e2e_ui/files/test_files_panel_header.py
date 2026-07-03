"""E2E: the Files rail "Working folder" header is a static label, not a toggle.

The desktop Workspace rail renders ``FilesPanel`` in its ``frameless``
(inline) mode. The working-folder header is a plain label: the file list is
the whole point of the panel, so there is nothing to collapse to. The header
must NOT be a button and the file-scope switch (the panel content) must be
visible with no toggle needed.

This is the regression guard against reintroducing the collapse chevron: the
header once doubled as a collapse toggle carrying ``aria-expanded``, which
made no sense in a panel whose only content is the file list. No message is
sent — the header is rail state, not a function of any turn — so this stays a
fast, LLM-free check.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

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

    # The content is always shown: the file-scope switch (Changed | All) is
    # visible with no toggle needed.
    expect(rail.get_by_role("radiogroup", name="File scope")).to_be_visible()
