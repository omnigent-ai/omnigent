"""E2E: the side-chat entry points across the chat UI.

Side chat is generic (every harness) and surfaces as a soft tab in the right
Workspace rail. This proves the three ways to start one are wired end to end in
the running SPA, via the DOM — no LLM turn is involved:

  1. the ``/side`` slash command in the composer's command menu,
  2. "Start a new side chat" in the composer ``+`` tray, and
  3. "Side chat" in the rail's "Open new" (``+``) menu.

Opening a *generic* side chat forks the conversation onto a managed sandbox,
which the e2e_ui rig can't provision, so these cover the entry points (the part
component tests can't reach: the real composer menu + rail wiring), not the
fork round-trip itself.

The seeded session runs the ``openai-agents`` harness, for which
``supportsSideChat`` is true and ``usesNativeSideChatFork`` is false, so all
three generic entry points render.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail


def test_slash_menu_lists_side_command(page: Page, seeded_session: tuple[str, str]) -> None:
    """Typing ``/side`` surfaces the ``/side`` row in the composer command menu."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()

    composer.fill("/side")
    # The restyled slash menu renders a row per matching command; /side is the
    # generic panel side chat, offered on every harness.
    expect(page.get_by_test_id("slash-menu-item-side")).to_be_visible()


def test_composer_add_tray_offers_a_side_chat(page: Page, seeded_session: tuple[str, str]) -> None:
    """The composer ``+`` tray has a "Start a new side chat" item."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible()

    page.get_by_test_id("composer-attach").click()
    expect(page.get_by_role("menuitem", name="Start a new side chat")).to_be_visible()


def test_rail_new_tab_menu_offers_a_side_chat(page: Page, seeded_session: tuple[str, str]) -> None:
    """The Workspace rail's "Open new" (``+``) menu lists "Side chat"."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible()

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new", exact=True).click()
    expect(page.get_by_role("menuitem", name="Side chat", exact=True)).to_be_visible()
