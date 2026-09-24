"""Browser contracts for client-owned Sessions appearance preferences."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.conftest import BrowserContract

_COMPOSER = "Send a message…"


@pytest.fixture
def appearance_url(browser_contract: BrowserContract) -> Iterator[str]:
    empty_list = {
        "object": "list",
        "data": [],
        "first_id": None,
        "last_id": None,
        "has_more": False,
    }
    browser_contract.json(
        "/v1/info",
        {
            "accounts_enabled": False,
            "single_user": True,
            "login_url": None,
            "needs_setup": False,
            "databricks_features": False,
            "managed_sandboxes_enabled": False,
            "sandbox_provider": None,
            "sandbox_providers": [],
            "sandbox_provider_capabilities": {},
            "enabled_connections": [],
            "sharing_mode": "off",
            "public_sharing_enabled": False,
            "server_version": "browser-contract",
            "smart_routing_enabled": False,
            "smart_routing_sources": {"external": False, "oss": False},
            "features": {},
            "harness_install_enabled": False,
            "installable_harnesses": [],
            "dictation_available": False,
            "branding": {
                "app_name": None,
                "heading": None,
                "logos": {"main": None, "loading": None, "favicon": None},
                "powered_by": True,
            },
        },
    )
    browser_contract.json("/v1/me", {"user_id": "local", "is_admin": True})
    browser_contract.json("/v1/agents", empty_list)
    browser_contract.json("/v1/hosts", empty_list)
    browser_contract.json("/v1/projects", empty_list)
    browser_contract.json(
        "/v1/projects/order", {"ordered_project_ids": None, "sort_mode": "alphabetical"}
    )
    browser_contract.json("/v1/extensions", empty_list)
    browser_contract.json("/v1/harnesses", {"data": [], "setup_steps": {}})
    browser_contract.json("/v1/sessions/projects", [])
    browser_contract.json("/v1/sessions", empty_list)
    browser_contract.websocket(
        "**/v1/sessions/updates*", lambda ws: ws.on_message(lambda _message: None)
    )
    yield browser_contract.base_url


@pytest.fixture
def sessions_url(appearance_url: str, browser_contract: BrowserContract) -> tuple[str, str, str]:
    session_ids = ("appearance-session-a", "appearance-session-b")
    empty_list = {
        "object": "list",
        "data": [],
        "first_id": None,
        "last_id": None,
        "has_more": False,
    }
    sessions = [
        {
            "id": session_id,
            "object": "conversation",
            "title": f"Appearance session {index + 1}",
            "agent_id": "appearance-agent",
            "agent_name": "appearance-agent",
            "status": "idle",
            "created_at": index,
            "updated_at": index,
            "labels": {},
            "permission_level": None,
        }
        for index, session_id in enumerate(session_ids)
    ]
    browser_contract.json(
        "/v1/sessions",
        {**empty_list, "data": sessions, "first_id": session_ids[0], "last_id": session_ids[-1]},
    )
    for session in sessions:
        session_id = session["id"]
        browser_contract.json(f"/v1/sessions/{session_id}", session)
        browser_contract.json(f"/v1/sessions/{session_id}/items", empty_list)
        browser_contract.json(
            f"/v1/sessions/{session_id}/agent",
            {
                "id": "appearance-agent",
                "object": "agent",
                "name": "appearance-agent",
                "description": "Browser-contract fixture",
                "harness": "openai-agents",
                "mcp_servers": [],
                "policies": [],
                "terminals": [],
            },
        )
        browser_contract.json(f"/v1/sessions/{session_id}/child_sessions", empty_list)
        browser_contract.json(f"/v1/sessions/{session_id}/resources/terminals", empty_list)
        browser_contract.response(f"/v1/sessions/{session_id}/read-state", method="PUT")
        for resource in ("environments/default", "github"):
            browser_contract.json(
                f"/v1/sessions/{session_id}/resources/{resource}",
                {"error": {"message": "No browser-contract environment"}},
                status=404,
            )
        browser_contract.sse(f"/v1/sessions/{session_id}/stream")
    browser_contract.json(
        re.compile(r"/health(?:\?.*)?$"),
        {
            "sessions": {
                session_id: {"runner_online": True, "host_online": None}
                for session_id in session_ids
            }
        },
    )
    return appearance_url, *session_ids


def _open_appearance(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/settings/appearance")
    expect(page.get_by_role("heading", name="Appearance")).to_be_visible(timeout=20_000)


def test_reset_button_has_rendered_top_margin(page: Page, appearance_url: str) -> None:
    _open_appearance(page, appearance_url)
    gap = page.get_by_test_id("reset-appearance-button").evaluate(
        """button => {
            const wrapper = button.closest('div');
            const controls = wrapper.previousElementSibling;
            if (!controls) throw new Error('controls column sibling not found');
            return wrapper.getBoundingClientRect().top - controls.getBoundingClientRect().bottom;
        }"""
    )
    assert gap >= 24, f"Reset button margin is only {gap:.0f}px; expected at least 24px"


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


def _stored_size(page: Page) -> str | None:
    return page.evaluate("() => localStorage.getItem('omnigent:ui-font-size')")


def _desktop_ui_font_size(page: Page) -> str:
    return page.evaluate(
        "() => getComputedStyle(document.documentElement)"
        ".getPropertyValue('--desktop-ui-font-size').trim()"
    )


def _stored_family(page: Page) -> str | None:
    return page.evaluate("() => localStorage.getItem('omnigent:ui-font-family')")


def _ui_font_family(page: Page) -> str:
    return page.evaluate(
        "() => getComputedStyle(document.documentElement)"
        ".getPropertyValue('--ui-font-family').trim()"
    )


def _stored_default(page: Page) -> str | None:
    return page.evaluate("() => localStorage.getItem('omnigent:default-workspace-panel')")


def _pick_workspace_panel_default(page: Page, value: str) -> None:
    card = page.get_by_test_id(f"workspace-panel-default-{value}")
    card.click()
    expect(card).to_have_attribute("aria-checked", "true")


def test_sidebar_font_size_card_is_removed(page: Page, appearance_url: str) -> None:
    """The dedicated Sidebar font size card is no longer rendered."""
    base_url = appearance_url
    _open_appearance(page, base_url)

    expect(page.get_by_role("group", name="Sidebar settings", exact=True)).to_have_count(0)
    expect(page.get_by_test_id("sidebar-font-size-input")).to_have_count(0)


def test_appearance_reset_restores_defaults(page: Page, appearance_url: str) -> None:
    """Clicking Reset → confirm resets UI font size and terminal theme back to defaults."""
    base_url = appearance_url
    _open_appearance(page, base_url)

    font_size_input = page.get_by_test_id("ui-font-size-input")
    font_size_inc = page.get_by_test_id("ui-font-size-inc")
    terminal_dark = page.get_by_test_id("terminal-theme-dark")

    # Fresh context: the defaults are applied and nothing is persisted yet.
    expect(font_size_input).to_have_value("13")
    expect(page.get_by_test_id("terminal-theme-auto")).to_have_attribute("aria-checked", "true")
    stored_font_size = page.evaluate("() => window.localStorage.getItem('omnigent:ui-font-size')")
    assert stored_font_size is None, "expected no persisted font size on a fresh load"

    # Change two unrelated appearance preferences away from their defaults.
    for _ in range(5):
        font_size_inc.click()
    expect(font_size_input).to_have_value("18")
    terminal_dark.click()
    expect(page.get_by_test_id("terminal-theme-dark")).to_have_attribute("aria-checked", "true")

    # Confirm both changes were persisted.
    assert page.evaluate("() => window.localStorage.getItem('omnigent:ui-font-size')") == "18"
    assert page.evaluate("() => window.localStorage.getItem('omnigent:terminal-theme')") == "dark"

    # Reset, confirming through the dialog.
    page.get_by_test_id("reset-appearance-button").click()
    expect(page.get_by_role("dialog", name="Reset appearance?")).to_be_visible(timeout=30_000)
    page.get_by_test_id("reset-appearance-confirm").click()

    # Both choices are back to the product defaults.
    expect(font_size_input).to_have_value("13")
    expect(page.get_by_test_id("terminal-theme-auto")).to_have_attribute("aria-checked", "true")
    assert page.evaluate("() => window.localStorage.getItem('omnigent:ui-font-size')") is None
    assert page.evaluate("() => window.localStorage.getItem('omnigent:terminal-theme')") is None


def test_ui_font_family_applies_and_persists(page: Page, appearance_url: str) -> None:
    """Typing a family updates the applied property + value live and survives reload.

    A fresh context has no stored preference → empty field, no ``--ui-font-family``
    override (the UI uses the system stack). Typing a name applies the property and
    persists the choice; a page reload restores it (no reset, no flash to default).
    """
    base_url = appearance_url
    _open_appearance(page, base_url)

    value = page.get_by_test_id("ui-font-family-input")

    # Fresh context → empty field, nothing stored, no override applied.
    expect(value).to_have_value("")
    assert _stored_family(page) is None, "expected no persisted family on a fresh load"
    assert _ui_font_family(page) == "", "fresh load should apply no family override"

    # → "Georgia": the field, the applied property, and storage all move together.
    # The applied value leads with the chosen family and appends the system stack
    # (so an uninstalled/partial name degrades to the default sans, not serif), so
    # the resolved custom property starts with — rather than equals — "Georgia".
    value.fill("Georgia")
    expect(value).to_have_value("Georgia")
    assert _stored_family(page) == '"Georgia"', "the typed family was not persisted"
    assert _ui_font_family(page).startswith("Georgia"), "root family did not track the typed name"

    # The choice survives a full reload (persisted + re-applied before paint).
    page.reload()
    expect(page.get_by_role("group", name="Font family", exact=True)).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("ui-font-family-input")).to_have_value("Georgia")
    assert _ui_font_family(page).startswith("Georgia"), "family was not restored after reload"


def test_ui_font_family_reset_restores_system_default(page: Page, appearance_url: str) -> None:
    """The Reset button clears the override and returns to the system default."""
    base_url = appearance_url

    # Seed a family before the app boots so the override is applied on load.
    page.goto(base_url)
    page.evaluate(
        f"() => window.localStorage.setItem('{'omnigent:ui-font-family'}', '\"Georgia\"')"
    )
    _open_appearance(page, base_url)

    value = page.get_by_test_id("ui-font-family-input")
    reset = page.get_by_test_id("ui-font-family-reset")

    # The seeded family renders and is applied to the root (leading the appended
    # system-stack fallback, so the resolved value starts with "Georgia").
    expect(value).to_have_value("Georgia")
    assert _ui_font_family(page).startswith("Georgia")

    # → Reset: the field clears, the override is removed, and the key is cleared.
    reset.click()
    expect(value).to_have_value("")
    assert _ui_font_family(page) == "", "the family override was not removed on reset"
    assert _stored_family(page) is None, "reset did not clear the persisted family"


def test_ui_font_size_scales_and_persists(page: Page, appearance_url: str) -> None:
    """Stepping the size updates the token + value live and survives a reload.

    A fresh context has no stored preference → default 13px. Increasing the
    size updates ``--desktop-ui-font-size`` and persists the px value; a page
    reload restores it (no reset, no flash back to the default).
    """
    base_url = appearance_url
    _open_appearance(page, base_url)

    value = page.get_by_test_id("ui-font-size-input")
    increase = page.get_by_test_id("ui-font-size-inc")

    # Fresh context → default 13px token, nothing stored.
    expect(value).to_have_value("13")
    assert _stored_size(page) is None, "expected no persisted size on a fresh load"
    assert _desktop_ui_font_size(page) == "13px", "fresh load should apply the default size"

    # → 18px: five steps up. The value, applied token, and storage all move.
    for _ in range(5):
        increase.click()
    expect(value).to_have_value("18")
    assert _stored_size(page) == "18"
    assert _desktop_ui_font_size(page) == "18px", "font token did not track the stepped size"

    # The choice survives a full reload (persisted + re-applied before paint).
    page.reload()
    expect(page.get_by_role("group", name="Interface font size", exact=True)).to_be_visible(
        timeout=30_000
    )
    expect(page.get_by_test_id("ui-font-size-input")).to_have_value("18")
    assert _desktop_ui_font_size(page) == "18px", "font size was not restored after reload"


def test_ui_font_size_steppers_clamp_at_bounds(page: Page, appearance_url: str) -> None:
    """The ``−``/``+`` buttons disable at the 11px min and 18px max."""
    base_url = appearance_url

    # Seed the max before the app boots so the "+" button renders disabled.
    page.goto(base_url)
    page.evaluate(f"() => window.localStorage.setItem('{'omnigent:ui-font-size'}', '18')")
    _open_appearance(page, base_url)

    value = page.get_by_test_id("ui-font-size-input")
    decrease = page.get_by_test_id("ui-font-size-dec")
    increase = page.get_by_test_id("ui-font-size-inc")

    # At the 18px max, only "+" is disabled.
    expect(value).to_have_value("18")
    expect(increase).to_be_disabled()
    expect(decrease).to_be_enabled()

    # Hold "−" down to the 11px min; there it flips to "−" disabled, "+" enabled.
    for _ in range(8):
        if decrease.is_disabled():
            break
        decrease.click()
    expect(value).to_have_value("11")
    expect(decrease).to_be_disabled()
    expect(increase).to_be_enabled()


def test_ui_font_size_input_allows_free_editing(page: Page, appearance_url: str) -> None:
    """Typing in the box doesn't clamp mid-edit; blur settles the final value.

    Regression guard: the box binds to a free-form draft, so backspacing "13"
    down to "1" (below the 11px min) must SHOW "1" without snapping to 11 or
    persisting the transient value. Retyping a valid size applies it live, and
    blurring a still-out-of-range draft clamps to the minimum.
    """
    base_url = appearance_url

    # Seed a two-digit size so deleting a digit lands on a below-min "1".
    page.goto(base_url)
    page.evaluate(f"() => window.localStorage.setItem('{'omnigent:ui-font-size'}', '13')")
    _open_appearance(page, base_url)

    value = page.get_by_test_id("ui-font-size-input")
    expect(value).to_have_value("13")

    # Backspace to "1": the box holds the partial value; nothing clamps or
    # re-persists while the draft is out of range.
    value.click()
    value.press("End")
    value.press("Backspace")
    expect(value).to_have_value("1")
    assert _stored_size(page) == "13", "a mid-edit below-min draft must not persist"

    # Finish typing a valid size — it applies live and persists.
    value.press("8")
    expect(value).to_have_value("18")
    assert _stored_size(page) == "18"
    assert _desktop_ui_font_size(page) == "18px"

    # A still-out-of-range draft clamps to the minimum on blur.
    value.fill("1")
    value.blur()
    expect(value).to_have_value("11")
    assert _stored_size(page) == "11"


def test_code_font_size_steppers_clamp_at_bounds(page: Page, appearance_url: str) -> None:
    """The ``−`` / ``+`` buttons disable at the 10px min and 24px max."""
    base_url = appearance_url

    # Seed the max before the app boots so the "+" button renders disabled.
    page.goto(base_url)
    page.evaluate(f"() => window.localStorage.setItem('{'omnigent:code-font-size'}', '24')")
    _open_appearance(page, base_url)

    value = page.get_by_test_id("code-font-size-input")
    decrease = page.get_by_test_id("code-font-size-dec")
    increase = page.get_by_test_id("code-font-size-inc")

    # At the 24px max, only "+" is disabled.
    expect(value).to_have_value("24")
    expect(increase).to_be_disabled()
    expect(decrease).to_be_enabled()

    # Hold "−" down to the 10px min; there it flips to "−" disabled, "+" enabled.
    for _ in range(16):
        if decrease.is_disabled():
            break
        decrease.click()
    expect(value).to_have_value("10")
    expect(decrease).to_be_disabled()
    expect(increase).to_be_enabled()


def test_workspace_panel_default_control_defaults_and_persists(
    page: Page, appearance_url: str
) -> None:
    """Collapsed is the default; picking Open persists and survives a reload."""
    base_url = appearance_url
    _open_appearance(page, base_url)

    # Fresh context → Collapsed is selected and nothing is stored.
    expect(page.get_by_test_id("workspace-panel-default-collapsed")).to_have_attribute(
        "aria-checked", "true"
    )
    assert _stored_default(page) is None, "a fresh load should store no Workspace panel default"

    _pick_workspace_panel_default(page, "open")
    assert _stored_default(page) == "open"

    page.reload()
    expect(page.get_by_role("radiogroup", name="Workspace panel")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("workspace-panel-default-open")).to_have_attribute(
        "aria-checked", "true"
    )
    assert _stored_default(page) == "open", "the Workspace panel default did not survive a reload"

    _pick_workspace_panel_default(page, "collapsed")
    assert _stored_default(page) is None, "the product default should clear the storage key"


def test_new_chat_follows_workspace_panel_default(
    page: Page, sessions_url: tuple[str, str, str]
) -> None:
    """Collapsed seeds a never-visited chat; Open seeds a different never-visited chat.

    Uses two fresh sessions so neither has a saved per-chat ``open`` state when
    first opened. Session A is visited only after Collapsed is selected; session
    B only after Open is restored — proving the Appearance default applies to
    brand-new chats without rewriting chats the user has not opened yet.
    """
    base_url, session_a, session_b = sessions_url

    # Session A has never been opened in this browser → the product default applies.
    page.goto(f"{base_url}/c/{session_a}")
    _wait_session_ready(page)
    expect(page.get_by_role("complementary", name="Workspace")).to_have_count(0)
    expect(page.get_by_role("button", name="Expand right panel")).to_be_visible()

    # Select Open and open a different never-visited session → rail starts open.
    _open_appearance(page, base_url)
    _pick_workspace_panel_default(page, "open")
    assert _stored_default(page) == "open"

    page.goto(f"{base_url}/c/{session_b}")
    _wait_session_ready(page)
    expect(page.get_by_role("complementary", name="Workspace")).to_be_visible()
    expect(page.get_by_role("button", name="Collapse right panel")).to_be_visible()


def test_collapsing_the_rail_sticks_across_reload_and_new_sessions(
    page: Page, sessions_url: tuple[str, str, str]
) -> None:
    """A collapsed rail stays collapsed on reload and in the next chat opened.

    Collapsing in session A must not be undone by a reload, and must carry into
    session B — a chat with no saved open-state of its own — instead of
    springing back open. Reopening in B flips the remembered state, so the
    next fresh chat starts open.
    """
    base_url, session_a, session_b = sessions_url

    page.goto(f"{base_url}/c/{session_a}")
    _wait_session_ready(page)
    expect(page.get_by_role("complementary", name="Workspace")).to_have_count(0)
    page.get_by_role("button", name="Expand right panel").click()
    expect(page.get_by_role("complementary", name="Workspace")).to_be_visible()
    assert _stored_default(page) == "open", "expanding did not record the rail state"

    page.get_by_role("button", name="Collapse right panel").click()
    expect(page.get_by_role("complementary", name="Workspace")).to_have_count(0)
    assert _stored_default(page) is None, "collapsing should restore the product default"

    # A reload of the same chat keeps it collapsed.
    page.reload()
    _wait_session_ready(page)
    expect(page.get_by_role("complementary", name="Workspace")).to_have_count(0)

    # Session B has never been opened in this browser: it follows the
    # remembered collapse rather than the open default.
    page.goto(f"{base_url}/c/{session_b}")
    _wait_session_ready(page)
    expect(page.get_by_role("complementary", name="Workspace")).to_have_count(0)

    # Reopening it there flips the remembered state back.
    page.get_by_role("button", name="Expand right panel").click()
    expect(page.get_by_role("complementary", name="Workspace")).to_be_visible()
    assert _stored_default(page) == "open", "reopening the rail did not record the open state"
