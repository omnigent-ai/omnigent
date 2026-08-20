"""E2E: Settings → Appearance Workspace panel default for new chats.

The Workspace panel control (``WorkspacePanelDefaultControl`` on
``pages/SettingsPage.tsx``) is a two-card radiogroup — Open / Collapsed —
under Settings → Appearance. Picking a mode writes to
``localStorage["omnigent:default-workspace-panel"]`` (absent = "open").

AppShell applies that preference only when a session has no saved
``SessionWorkspaceState.open``. Once the user toggles the rail in a chat,
that chat's own open-state wins on restore — so changing Appearance later
does not rewrite existing layouts.

This covers the pair the feature exists for: a brand-new session starts
collapsed after picking Collapsed, and a session the user already toggled
keeps its saved open-state even when the Appearance default is Collapsed.
No LLM turn is needed.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

STORAGE_KEY = "omnigent:default-workspace-panel"
_COMPOSER = "Ask the agent anything…"


def _stored_default(page: Page) -> str | None:
    """The persisted Workspace panel default, or None when unset (open)."""
    return page.evaluate(f"() => window.localStorage.getItem('{STORAGE_KEY}')")


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


def test_workspace_panel_default_control_defaults_and_persists(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Open is the default; picking Collapsed persists and survives a reload."""
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    # Fresh context → Open is selected and nothing is stored.
    expect(page.get_by_test_id("workspace-panel-default-open")).to_have_attribute(
        "aria-checked", "true"
    )
    assert _stored_default(page) is None, "a fresh load should store no Workspace panel default"

    _pick_workspace_panel_default(page, "collapsed")
    assert _stored_default(page) == "collapsed"

    page.reload()
    expect(page.get_by_role("radiogroup", name="Workspace panel")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("workspace-panel-default-collapsed")).to_have_attribute(
        "aria-checked", "true"
    )
    assert _stored_default(page) == "collapsed", (
        "the Workspace panel default did not survive a reload"
    )


def test_new_chat_follows_workspace_panel_default(
    page: Page, seeded_session_pair: tuple[str, str, str]
) -> None:
    """Collapsed seeds a never-visited chat; Open seeds a different never-visited chat.

    Uses two fresh sessions so neither has a saved per-chat ``open`` state when
    first opened. Session A is visited only after Collapsed is selected; session
    B only after Open is restored — proving the Appearance default applies to
    brand-new chats without rewriting chats the user has not opened yet.
    """
    base_url, session_a, session_b = seeded_session_pair

    _open_appearance(page, base_url)
    _pick_workspace_panel_default(page, "collapsed")
    assert _stored_default(page) == "collapsed"

    # Session A has never been opened in this browser → Collapsed applies.
    page.goto(f"{base_url}/c/{session_a}")
    _wait_session_ready(page)
    expect(page.get_by_role("complementary", name="Workspace")).to_have_count(0)
    expect(page.get_by_role("button", name="Expand right panel")).to_be_visible()

    # Restore Open and open a different never-visited session → rail starts open.
    _open_appearance(page, base_url)
    _pick_workspace_panel_default(page, "open")
    assert _stored_default(page) is None, "Open clears the storage key (product default)"

    page.goto(f"{base_url}/c/{session_b}")
    _wait_session_ready(page)
    expect(page.get_by_role("complementary", name="Workspace")).to_be_visible()
    expect(page.get_by_role("button", name="Collapse right panel")).to_be_visible()


def test_saved_session_open_state_wins_over_appearance_default(
    page: Page, seeded_session_pair: tuple[str, str, str]
) -> None:
    """Expanding the rail in a chat sticks even after Appearance is set to Collapsed.

    Opens session A under the product default (Open), collapses then re-expands
    so ``open: true`` is written, switches Appearance to Collapsed, and remounts
    session A — the saved open-state must win. Session B (never visited until
    after the preference change) still follows Collapsed, proving the default
    only seeds sessions without saved open-state.
    """
    base_url, session_a, session_b = seeded_session_pair

    # Visit session A while the product default is still Open, then toggle so
    # the per-session store records an explicit open=true.
    page.goto(f"{base_url}/c/{session_a}")
    _wait_session_ready(page)
    expect(page.get_by_role("complementary", name="Workspace")).to_be_visible(timeout=30_000)
    page.get_by_role("button", name="Collapse right panel").click()
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
