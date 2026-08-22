"""The sidebar server picker as the sole picker on native shells.

The native shells (Electron, iOS, Android) expose a shared, optional bridge
trio — ``getServerPicker`` / ``switchServer`` / ``openServerSetup`` on
``window.omnigentNative`` (Electron: ``window.omnigentDesktop``) — and the web
``SidebarServerPicker`` (mounted at the bottom of the conversations sidebar) is
the one and only server-switching UI. There is no native top-center pill any
more, on any platform.

The e2e_ui harness runs in a plain Chromium browser, so the bridge is stubbed
via ``add_init_script`` exactly like ``test_android_shell.py`` does: the stub
carries the picker trio and records what the SPA asks the shell to do. These
cover what the unit tests can't: the injected bridge -> ``getServerPicker`` ->
the picker actually rendering in the sidebar, opening upward, and driving
``switchServer`` / ``openServerSetup`` — plus the picker staying single-mounted
across the 768px breakpoint.
"""

from __future__ import annotations

from playwright.sync_api import Page, ViewportSize, expect

# Phone-sized viewport: on a native mobile shell the sidebar is an overlay
# drawer, which is where the picker must be reachable.
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

# Desktop-sized viewport, across Tailwind's canonical 768px (md) breakpoint,
# where the sidebar docks as the persistent left rail.
_DESKTOP_VIEWPORT: ViewportSize = {"width": 1024, "height": 844}

# Android-shaped bridge stub carrying the server-picker trio. Runs before any
# app script (``add_init_script``), so ``nativeApi()`` sees a native shell and
# ``getServerPicker()`` resolves non-null — the single gate that mounts the
# sidebar picker. ``switchServer`` / ``openServerSetup`` record their calls so
# the test can assert the SPA handed the action to the shell.
_PICKER_SHELL_INIT_SCRIPT = """
window.__omnigentSwitchedTo = [];
window.__omnigentSetupOpened = 0;
window.omnigentNative = {
  kind: "android",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onNativeInsets: function () { return function () {}; },
  getServerPicker: function () {
    return Promise.resolve({
      currentOrigin: "http://localhost:8000",
      recentServers: [
        "https://managed.example.com/",
        "https://recent.example.com/",
      ],
    });
  },
  switchServer: function (url) {
    window.__omnigentSwitchedTo.push(url);
    return Promise.resolve();
  },
  openServerSetup: function () { window.__omnigentSetupOpened += 1; },
};
"""


def test_sidebar_picker_lists_shell_servers_and_drives_the_bridge(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The picker renders from the shell's payload and routes actions to it.

    Asserts the chain end to end: the injected bridge's ``getServerPicker``
    payload mounts the picker at the sidebar's bottom showing the current
    host; its menu opens UPWARD (the trigger sits at the viewport's bottom
    edge, so a downward menu would leave the viewport) and lists the shell's
    offered servers; selecting one calls ``switchServer`` with that URL.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, _session_id = seeded_session

    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.add_init_script(_PICKER_SHELL_INIT_SCRIPT)
    # ?sidebar=open reveals the phone-width drawer on mount (same trick the
    # Android badge-notification target uses).
    page.goto(f"{base_url}/?sidebar=open")

    trigger = page.get_by_test_id("sidebar-server-picker")
    expect(trigger).to_be_visible()
    expect(trigger).to_contain_text("localhost:8000")

    trigger.click()
    menu = page.get_by_role("menu")
    expect(menu).to_be_visible()
    expect(menu).to_contain_text("managed.example.com")
    expect(menu).to_contain_text("recent.example.com")
    expect(menu).to_contain_text("Connect to new server…")

    # Upward-opening and viewport-contained: the whole menu sits above the
    # trigger's top edge and inside the viewport.
    trigger_box = trigger.bounding_box()
    menu_box = menu.bounding_box()
    assert trigger_box is not None and menu_box is not None
    assert menu_box["y"] >= 0
    assert menu_box["y"] + menu_box["height"] <= trigger_box["y"] + 1

    page.get_by_role("menuitem", name="recent.example.com").click()
    assert page.evaluate("() => window.__omnigentSwitchedTo") == ["https://recent.example.com/"]


def test_connect_to_new_server_opens_the_shells_setup(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """ "Connect to new server…" hands off to the shell's setup flow.

    On iOS/Android that is the full-screen native connect screen — the sole
    recovery/setup surface now that no native pill exists — so the picker's
    action must reach ``openServerSetup`` on the bridge.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, _session_id = seeded_session

    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.add_init_script(_PICKER_SHELL_INIT_SCRIPT)
    page.goto(f"{base_url}/?sidebar=open")

    page.get_by_test_id("sidebar-server-picker").click()
    page.get_by_role("menuitem", name="Connect to new server…").click()
    assert page.evaluate("() => window.__omnigentSetupOpened") == 1


def test_breakpoint_crossing_keeps_one_mounted_picker(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Crossing 768px keeps exactly one picker, still functional, in the rail.

    The picker is mounted once inside the shared conversations sidebar, which
    morphs between a phone drawer and the docked desktop rail. Crossing the
    breakpoint in either direction must neither duplicate nor unmount it, and
    the sidebar rail itself must stay intact around it.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, _session_id = seeded_session

    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.add_init_script(_PICKER_SHELL_INIT_SCRIPT)
    page.goto(f"{base_url}/?sidebar=open")

    picker = page.get_by_test_id("sidebar-server-picker")
    expect(picker).to_be_visible()
    expect(picker).to_have_count(1)

    # Cross up to desktop: the sidebar becomes the docked left rail and the
    # one picker instance rides along.
    page.set_viewport_size(_DESKTOP_VIEWPORT)
    sidebar = page.locator('aside[aria-label="Conversations"]')
    expect(sidebar).to_be_visible()
    expect(picker).to_have_count(1)
    expect(picker).to_be_visible()

    # And back down: still exactly one, still functional.
    page.set_viewport_size(_MOBILE_VIEWPORT)
    expect(picker).to_have_count(1)
    picker.click()
    expect(page.get_by_role("menu")).to_contain_text("recent.example.com")


def test_plain_browser_has_no_picker(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Without a native bridge the sidebar ends with the list — no picker.

    Browser-only deployments stay null-gated: ``getServerPicker`` resolves
    null off-shell, so the row never mounts.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, _session_id = seeded_session

    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.goto(f"{base_url}/?sidebar=open")

    expect(page.locator('aside[aria-label="Conversations"]')).to_be_visible()
    expect(page.get_by_test_id("sidebar-server-picker")).to_have_count(0)
