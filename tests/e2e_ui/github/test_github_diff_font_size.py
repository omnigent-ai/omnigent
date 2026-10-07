"""E2E: the GitHub tab's change diff is sized by the Interface font size.

``@pierre/diffs`` renders the stacked diff inside a ``diffs-container`` shadow
root whose stylesheet falls back to a fixed 13px/20px unless the app binds its
typography variables, while the rest of the Workspace rail uses the ``text-ui``
step that follows Appearance → "Interface font size". This pins that the diff's
code renders smaller than the panel text beside it (the app's mono compensation)
and tracks a live change of that setting while mounted.

The GitHub resource endpoints are answered with canned JSON (see
``test_github_tab.py``), so no ``gh``/``git`` runs.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail
from tests.e2e_ui.github.test_github_tab import _stub_github

# Computed typography of the diff's code lines (inside the FileDiff shadow root)
# and of the sticky file header beside them.
_MEASURE_JS = """
() => {
  const rail = document.querySelector(
    'aside[aria-label="Workspace"], [role="complementary"][aria-label="Workspace"]');
  const px = (el, prop) => parseFloat(getComputedStyle(el)[prop]);
  const host = Array.from(rail.querySelectorAll('diffs-container')).find(
    (h) => h.shadowRoot?.querySelector('[data-line]'));
  const lines = Array.from(host.shadowRoot.querySelectorAll('[data-line]'));
  const tops = lines.map((l) => l.getBoundingClientRect().top);
  const header = rail.querySelector('button.sticky span.text-ui');
  return {
    diffFontSize: px(lines[0], 'fontSize'),
    diffLineHeight: px(lines[0], 'lineHeight'),
    diffFontFamily: getComputedStyle(lines[0]).fontFamily,
    rowPitch: Math.round((tops[1] - tops[0]) * 100) / 100,
    headerFontSize: px(header, 'fontSize'),
    appMonoStack: getComputedStyle(document.documentElement).getPropertyValue('--font-mono'),
  };
}
"""


def _families(stack: str) -> list[str]:
    return [family.strip().strip("\"'") for family in stack.split(",")]


def test_github_diff_text_tracks_interface_font_size(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    _stub_github(page)

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name="GitHub").click()
    expect(rail.get_by_text("Add the GitHub tab")).to_be_visible(timeout=30_000)
    rail.get_by_role("tablist", name="Pull request").get_by_role("tab", name="Changes").click()
    # The diff lines live in the FileDiff shadow root; Playwright pierces it.
    expect(rail.locator("diffs-container").get_by_text("added line")).to_be_visible(timeout=30_000)

    at_default: dict[str, Any] = page.evaluate(_MEASURE_JS)
    print(f"GitHub diff typography (Interface 13): {json.dumps(at_default)}")
    assert at_default["headerFontSize"] == 13
    assert at_default["diffFontSize"] < at_default["headerFontSize"], (
        f"diff code renders at {at_default['diffFontSize']}px, not smaller than the panel "
        f"text beside it at {at_default['headerFontSize']}px"
    )
    assert at_default["rowPitch"] == pytest.approx(at_default["diffLineHeight"], abs=0.05)
    assert _families(at_default["diffFontFamily"]) == _families(at_default["appMonoStack"])

    # Lower the Interface font size the way Settings → Appearance applies it
    # (lib/uiFontPreferences.ts) and re-measure the already-mounted diff.
    page.evaluate("document.documentElement.style.setProperty('--desktop-ui-font-size', '11px')")
    at_small: dict[str, Any] = page.evaluate(_MEASURE_JS)
    print(f"GitHub diff typography (Interface 11): {json.dumps(at_small)}")
    assert at_small["headerFontSize"] == 11
    assert at_small["diffFontSize"] < at_small["headerFontSize"], (
        f"diff code renders at {at_small['diffFontSize']}px, not smaller than the panel "
        f"text beside it at {at_small['headerFontSize']}px"
    )
    # The diff scales with the setting rather than sitting at some fixed size.
    assert at_small["diffFontSize"] / at_small["headerFontSize"] == pytest.approx(
        at_default["diffFontSize"] / at_default["headerFontSize"], abs=0.01
    )
    assert at_small["rowPitch"] == pytest.approx(at_small["diffLineHeight"], abs=0.05)
    assert at_small["rowPitch"] < at_default["rowPitch"]
