"""E2E coverage for rebinding a shortcut from Settings."""

from playwright.sync_api import Page, expect

ACTION_ID = "workbench.action.openKeyboardShortcuts"
STORAGE_KEY = "omnigent:keybindings:v1"


def test_rebind_shortcut_updates_pill(page: Page, live_server: str) -> None:
    """A shortcut pill opens the recorder and persists the replacement."""
    page.goto(f"{live_server}/settings/shortcuts")
    editor = page.get_by_test_id("keybinding-editor")
    expect(editor).to_be_visible(timeout=30_000)

    page.get_by_role("textbox", name="Search keyboard shortcuts").fill("openKeyboardShortcuts")
    expect(page.locator("code", has_text=ACTION_ID)).to_be_visible()
    pill = page.get_by_role(
        "button",
        name=f"Rebind {ACTION_ID} primary+/",
        exact=True,
    )
    expect(pill).to_be_visible()

    pill.click()
    dialog = page.get_by_role("dialog", name="Rebind keyboard shortcut")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_test_id("keybinding-recorder")).to_be_focused()

    page.keyboard.press("Control+Alt+KeyJ")
    expect(dialog).to_be_hidden()
    expect(
        page.get_by_role(
            "button",
            name=f"Rebind {ACTION_ID} ctrl+alt+[KeyJ]",
            exact=True,
        )
    ).to_be_visible()

    stored = page.evaluate(f"() => window.localStorage.getItem('{STORAGE_KEY}')")
    assert stored is not None
    assert "ctrl+alt+[KeyJ]" in stored
    assert "workbench.openKeyboardShortcuts" in stored
