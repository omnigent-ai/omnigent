"""Android WebView shell: web-layer feature detection and the safe-area fold.

The native Android shell (``web/android``) loads the SPA and injects
``window.omnigentNative = {kind: "android", ...}``. The web layer feature-detects
it (``isAndroidShell()`` in ``web/src/lib/nativeBridge.ts``) and, when true, the
``AppShell`` tags its root with ``data-android-native="true"``
(``web/src/shell/AppShell.tsx``). That attribute gates the Android-specific
chrome in ``index.css`` — most importantly the safe-area fold: unlike iOS,
Android WebView reports ``env(safe-area-inset-*)`` as 0, so the shell injects the
OS-measured inset as ``--omnigent-android-safe-area-*`` and ``index.css`` folds
it into the shared ``--omnigent-safe-top/bottom`` with ``max()``.

The e2e_ui harness runs the SPA in a plain Chromium browser, not the Android
WebView, so ``isAndroidShell()`` is false by default. To exercise the shell path
end-to-end we inject a minimal ``window.omnigentNative`` stub via
``add_init_script`` *before any app script runs* — the same feature-detection
stubbing the desktop shell tests use (``sessions/test_pinned_session_hotkeys.py``
injects ``window.omnigentDesktop``).

These cover the chain the ``nativeBridge`` unit tests can't reach end to end:
the injected bridge -> ``isAndroidShell()`` -> the ``AppShell``
``data-android-native`` attribute -> the ``index.css`` ``max()`` fold that lets
the injected OS inset reach the layout's shared vars.
"""

from __future__ import annotations

from playwright.sync_api import Page, ViewportSize, expect

# A phone-sized viewport: the Android shell is a mobile surface, and the narrow
# width is where the sidebar behaves as an overlay drawer (the
# ``[data-android-native]`` drawer rules this change adds). The
# ``data-android-native`` tag itself is viewport-independent.
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

# Minimal stand-in for the Android WebView bridge (``web/android``'s
# ``NativeBridgeScript``). Runs before any app script on every navigation
# (``add_init_script``), so ``nativeApi()`` in ``nativeBridge.ts`` — which now
# accepts ``kind === "android"`` — sees a native shell. Every method is a guarded
# no-op: ``kind`` is what ``isAndroidShell()`` keys off, and the rest keep
# unrelated native calls (badge / notify / inset subscription) from throwing
# under the stub.
_ANDROID_SHELL_INIT_SCRIPT = """
window.omnigentNative = {
  kind: "android",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onNativeInsets: function () { return function () {}; },
};
"""

# Read the *resolved* ``--omnigent-safe-top`` in pixels. ``getComputedStyle`` on a
# custom property returns its declared text (the ``max()`` expression), so instead
# size a throwaway probe by ``var(--omnigent-safe-top)`` and read its computed
# height, which resolves the fold.
_READ_SAFE_TOP_PX = """
() => {
  const probe = document.createElement('div');
  probe.style.cssText =
    'position:absolute;visibility:hidden;pointer-events:none;height:var(--omnigent-safe-top)';
  document.body.appendChild(probe);
  const px = getComputedStyle(probe).height;
  probe.remove();
  return px;
}
"""


