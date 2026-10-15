"""E2E: entering Settings must not flash intermediate layouts.

Settings renders into the ``AppShell`` outlet while the conversations sidebar
stays mounted and swaps its body to the section nav (``settingsNav.tsx``). A
per-frame probe records the layout of the persistent chrome across the
transition so a one-frame glitch — too short for a screenshot — still fails.

No LLM turn is involved.
"""

from __future__ import annotations

import json
from typing import Any

from playwright.sync_api import Page, expect

_PROBE_INSTALL = """
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
      conversationList: !!document.querySelector(".sidebar-header-row"),
      asideCollapsed: aside ? aside.hasAttribute("data-collapsed") : null,
      aside: rect(aside),
      main: rect(main),
      mainChildren: main ? main.children.length : null,
      settingsTitle: !!document.querySelector(".settings-page-title"),
      loading: !!document.querySelector('[role="status"][aria-busy="true"]'),
    };
  };
  const tick = () => {
    const s = snapshot();
    const key = JSON.stringify(s);
    if (key !== last) {
      samples.push({ t: Math.round(performance.now()), ...s });
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


def _wait_for_settings_settled(page: Page) -> None:
    page.wait_for_url("**/settings/general", timeout=30_000)
    expect(page.get_by_role("link", name="Back", exact=True)).to_be_visible(timeout=30_000)
    expect(
        page.get_by_role("main").get_by_role("heading", name="General", exact=True)
    ).to_be_visible(timeout=30_000)
    page.wait_for_timeout(500)


def _collect_transition(page: Page) -> list[dict[str, Any]]:
    samples = page.evaluate("window.__settingsTransitionProbe.stop()")
    assert samples, "the per-frame probe captured no frames"
    return samples


def _describe(samples: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(s, separators=(",", ":")) for s in samples)


def test_settings_open_keeps_main_populated_while_sidebar_swaps(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The main area never goes empty while the sidebar already shows the Settings nav."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=30_000)

    page.evaluate(_PROBE_INSTALL)
    page.wait_for_timeout(300)
    page.get_by_test_id("settings-button").click()
    _wait_for_settings_settled(page)
    samples = _collect_transition(page)

    assert not any(s["loading"] for s in samples), _describe(samples)
    blank = [s for s in samples if s["settingsNav"] and s["mainChildren"] == 0]
    assert not blank, (
        "Settings nav was shown beside an empty main area:\n"
        + _describe(blank)
        + "\n--- full transition ---\n"
        + _describe(samples)
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

    page.evaluate(_PROBE_INSTALL)
    page.wait_for_timeout(300)
    # The in-sidebar gear is clipped while collapsed; the hotkey is the Settings path here.
    # ControlOrMeta maps to the platform modifier (Cmd on macOS, Ctrl elsewhere).
    page.keyboard.press("ControlOrMeta+Alt+,")
    _wait_for_settings_settled(page)
    samples = _collect_transition(page)

    expect(sidebar).not_to_have_attribute("data-collapsed", "true")
    two_step = [s for s in samples if s["settingsNav"] and s["asideCollapsed"]]
    assert not two_step, (
        "Settings rendered while the sidebar was still collapsed:\n"
        + _describe(two_step)
        + "\n--- full transition ---\n"
        + _describe(samples)
    )
