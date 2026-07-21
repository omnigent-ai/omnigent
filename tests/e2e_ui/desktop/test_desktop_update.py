"""E2E: the desktop auto-update Settings surface.

Desktop update *notifications* are shell-owned (a native corner overlay the
Electron shell renders in its own child window -- see
``web/electron/src/update_overlay.js`` + ``web/src/update-overlay.tsx``), so
they show regardless of the connected server's web-bundle version. The in-page
``UpdateBanner`` is no longer mounted by ``AppShell``; the server-page preload
is "banner-safe" (collapses available/downloaded/error-security to ``idle`` in
``preload.js``) so no web bundle -- including older ones -- can show a duplicate.

What remains in the server-rendered SPA is the Settings -> Updates section
(``web/src/pages/SettingsPage.tsx``), which still reads/writes update
preferences (mode, auto-install) and triggers a check over the bridge. The e2e_ui
harness runs the SPA in a plain Chromium browser, not Electron, so we inject a
scriptable ``window.omnigentDesktop`` stub -- including a full ``updates`` bridge
-- via ``add_init_script`` *before any app script runs* to exercise that surface.
The stub records every bridge call and captures the live ``onStatus`` subscriber
so the test can stream update-lifecycle statuses from Python
(``window.__omniUpdate.emit(...)``), modelling the main process without a real
electron-updater server.

No LLM turn is involved; the assertions are DOM- and bridge-call-based.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

# Minimal, scriptable stand-in for the Electron preload bridge. Runs before any
# app script on every navigation (add_init_script), so the SPA's feature
# detection (``isElectronShell()`` / ``updateBridge()`` in nativeBridge.ts) sees
# a desktop shell with an update bridge. The base native methods are guarded
# no-ops (badge/notify) so unrelated native calls don't throw under the stub;
# the ``updates`` object is the auto-update bridge Settings drives.
#
# Every bridge call is recorded on ``window.__omniUpdate.calls`` and the live
# ``onStatus`` subscriber is captured so the test can push new statuses via
# ``window.__omniUpdate.emit(...)`` -- modelling the main process streaming
# update lifecycle events without a real update server. ``getStatus`` seeds the
# initial state each test passes in.
_UPDATE_SHELL_INIT_SCRIPT = """
(() => {
  const state = { calls: [], onStatus: null, current: %s, config: %s };
  window.__omniUpdate = {
    calls: state.calls,
    emit: (next) => { state.current = next; if (state.onStatus) state.onStatus(next); },
  };
  const updates = {
    getConfig: () => Promise.resolve(state.config),
    getStatus: () => Promise.resolve(state.current),
    check: () => { state.calls.push("check"); return Promise.resolve(); },
    download: () => { state.calls.push("download"); return Promise.resolve(); },
    installNow: () => { state.calls.push("installNow"); return Promise.resolve(); },
    setConfig: (patch) => {
      state.calls.push("setConfig:" + JSON.stringify(patch));
      state.config = Object.assign({}, state.config, patch);
      return Promise.resolve(state.config);
    },
    onStatus: (cb) => { state.onStatus = cb; return () => { state.onStatus = null; }; },
  };
  window.omnigentDesktop = {
    kind: "electron",
    setBadgeCount: function () {},
    notify: function () { return Promise.resolve(false); },
    onNotificationActivated: function () { return function () {}; },
    getServerPicker: function () { return Promise.resolve(null); },
    switchServer: function () { return Promise.resolve(); },
    openServerSetup: function () {},
    updates: updates,
  };
})();
"""

# Default desktop update config the bridge reports: periodic checks, install on
# quit, nothing skipped -- the shape ``UpdateConfig`` in nativeBridge.ts expects.
_DEFAULT_CONFIG = '{ mode: "default", autoInstall: true, skippedVersion: null }'


def _install_update_stub(page: Page, initial_status: str, config: str = _DEFAULT_CONFIG) -> None:
    """Inject the scriptable Electron update bridge before app scripts run.

    :param page: Playwright page fixture (fresh context per test).
    :param initial_status: JS object literal for the initial ``UpdateStatus``
        ``getStatus()`` resolves, e.g. ``'{ state: "available", info: {...} }'``.
    :param config: JS object literal for the ``UpdateConfig`` ``getConfig()``
        resolves; defaults to the standard "check periodically" config.
    """
    page.add_init_script(_UPDATE_SHELL_INIT_SCRIPT % (initial_status, config))


def _bridge_calls(page: Page) -> list[str]:
    """The ordered list of update-bridge method calls recorded by the stub."""
    return page.evaluate("() => window.__omniUpdate.calls")


def test_settings_updates_section_check_and_mode(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Settings -> Updates exposes the mode selector and a working Check button.

    The desktop-only Updates section (``UpdatesSection`` in SettingsPage.tsx)
    reads the config from the bridge and lets the user trigger a check. Its
    controls sit below the header band, so this uses real clicks: the mode
    selector reflects the bridge config and ``Check for updates now`` calls the
    bridge's ``check()``.
    """
    base_url, _session_id = seeded_session

    _install_update_stub(page, '{ state: "idle" }')
    page.goto(f"{base_url}/settings/updates")

    # The section renders (bridge-backed), with its mode selector and check CTA.
    mode_select = page.get_by_test_id("update-mode-select")
    expect(mode_select).to_be_visible(timeout=30_000)
    check_button = page.get_by_role("button", name="Check for updates now")
    expect(check_button).to_be_visible()

    # Triggering a check calls the bridge.
    check_button.click()
    page.wait_for_function("() => window.__omniUpdate.calls.includes('check')")
    assert "check" in _bridge_calls(page)