def test_android_shell_tags_root_and_folds_os_inset(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Under the injected Android bridge, the SPA tags its root and folds the inset.

    Asserts (1) the app-shell carries ``data-android-native="true"`` — i.e.
    ``isAndroidShell()`` -> ``AppShell`` wiring fires — and (2) the OS inset the
    shell injects as ``--omnigent-android-safe-area-top`` reaches the shared
    ``--omnigent-safe-top`` through the ``index.css`` ``max()`` fold (which is 0
    in a plain browser, where ``env(safe-area-inset-top)`` is also 0).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session

    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.add_init_script(_ANDROID_SHELL_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")

    shell = page.locator(".app-shell")
    expect(shell).to_have_attribute("data-android-native", "true")

    # No native inset injected yet: env(safe-area-inset-top) is 0 in a plain
    # browser and the Android var is unset, so the fold resolves to 0.
    assert page.evaluate(_READ_SAFE_TOP_PX) == "0px"

    # The native layer pushes the measured OS inset as
    # --omnigent-android-safe-area-top; index.css folds it into
    # --omnigent-safe-top via max(), so the layout reads the real inset.
    page.evaluate(
        "() => document.documentElement.style"
        ".setProperty('--omnigent-android-safe-area-top', '40px')"
    )
    assert page.evaluate(_READ_SAFE_TOP_PX) == "40px"


def test_no_android_tag_or_fold_in_plain_browser(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A plain browser tab (no bridge) gets neither the tag nor the fold.

    Without the ``window.omnigentNative`` stub, ``isAndroidShell()`` is false, so
    the app-shell must NOT carry ``data-android-native`` and the
    ``--omnigent-android-safe-area-*`` fold must contribute nothing — the gate
    that keeps the Android chrome off the plain web app. This is the half of the
    contract only an end-to-end browser run can prove.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session

    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")

    shell = page.locator(".app-shell")
    expect(shell).to_be_visible()
    assert shell.get_attribute("data-android-native") is None

    # The web app never injects --omnigent-android-safe-area-*, so with
    # env(safe-area-inset-top) also 0 here the shared inset stays 0.
    assert page.evaluate(_READ_SAFE_TOP_PX) == "0px"


# Bridge stub that also CAPTURES the notification-activation callback the SPA
# registers (``useIdleNotifications`` -> ``onNativeNotificationActivated``), so a
# test can fire it the way the Android shell does when its badge notification is
# tapped. The real shell calls the callback with the notification's stored
# ``navigatePath``.
_ANDROID_ACTIVATION_INIT_SCRIPT = """
window.__omnigentActivations = [];
window.omnigentNative = {
  kind: "android",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function (cb) {
    window.__omnigentActivate = cb;
    return function () { delete window.__omnigentActivate; };
  },
  onNativeInsets: function () { return function () {}; },
};
"""


def test_badge_notification_activation_navigates_to_target(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Tapping the Android badge notification routes the SPA to its target.

    The chain under test is the one the unit tests can't reach end to end: the
    shell's ``onNotificationActivated`` callback (registered by
    ``useIdleNotifications``) receives the badge's ``navigatePath`` and the SPA
    navigates there in place — no reload, sidebar-closed mobile layout intact.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session

    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.add_init_script(_ANDROID_ACTIVATION_INIT_SCRIPT)
    page.goto(f"{base_url}/")

    # The SPA registers its activation callback on mount.
    page.wait_for_function("() => typeof window.__omnigentActivate === 'function'")

    # Fire the callback the way the shell does for a single-unread badge tap.
    page.evaluate("path => window.__omnigentActivate(path)", f"/c/{session_id}")
    page.wait_for_url(f"**/c/{session_id}")
    expect(page.locator(".app-shell")).to_have_attribute("data-android-native", "true")


def test_sidebar_open_param_reveals_drawer_and_strips_itself(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """``?sidebar=open`` (the multi-unread badge target) opens the drawer once.

    A multi-unread badge notification targets ``/?sidebar=open`` so the tap
    lands on the session list instead of a bare composer. The param must open
    the phone-width drawer and then strip itself from the URL (one-shot,
    ``replace``), so a later in-app navigation doesn't re-trigger it.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, _session_id = seeded_session

    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.add_init_script(_ANDROID_SHELL_INIT_SCRIPT)
    drawer = page.locator('aside[aria-label="Conversations"]')

    # Control: without the param the phone-width drawer starts closed
    # (data-collapsed; CSS keeps the off-canvas box technically "visible",
    # so the attribute is the reliable open/closed signal).
    page.goto(f"{base_url}/")
    expect(drawer).to_have_attribute("data-collapsed", "true")

    page.goto(f"{base_url}/?sidebar=open")
    expect(drawer).not_to_have_attribute("data-collapsed", "true")
    page.wait_for_function("() => !window.location.search.includes('sidebar')")
