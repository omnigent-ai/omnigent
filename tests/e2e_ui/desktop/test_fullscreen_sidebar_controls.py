"""E2E: macOS-shell fullscreen drops the traffic-light clearance.

The macOS desktop layout pins the sidebar header cluster (toggle/Search/
Settings) in the title-bar strip at ``left: 5.5rem`` so it clears the traffic
lights (the ``[data-electron-mac]`` rules in ``web/src/index.css``). Native
fullscreen removes the lights, so the cluster must realign with the window's
left edge instead of keeping an empty 5.5rem strip; leaving fullscreen
restores the clearance.

The e2e_ui harness runs the SPA in plain Chromium, not Electron, so we inject
a scriptable ``window.omnigentDesktop`` stub (before any app script runs) with
the fullscreen half of the preload bridge, and drive enter/leave transitions
from Python via ``window.__omniFullScreen.set(...)`` -- modelling the main
process's ``omnigent:full-screen-changed`` forwarding without a real window.
The full native path (real ``setFullScreen`` on the Electron main process) is
covered by ``web/electron/e2e/desktop_fullscreen_sidebar_controls.e2e.js``.
"""

from __future__ import annotations

import os

from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# Stands in for the Electron preload bridge, macOS flavour: the Macintosh UA
# engages isMacElectronShell()'s layout, and the fullscreen methods mirror
# preload.js. Base native methods are guarded no-ops so unrelated calls don't
# throw under the stub.
_MAC_SHELL_INIT_SCRIPT = """
(() => {
  Object.defineProperty(navigator, "platform", { value: "MacIntel" });
  Object.defineProperty(navigator, "userAgentData", { value: { platform: "macOS" } });
  Object.defineProperty(navigator, "userAgent", { value: "Mozilla/5.0 (Macintosh)" });
  const state = { fullScreen: false, listeners: new Set() };
  window.__omniFullScreen = {
    set: (fullScreen) => {
      state.fullScreen = fullScreen;
      for (const listener of state.listeners) listener(fullScreen);
    },
  };
  window.omnigentDesktop = {
    kind: "electron",
    setBadgeCount() {},
    notify() { return Promise.resolve(false); },
    onNotificationActivated() { return () => {}; },
    getServerPicker() { return Promise.resolve(null); },
    switchServer() { return Promise.resolve(); },
    openServerSetup() {},
    isFullScreen() { return Promise.resolve(state.fullScreen); },
    onFullScreenChanged(callback) {
      state.listeners.add(callback);
      return () => state.listeners.delete(callback);
    },
  };
})();
"""

# The 5.5rem clearance the cluster keeps for the traffic lights while windowed.
TRAFFIC_LIGHT_CLEARANCE_PX = 88
# "Realigned": the cluster must start well inside the old clearance.
FULLSCREEN_ALIGNED_MAX_X = 48

_CLUSTER = ".electron-sidebar-header-actions"


def _linger(page: Page) -> None:
    """Hold the current state briefly so recorded footage shows it; no-op in CI."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(1_500)


def _cluster_x(page: Page) -> float:
    box = page.locator(_CLUSTER).bounding_box()
    assert box is not None, "title-bar cluster has no bounding box"
    return box["x"]


def _wait_for_cluster_x(page: Page, predicate: str, limit: float) -> None:
    """Wait briefly for the cluster to settle at a position; assert separately."""
    page.wait_for_function(
        "([selector, limit]) => {"
        "  const el = document.querySelector(selector);"
        f" return el && el.getBoundingClientRect().x {predicate} limit;"
        "}",
        arg=[_CLUSTER, limit],
        timeout=5_000,
    )


def test_fullscreen_realigns_sidebar_header_controls(page: Page, live_server: str) -> None:
    page.add_init_script(_MAC_SHELL_INIT_SCRIPT)
    page.goto(live_server)

    cluster = page.locator(_CLUSTER)
    expect(cluster).to_be_visible(timeout=30_000)

    # Windowed: the cluster clears the traffic lights.
    windowed_x = _cluster_x(page)
    assert windowed_x >= TRAFFIC_LIGHT_CLEARANCE_PX - 8, (
        f"windowed cluster should clear the traffic lights, got x={windowed_x}"
    )
    _linger(page)

    # The window goes fullscreen: the lights are gone, so the cluster must
    # realign with the left edge instead of keeping the dead 5.5rem strip.
    page.evaluate("window.__omniFullScreen.set(true)")
    try:
        _wait_for_cluster_x(page, "<", FULLSCREEN_ALIGNED_MAX_X)
    except PlaywrightTimeoutError:
        pass  # Assert below with the measured position for a clear failure.
    fullscreen_x = _cluster_x(page)
    assert fullscreen_x < FULLSCREEN_ALIGNED_MAX_X, (
        "sidebar header controls still reserve the traffic-light strip in "
        f"fullscreen: cluster at x={fullscreen_x} (expected < {FULLSCREEN_ALIGNED_MAX_X})"
    )
    _linger(page)

    # Leaving fullscreen restores the clearance (the lights are back).
    page.evaluate("window.__omniFullScreen.set(false)")
    try:
        _wait_for_cluster_x(page, ">=", TRAFFIC_LIGHT_CLEARANCE_PX - 8)
    except PlaywrightTimeoutError:
        pass
    restored_x = _cluster_x(page)
    assert restored_x >= TRAFFIC_LIGHT_CLEARANCE_PX - 8, (
        f"windowed traffic-light clearance not restored, got x={restored_x}"
    )
    _linger(page)
