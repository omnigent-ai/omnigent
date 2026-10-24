"""E2E: leaving Settings returns to the conversation you came from.

Settings renders into the shared ``AppShell`` outlet under ``/settings`` — a
URL that carries no conversation id. The "Back" link in the
settings sidebar (``SettingsSidebarBody`` in ``shell/settingsNav.tsx``) used to
be hardcoded to ``/``, so leaving settings always dropped the user on the home
landing page instead of the conversation they were viewing.

The Sidebar stays mounted across the transition into settings and now records
the last non-settings location (via ``useTrackSettingsReturn``); the back link
points at it. This test drives the real in-app flow — open a conversation, open
Settings from the sidebar, click Back — and asserts the URL returns to
``/c/<session_id>`` rather than ``/``.

Opening Settings is also sampled every animation frame, so a one-frame layout
glitch that a screenshot would miss still fails.

No LLM turn is involved.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from playwright.sync_api import Page, expect

_SETTINGS_TRANSITION_PROBE = """
(() => {
  const rect = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.x), w: Math.round(r.width) };
  };
  const samples = [];
  let running = true;
  let last = null;
  const snapshot = () => {
    const aside = document.querySelector('aside[aria-label="Conversations"]');
    const main = document.querySelector("main");
    return {
      path: location.pathname,
      settingsNav: !!document.querySelector('[data-testid="settings-nav-general"]'),
      asideCollapsed: aside ? aside.hasAttribute("data-collapsed") : null,
      aside: rect(aside),
      main: rect(main),
      mainChildren: main ? main.children.length : null,
      loading: !!document.querySelector('[role="status"][aria-busy="true"]'),
    };
  };
  const tick = () => {
    const sample = snapshot();
    const key = JSON.stringify(sample);
    if (key !== last) {
      samples.push({ t: Math.round(performance.now()), ...sample });
      last = key;
    }
    if (running) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  window.__settingsTransitionProbe = {
    stop: () => {
      running = false;
      return samples;
    },
  };
})()
"""


def _record_settings_open(page: Page, open_settings: Callable[[], None]) -> list[dict[str, Any]]:
    page.evaluate(_SETTINGS_TRANSITION_PROBE)
    page.wait_for_timeout(300)
    open_settings()
    page.wait_for_url("**/settings/general", timeout=30_000)
    expect(page.get_by_role("link", name="Back", exact=True)).to_be_visible(timeout=30_000)
    expect(
        page.get_by_role("main").get_by_role("heading", name="General", exact=True)
    ).to_be_visible(timeout=30_000)
    page.wait_for_timeout(500)
    samples = page.evaluate("window.__settingsTransitionProbe.stop()")
    assert samples, "the per-frame probe captured no frames"
    return samples


def _describe_frames(samples: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(sample, separators=(",", ":")) for sample in samples)


_NATIVE_OPEN_PATH_INIT_SCRIPT = """
window.__nativeOpenPathListener = null;
window.__documentLoadMarker = crypto.randomUUID();
window.omnigentDesktop = {
  kind: "electron",
  setBadgeCount() {},
  notify() { return Promise.resolve(true); },
  onOpenPath(callback) {
    window.__nativeOpenPathListener = callback;
    return () => {
      if (window.__nativeOpenPathListener === callback) {
        window.__nativeOpenPathListener = null;
      }
    };
  },
};
"""


def test_native_open_path_navigates_to_settings_without_reload(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The desktop bridge's basename-less ``/settings`` path routes in place.

    Bare ``/settings`` canonicalizes to ``/settings/general``.
    """
    base_url, session_id = seeded_session
    page.add_init_script(_NATIVE_OPEN_PATH_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page).to_have_url(f"{base_url}/c/{session_id}")
    page.wait_for_function("typeof window.__nativeOpenPathListener === 'function'")
    load_marker = page.evaluate("window.__documentLoadMarker")

    page.evaluate("window.__nativeOpenPathListener('/settings')")

    expect(page).to_have_url(f"{base_url}/settings/general", timeout=30_000)
    expect(page.get_by_role("link", name="Back", exact=True)).to_be_visible(timeout=30_000)
    assert page.evaluate("window.__documentLoadMarker") == load_marker


def test_settings_back_returns_to_conversation(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Conversation → Settings → Back lands back on the conversation, not home.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")
    expect(page).to_have_url(f"{base_url}/c/{session_id}")

    # Enter Settings from the sidebar footer control. The conversations sidebar
    # stays mounted and swaps its body to the settings section nav.
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=30_000)
    samples = _record_settings_open(page, page.get_by_test_id("settings-button").click)

    # The settings section nav renders in place of the conversation list.
    back = page.get_by_role("link", name="Back", exact=True)
    expect(back).to_be_visible(timeout=30_000)

    # Back returns to the conversation we came from — the fix. A regression to
    # the hardcoded "/" would land on the home landing page instead.
    back.click()
    expect(page).to_have_url(f"{base_url}/c/{session_id}", timeout=30_000)

    blank = [s for s in samples if s["settingsNav"] and s["mainChildren"] == 0]
    assert not blank, (
        "Settings nav was shown beside an empty main area:\n"
        + _describe_frames(blank)
        + "\n--- full transition ---\n"
        + _describe_frames(samples)
    )
    assert not any(s["loading"] for s in samples), (
        "Opening Settings flashed the page-loading fallback:\n" + _describe_frames(samples)
    )


def test_settings_open_from_collapsed_sidebar_pins_sidebar_before_first_paint(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """With the sidebar collapsed, Settings never paints before the sidebar is pinned open."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=30_000)

    # The collapsed sidebar is aria-hidden, so locate it by CSS rather than role.
    sidebar = page.locator('aside[aria-label="Conversations"]')
    page.get_by_role("button", name="Close sidebar").click()
    expect(sidebar).to_have_attribute("data-collapsed", "true", timeout=10_000)

    # The in-sidebar gear is clipped while collapsed, so use the Settings hotkey.
    samples = _record_settings_open(page, lambda: page.keyboard.press("ControlOrMeta+Alt+,"))

    expect(sidebar).not_to_have_attribute("data-collapsed", "true")
    collapsed = [s for s in samples if s["settingsNav"] and s["asideCollapsed"]]
    assert not collapsed, (
        "Settings rendered while the sidebar was still collapsed:\n"
        + _describe_frames(collapsed)
        + "\n--- full transition ---\n"
        + _describe_frames(samples)
    )


def test_single_user_hides_members_and_sharing_settings(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The shared e2e server is single-user (OMNIGENT_LOCAL_SINGLE_USER=1), so
    the Settings Admin group drops Members and Sharing (no other users to
    manage or share with) while keeping Policies (global policies apply to a
    solo user's own sessions).

    Counterpart to the multi-user case in
    ``test_permissions_modal.py::test_multi_user_admin_sees_members_and_
    sharing_settings``, where the same auth shape keeps all three.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    page.get_by_test_id("settings-button").click()
    page.wait_for_url("**/settings**", timeout=30_000)
    # Policies anchors the Admin group in single-user mode; wait for it so the
    # absence assertions run against a rendered nav, not an unmounted one.
    expect(page.get_by_test_id("settings-nav-policies")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("settings-nav-members")).to_have_count(0)
    expect(page.get_by_test_id("settings-nav-sharing")).to_have_count(0)
