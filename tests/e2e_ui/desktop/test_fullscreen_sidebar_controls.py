"""Check macOS sidebar-header clearance across fullscreen transitions.

Chromium models the Electron bridge; the native journey is tested separately.
"""

from __future__ import annotations

import contextlib
import os

import pytest
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# The macOS UA activates the desktop layout; other bridge methods stay inert.
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

# Clearance (px) each surface uses windowed vs. fullscreen. These mirror the
# offsets in web/src/index.css: the cluster shifts left 5.5rem -> 0.75rem (88 ->
# 12px) and the header/rail slots drop padding 10.5rem -> 5.75rem (168 -> 92px).
_WINDOWED_CLUSTER_PX = 88
_FULLSCREEN_CLUSTER_PX = 12
_WINDOWED_SLOT_PX = 168
_FULLSCREEN_SLOT_PX = 92

# Measure the cluster offset and both padded slots (chat-header breadcrumb slot
# and maximized workspace tab strip) windowed vs. fullscreen. Synthetic fixtures
# isolate the clearance rules from the live app state needed to render them.
_MEASURE_CLEARANCE_SURFACES = """
() => {
  const root = document.querySelector("[data-electron-mac]");
  if (!root) return null;
  const hadSidebarOpen = root.hasAttribute("data-sidebar-open");
  // The maximized-rail clearance rule only applies with the sidebar closed.
  root.removeAttribute("data-sidebar-open");

  const slot = document.createElement("div");
  slot.className = "traffic-light-clearance";
  root.appendChild(slot);

  const rail = document.createElement("aside");
  rail.setAttribute("aria-label", "Workspace");
  rail.setAttribute("data-maximized", "");
  const strip = document.createElement("div");
  strip.className = "workspace-tab-strip";
  rail.appendChild(strip);
  root.appendChild(rail);

  const cluster = document.querySelector(".electron-sidebar-header-actions");
  const snapshot = () => ({
    cluster: parseFloat(getComputedStyle(cluster).left),
    headerSlot: parseFloat(getComputedStyle(slot).paddingLeft),
    workspaceStrip: parseFloat(getComputedStyle(strip).paddingLeft),
  });

  root.removeAttribute("data-electron-fullscreen");
  const windowed = snapshot();
  root.setAttribute("data-electron-fullscreen", "true");
  const fullscreen = snapshot();

  // Restore the DOM so nothing leaks past this measurement.
  root.removeAttribute("data-electron-fullscreen");
  if (hadSidebarOpen) root.setAttribute("data-sidebar-open", "true");
  slot.remove();
  rail.remove();
  return { windowed, fullscreen };
}
"""


def _linger(page: Page) -> None:
    """Hold the current state briefly so recorded footage shows it; no-op in CI."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(1_500)


def _cluster_x(page: Page) -> float:
    box = page.locator(_CLUSTER).bounding_box()
    assert box is not None, "title-bar cluster has no bounding box"
    return box["x"]


def _wait_for_cluster_x(page: Page, *, below: bool, limit: float) -> None:
    """Wait briefly for the cluster to settle at a position; assert separately."""
    page.wait_for_function(
        "([selector, below, limit]) => {"
        "  const el = document.querySelector(selector);"
        "  if (!el) return false;"
        "  const x = el.getBoundingClientRect().x;"
        "  return below ? x < limit : x >= limit;"
        "}",
        arg=[_CLUSTER, below, limit],
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

    # The cluster realigns when fullscreen hides the traffic lights.
    page.evaluate("window.__omniFullScreen.set(true)")
    with contextlib.suppress(PlaywrightTimeoutError):
        _wait_for_cluster_x(page, below=True, limit=FULLSCREEN_ALIGNED_MAX_X)
    fullscreen_x = _cluster_x(page)
    assert fullscreen_x < FULLSCREEN_ALIGNED_MAX_X, (
        "sidebar header controls still reserve the traffic-light strip in "
        f"fullscreen: cluster at x={fullscreen_x} (expected < {FULLSCREEN_ALIGNED_MAX_X})"
    )
    _linger(page)

    # Leaving fullscreen restores the clearance (the lights are back).
    page.evaluate("window.__omniFullScreen.set(false)")
    with contextlib.suppress(PlaywrightTimeoutError):
        _wait_for_cluster_x(page, below=False, limit=TRAFFIC_LIGHT_CLEARANCE_PX - 8)
    restored_x = _cluster_x(page)
    assert restored_x >= TRAFFIC_LIGHT_CLEARANCE_PX - 8, (
        f"windowed traffic-light clearance not restored, got x={restored_x}"
    )
    _linger(page)


def test_fullscreen_shrinks_every_clearance_surface(page: Page, live_server: str) -> None:
    """Fullscreen drops the cluster offset and both padded slots' clearance."""
    page.add_init_script(_MAC_SHELL_INIT_SCRIPT)
    page.goto(live_server)
    expect(page.locator(_CLUSTER)).to_be_visible(timeout=30_000)

    measured = page.evaluate(_MEASURE_CLEARANCE_SURFACES)
    assert measured is not None, "desktop shell root not found"
    windowed, fullscreen = measured["windowed"], measured["fullscreen"]

    assert windowed["cluster"] == pytest.approx(_WINDOWED_CLUSTER_PX, abs=2)
    assert windowed["headerSlot"] == pytest.approx(_WINDOWED_SLOT_PX, abs=2)
    assert windowed["workspaceStrip"] == pytest.approx(_WINDOWED_SLOT_PX, abs=2)

    assert fullscreen["cluster"] == pytest.approx(_FULLSCREEN_CLUSTER_PX, abs=2)
    assert fullscreen["headerSlot"] == pytest.approx(_FULLSCREEN_SLOT_PX, abs=2)
    assert fullscreen["workspaceStrip"] == pytest.approx(_FULLSCREEN_SLOT_PX, abs=2)
