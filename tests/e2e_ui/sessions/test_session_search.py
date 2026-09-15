from __future__ import annotations

import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect


@pytest.mark.parametrize("mac_electron", [False, True], ids=["browser", "mac-electron"])
def test_session_search_and_general_search(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
    mac_electron: bool,
) -> None:
    base_url, session_a, session_b = seeded_session_pair
    suffix = uuid.uuid4().hex[:8]
    target_title = f"Fix the parser {suffix}"
    for session_id, title in ((session_a, f"Deploy API {suffix}"), (session_b, target_title)):
        response = httpx.patch(
            f"{base_url}/v1/sessions/{session_id}", json={"title": title}, timeout=10.0
        )
        response.raise_for_status()

    if mac_electron:
        page.add_init_script("""
            Object.defineProperty(navigator, "platform", { value: "MacIntel" });
            Object.defineProperty(navigator, "userAgentData", { value: { platform: "macOS" } });
            Object.defineProperty(navigator, "userAgent", { value: "Mozilla/5.0 (Macintosh)" });
            window.omnigentDesktop = {
                kind: "electron",
                setBadgeCount() {},
                notify() { return Promise.resolve(false); },
                onNotificationActivated() { return () => {}; },
                getServerPicker() { return Promise.resolve(null); },
                switchServer() { return Promise.resolve(); },
                openServerSetup() {},
            };
        """)

    page.goto(f"{base_url}/c/{session_a}")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Keep this unsent draft")
    modifier = "Meta" if mac_electron else "Control"
    search_chord = f"{modifier}+Alt+KeyS"
    page.keyboard.press(search_chord)

    picker = page.get_by_role("dialog", name="Switch session", exact=True)
    expect(picker).to_be_visible()
    search_input = picker.get_by_placeholder("Search sessions by name…")
    search_input.fill(f"fxprs {suffix}")
    expect(picker.get_by_role("option").filter(has_text=target_title)).to_be_visible()
    expect(picker.get_by_role("option")).to_have_count(1)
    expect(picker.get_by_text("Actions", exact=True)).to_have_count(0)
    search_input.press("Escape")
    expect(picker).not_to_be_visible()
    expect(composer).to_have_value("Keep this unsent draft")

    page.keyboard.press(search_chord)
    expect(search_input).to_have_value("")
    search_input.fill(f"fxprs {suffix}")
    expect(picker.get_by_role("option")).to_have_count(1)
    search_input.press("Enter")
    page.wait_for_url(f"{base_url}/c/{session_b}")
    expect(picker).not_to_be_visible()

    search_container = (
        page.locator(".electron-sidebar-header-actions")
        if mac_electron
        else page.get_by_role("complementary", name="Conversations", exact=True)
    )
    search_container.get_by_role("button", name="Search", exact=True).click()
    general = page.get_by_role("dialog", name="Command palette", exact=True)
    expect(general).to_be_visible()
    expect(general.get_by_text("Go to Settings", exact=True)).to_be_visible()
    general.get_by_role("combobox").press("Escape")

    page.keyboard.press(search_chord)
    expect(picker).to_be_visible()
    page.keyboard.press(f"{modifier}+KeyK")
    expect(general).to_be_visible()
    expect(general.get_by_text("Go to Settings", exact=True)).to_be_visible()
