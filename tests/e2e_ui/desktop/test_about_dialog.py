"""E2E coverage for the shell-owned Electron About page.

The e2e_ui harness runs Chromium rather than Electron, so this injects the same
narrow ``window.omnigentAbout`` contract as the About preload and drives the
bundled file page through its visible update states.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page, expect

_ABOUT_PAGE = Path(__file__).parents[3] / "web" / "electron" / "about" / "index.html"

_ABOUT_BRIDGE = """
(() => {
  const state = { calls: [], listener: null };
  const emit = (status) => { if (state.listener) state.listener(status); };
  window.__aboutTest = { calls: state.calls, emit };
  window.omnigentAbout = {
    getInfo: () => Promise.resolve({
      desktopVersion: "0.13.0",
      platformName: "macOS",
      appIconDataUrl: null,
      cli: {
        installed: true,
        version: "omnigent 0.13.0.dev0",
        path: "/Users/alice/.local/bin/omnigent",
      },
    }),
    getDesktopUpdateStatus: () => Promise.resolve({
      state: "available",
      info: { version: "0.14.0" },
    }),
    onDesktopUpdateStatus: (listener) => {
      state.listener = listener;
      return () => { state.listener = null; };
    },
    checkDesktopUpdates: () => Promise.resolve({ state: "none" }),
    downloadDesktopUpdate: () => {
      state.calls.push("download");
      setTimeout(() => emit({ state: "downloading", progress: { percent: 42.4 } }), 0);
      return Promise.resolve();
    },
    installDesktopUpdate: () => {
      state.calls.push("install");
      return Promise.resolve();
    },
    close: () => { state.calls.push("close"); },
  };
})();
"""


def test_about_dialog_update_flow(page: Page) -> None:
    """The About page distinguishes versions and owns desktop update progress."""
    page.add_init_script(_ABOUT_BRIDGE)
    page.goto(_ABOUT_PAGE.as_uri())

    expect(page.get_by_role("heading", name="Omnigent macOS app")).to_be_visible()
    expect(page.get_by_text("0.13.0", exact=True)).to_be_visible()
    expect(page.get_by_text("0.13.0.dev0", exact=True)).to_be_visible()
    expect(page.get_by_text("/Users/alice/.local/bin/omnigent", exact=True)).to_be_visible()
    expect(page.get_by_text("omni upgrade", exact=True)).to_be_visible()

    update_now = page.get_by_role("button", name="Update now")
    expect(update_now).to_be_visible()
    update_now.click()

    progress = page.get_by_role("progressbar", name="Update download progress")
    expect(progress).to_be_visible()
    expect(progress).to_have_attribute("aria-valuenow", "42")
    expect(page.get_by_text("42%", exact=True)).to_be_visible()
    assert "download" in page.evaluate("() => window.__aboutTest.calls")

    page.evaluate(
        """() => window.__aboutTest.emit({
          state: "downloaded",
          info: { version: "0.14.0" },
        })"""
    )
    restart = page.get_by_role("button", name="Restart to update")
    expect(restart).to_be_visible()
    restart.click()
    page.wait_for_function("() => window.__aboutTest.calls.includes('install')")
