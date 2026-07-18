"""E2E: the desktop auto-update UI (``UpdateBanner`` + Settings → Updates).

Auto-update is desktop-only. ``AppShell`` renders ``UpdateBanner`` above the
routed ``Outlet`` only when ``isElectronShell()`` is true, and the banner itself
only paints when the Electron preload exposes an update bridge
(``window.omnigentDesktop.updates`` — see ``web/src/lib/nativeBridge.ts`` and
``web/src/components/UpdateBanner.tsx``). The Settings → Updates section is
gated the same way (``web/src/pages/SettingsPage.tsx``).

The e2e_ui harness runs the SPA in a plain Chromium browser, not Electron, so
by default the bridge is absent and neither surface shows. To exercise the
desktop path end to end we inject a scriptable ``window.omnigentDesktop`` stub —
including a full ``updates`` bridge — via ``add_init_script`` *before any app
script runs*, the same feature-detection stubbing ``browser/test_browser_tab.py``
uses for the embedded browser. The stub records every bridge call and captures
the live ``onStatus`` subscriber so the test can stream update-lifecycle
statuses from Python (``window.__omniUpdate.emit(...)``), modelling the main
process without a real electron-updater server.

Interaction note: ``UpdateBanner`` mounts as the first child of ``<main>``,
directly under the shell's ``ChatHeader`` — a full-width ``absolute top-0``
transparent bar (no ``pointer-events: none``) that overlays the banner's band.
A coordinate-based Playwright click on a banner button is therefore intercepted
by that header. The banner's *rendering* across the status lifecycle is asserted
by streaming statuses (no clicks); each action button's *handler→bridge wiring*
is exercised with a direct ``dispatch_event("click")`` that fires the real React
``onClick`` regardless of the floating overlay, asserting both the recorded
bridge call and the resulting visible state change. The Settings → Updates
controls sit well below the header band and are driven with real clicks.

No LLM turn is involved; the assertions are DOM- and bridge-call-based.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

# Minimal, scriptable stand-in for the Electron preload bridge. Runs before any
# app script on every navigation (add_init_script), so the SPA's feature
# detection (``isElectronShell()`` / ``updateBridge()`` in nativeBridge.ts) sees
# a desktop shell with an update bridge. The base native methods are guarded
# no-ops (badge/notify) so unrelated native calls don't throw under the stub;
# the ``updates`` object is the auto-update bridge the banner + Settings drive.
#
# Every bridge call is recorded on ``window.__omniUpdate.calls`` and the live
# ``onStatus`` subscriber is captured so the test can push new statuses via
# ``window.__omniUpdate.emit(...)`` — modelling the main process streaming
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
# quit, nothing skipped — the shape ``UpdateConfig`` in nativeBridge.ts expects.
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


def _emit_status(page: Page, status: str) -> None:
    """Stream a new ``UpdateStatus`` to the banner via the captured subscriber.

    :param status: JS object literal for the next status, e.g.
        ``'{ state: "downloading", progress: { percent: 42 } }'``.
    """
    page.evaluate(f"() => window.__omniUpdate.emit({status})")


def test_update_banner_renders_across_the_status_lifecycle(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The banner mounts in the desktop shell and tracks the live status stream.

    Proves the desktop-only chain the component test can't reach end to end:
    injected bridge → ``isElectronShell()`` gate in ``AppShell`` → banner mount →
    initial ``getStatus`` + live ``onStatus`` subscription driving per-state copy
    and controls. Streams available → downloading → downloaded and asserts each
    state's visible rendering (no clicks; see the module docstring).
    """
    base_url, session_id = seeded_session

    _install_update_stub(
        page,
        '{ state: "available", info: { version: "9.9.9", releaseNotes: "Fixes and polish." } }',
    )
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Ask the agent anything…")).to_be_visible()

    banner = page.get_by_role("region", name="Desktop update")

    # Available: version copy + the three offered actions + release notes.
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("Omnigent 9.9.9 is available.")
    expect(banner.get_by_role("button", name="Update now")).to_be_visible()
    expect(banner.get_by_role("button", name="Later")).to_be_visible()
    expect(banner.get_by_role("button", name="Skip this version")).to_be_visible()
    expect(banner.get_by_text("Release notes")).to_be_visible()

    # Downloading: progress copy + progress bar; the action buttons are gone.
    _emit_status(
        page,
        '{ state: "downloading", info: { version: "9.9.9" }, progress: { percent: 42 } }',
    )
    expect(banner).to_contain_text("Downloading Omnigent update… 42%")
    expect(banner.get_by_role("progressbar")).to_be_visible()
    expect(banner.get_by_role("button", name="Update now")).to_have_count(0)

    # Downloaded: ready-to-install copy + the restart action.
    _emit_status(page, '{ state: "downloaded", info: { version: "9.9.9" } }')
    expect(banner).to_contain_text("Omnigent 9.9.9 is ready to install.")
    expect(banner.get_by_role("button", name="Restart to update")).to_be_visible()


