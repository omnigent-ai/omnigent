"""E2E: the GitHub tab's change diff is sized like the rest of the panel.

The stacked diff is rendered by ``@pierre/diffs`` inside a ``diffs-container``
shadow root. The rest of the Workspace rail (file-section headers, file tree,
panel heading) uses the app's ``text-ui`` step, which follows the Appearance
"Interface font size" setting. This pins that the diff's code text never renders
larger than the panel text beside it, at the default preference and at the
smallest supported Interface font size.

The GitHub resource endpoints are answered with canned JSON (see
``test_github_tab.py``), so no ``gh``/``git`` runs; the sizing under test is
client-side rendering of the returned patch.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail
from tests.e2e_ui.github.test_github_tab import _CHANGES, _INFO

_BEFORE = "\n".join(f"line{i}" for i in range(1, 9)) + "\n"
_AFTER = (
    "\n".join(
        ["line1", "line2", "import os", "import sys", "", "def main():", "    return 0"]
        + [f"line{i}" for i in range(3, 9)]
    )
    + "\n"
)

_PATCH = (
    "diff --git a/src/app/main.py b/src/app/main.py\n"
    "index e69de29..4b825dc 100644\n"
    "--- a/src/app/main.py\n"
    "+++ b/src/app/main.py\n"
    "@@ -1,8 +1,13 @@\n"
    " line1\n"
    " line2\n"
    "+import os\n"
    "+import sys\n"
    "+\n"
    "+def main():\n"
    "+    return 0\n"
    " line3\n"
    " line4\n"
    " line5\n"
    " line6\n"
    " line7\n"
    " line8\n"
)

# Computed typography of the first rendered diff line (inside the FileDiff
# shadow root) and of the panel text around it.
_MEASURE_JS = """
() => {
  const rail = document.querySelector(
    'aside[aria-label="Workspace"], [role="complementary"][aria-label="Workspace"]');
  const style = (el) => {
    const cs = getComputedStyle(el);
    return {
      fontSize: parseFloat(cs.fontSize), lineHeight: cs.lineHeight, fontFamily: cs.fontFamily,
    };
  };
  const out = { diff: null, ui: {} };
  for (const host of rail.querySelectorAll('diffs-container')) {
    const lines = Array.from(host.shadowRoot?.querySelectorAll('[data-line]') ?? []);
    if (!lines.length) continue;
    const rects = lines.slice(0, 13).map((l) => l.getBoundingClientRect());
    out.diff = {
      host: style(host),
      line: style(lines[0]),
      lineCount: lines.length,
      pitches: rects.slice(1).map((r, i) => Math.round((r.top - rects[i].top) * 100) / 100),
    };
    break;
  }
  const header = rail.querySelector('button.sticky span.text-ui');
  if (header) out.ui.sectionHeader = style(header);
  const heading = rail.querySelector('h2');
  if (heading) out.ui.panelHeading = style(heading);
  const treeRow = Array.from(rail.querySelectorAll('button')).find(
    (b) => !b.classList.contains('sticky') && /main\\.py/.test(b.textContent || ''));
  if (treeRow) out.ui.treeRow = style(treeRow.querySelector('span.truncate') || treeRow);
  out.ui.body = style(document.body);
  const composer = document.querySelector('textarea[aria-label="Message the agent"]');
  if (composer) out.ui.composer = style(composer);
  out.ui.textUiVar = getComputedStyle(document.documentElement)
    .getPropertyValue('--text-ui').trim();
  return out;
}
"""


def _stub_github_with_patch(page: Page) -> None:
    page.route(re.compile(r"/resources/github(?:\?|$)"), lambda r: r.fulfill(json=_INFO))
    page.route(re.compile(r"/resources/github/changes"), lambda r: r.fulfill(json=_CHANGES))
    page.route(
        re.compile(r"/resources/github/diff(?:\?|$)"),
        lambda r: r.fulfill(json={"object": "session.github.pr_diff", "patch": _PATCH}),
    )
    page.route(
        re.compile(r"/resources/github/diff/"),
        lambda r: r.fulfill(
            json={
                "object": "session.github.file_diff",
                "path": "src/app/main.py",
                "before": _BEFORE,
                "after": _AFTER,
            }
        ),
    )


def _open_changes_diff(page: Page, base_url: str, session_id: str) -> None:
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name="GitHub").click()
    expect(rail.get_by_text("Add the GitHub tab")).to_be_visible(timeout=30_000)
    rail.get_by_role("tablist", name="Pull request").get_by_role("tab", name="Changes").click()
    # The added lines live in the FileDiff shadow root; Playwright pierces it.
    expect(rail.locator("diffs-container").get_by_text("def main():")).to_be_visible(
        timeout=30_000
    )


_PREFERENCES: dict[str, dict[str, int]] = {
    "default-prefs": {},
    "interface-font-11": {"omnigent:ui-font-size": 11},
}


@pytest.mark.parametrize("prefs", list(_PREFERENCES), ids=list(_PREFERENCES))
def test_github_diff_text_is_not_larger_than_panel_text(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    prefs: str,
) -> None:
    base_url, session_id = seeded_session
    page: Page = request.getfixturevalue("page")
    page.add_init_script("window.localStorage.setItem('omnigent:default-workspace-panel', 'open')")
    for key, value in _PREFERENCES[prefs].items():
        page.add_init_script(f"window.localStorage.setItem({key!r}, {str(value)!r})")
    _stub_github_with_patch(page)

    _open_changes_diff(page, base_url, session_id)
    measured: dict[str, Any] = page.evaluate(_MEASURE_JS)
    page.get_by_role("complementary", name="Workspace").screenshot(
        path=tmp_path / f"github-changes-{prefs}.png", animations="disabled"
    )
    print(f"GitHub diff typography ({prefs}): {json.dumps(measured)}")

    assert measured["diff"] is not None, measured
    diff_font = measured["diff"]["line"]["fontSize"]
    panel_font = measured["ui"]["sectionHeader"]["fontSize"]
    assert diff_font <= panel_font, (
        f"diff code renders at {diff_font}px ({measured['diff']['line']['lineHeight']} lines) "
        f"but the panel text beside it is {panel_font}px "
        f"({measured['ui']['sectionHeader']['lineHeight']} lines)"
    )
