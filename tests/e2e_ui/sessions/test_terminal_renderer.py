"""E2E: Settings → Appearance terminal renderer preference.

The Compatibility escape hatch must be discoverable in the real Settings UI,
persist across a reload, and return cleanly to the GPU default.  TerminalSession
unit coverage owns the WebGL/DOM handover itself because headless CI may not
provide a WebGL context.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

STORAGE_KEY = "omnigent:terminal-renderer"


def _stored_renderer(page: Page) -> str | None:
    """Return the persisted renderer override; absent means GPU default."""
    return page.evaluate(f"() => window.localStorage.getItem('{STORAGE_KEY}')")


def test_terminal_renderer_control_persists_and_restores_gpu_default(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Compatibility survives reload; selecting GPU clears the override."""
    base_url, _session_id = seeded_session
    page.goto(f"{base_url}/settings/appearance")

    group = page.get_by_role("radiogroup", name="Terminal renderer")
    expect(group).to_be_visible(timeout=30_000)

    gpu = page.get_by_test_id("terminal-renderer-auto")
    compatibility = page.get_by_test_id("terminal-renderer-dom")

    expect(gpu).to_have_attribute("aria-checked", "true")
    expect(compatibility).to_have_attribute("aria-checked", "false")
    assert _stored_renderer(page) is None

    compatibility.click()
    expect(compatibility).to_have_attribute("aria-checked", "true")
    assert _stored_renderer(page) == "dom"

    page.reload()
    expect(group).to_be_visible(timeout=30_000)
    expect(compatibility).to_have_attribute("aria-checked", "true")
    assert _stored_renderer(page) == "dom"

    gpu.click()
    expect(gpu).to_have_attribute("aria-checked", "true")
    assert _stored_renderer(page) is None