def test_update_banner_actions_call_the_bridge(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The banner's action buttons invoke the matching bridge calls.

    Covers the available-update *action* flow: ``Update now`` → ``download()``,
    then (after the main process streams ``downloaded``) ``Restart to update`` →
    ``installNow()``. The buttons' handler→bridge wiring is exercised with a
    direct click-event dispatch because the shell's transparent ``ChatHeader``
    overlays the banner's band (see the module docstring); the resulting visible
    state change is asserted alongside each bridge call.
    """
    base_url, session_id = seeded_session

    _install_update_stub(
        page,
        '{ state: "available", info: { version: "9.9.9" } }',
    )
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Ask the agent anything…")).to_be_visible()

    banner = page.get_by_role("region", name="Desktop update")
    expect(banner).to_contain_text("Omnigent 9.9.9 is available.")

    # Update now → the bridge is asked to download.
    banner.get_by_role("button", name="Update now").dispatch_event("click")
    page.wait_for_function("() => window.__omniUpdate.calls.includes('download')")
    assert "download" in _bridge_calls(page)

    # The main process streams the finished download; the banner swaps to the
    # ready-to-install state (the same onStatus channel the real shell uses).
    _emit_status(page, '{ state: "downloaded", info: { version: "9.9.9" } }')
    restart = banner.get_by_role("button", name="Restart to update")
    expect(restart).to_be_visible()

    # Restart to update → the bridge is asked to install now.
    restart.dispatch_event("click")
    page.wait_for_function("() => window.__omniUpdate.calls.includes('installNow')")
    assert "installNow" in _bridge_calls(page)


def test_update_banner_skip_persists_and_hides(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """``Skip this version`` persists the skip via the bridge and hides the banner.

    The banner is a standing surface, so a user must be able to dismiss a
    version for good. Activating ``Skip this version`` calls
    ``setConfig({ skippedVersion })`` and, once persisted, the banner stops
    rendering that version. (Dispatch-click for the same overlay reason.)
    """
    base_url, session_id = seeded_session

    _install_update_stub(
        page,
        '{ state: "available", info: { version: "9.9.9" } }',
    )
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Ask the agent anything…")).to_be_visible()

    banner = page.get_by_role("region", name="Desktop update")
    expect(banner).to_contain_text("Omnigent 9.9.9 is available.")

    banner.get_by_role("button", name="Skip this version").dispatch_event("click")

    # The skip is persisted through the bridge with the offered version…
    page.wait_for_function("() => window.__omniUpdate.calls.some(c => c.startsWith('setConfig:'))")
    assert 'setConfig:{"skippedVersion":"9.9.9"}' in _bridge_calls(page)
    # …and the banner for that version is gone.
    expect(page.get_by_role("region", name="Desktop update")).to_have_count(0)


def test_settings_updates_section_check_and_mode(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Settings → Updates exposes the mode selector and a working Check button.

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


def test_no_update_banner_in_plain_browser(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A plain browser tab (no Electron bridge) never shows the update banner.

    Without the ``window.omnigentDesktop`` stub, ``isElectronShell()`` is false,
    so ``AppShell`` skips the banner entirely — the gate that keeps the desktop
    updater UI off the plain web app (which has no updater to drive). This is
    the half of the contract only an end-to-end browser run can prove.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Ask the agent anything…")).to_be_visible()

    expect(page.get_by_role("region", name="Desktop update")).to_have_count(0)
