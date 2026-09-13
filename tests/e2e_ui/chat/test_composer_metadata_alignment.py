"""UI regression: composer workspace-bar metadata sits on one centerline,
with visible gaps that group each icon with its own label.

The gray bar above the composer shows two kinds of controls:

- the 24px-tall workspace/branch buttons on the left, and
- the shorter trailing metadata cluster on the right — the GitHub PR pair
  (GitHub mark + ``#N``) and the context pair (usage ring + ``NN%``).

Journey (the report's steps, driven on the real SPA against a live server
with stubbed GitHub/context data — no LLM turns):

1. open a session with a workspace, branch, associated pull request, and
   nonzero context usage
2. at the given Appearance text size, wait for the gray bar with no
   background-task or subagent badges visible
3. compare the GitHub/context vertical centers against the folder/branch
   centers
4. compare the visible icon-to-label gaps within each metadata pair, and the
   visible separation between the unrelated pairs

Reported failure this guards against:

- the bar top-aligns the metadata with the taller workspace controls, so the
  GitHub/context centers ride ~2px above the folder/branch centers at the
  default desktop text size;
- the within-pair icon/label gaps (6px) are larger than the gap between the
  unrelated pairs (4px), and the ring's SVG adds ~1.5px of transparent
  horizontal padding — so the context wheel visibly sits closer to the
  unrelated PR count than to its own percentage, and the two pairs' visible
  icon-to-label gaps are unequal.

"Visible" gaps are measured to each SVG icon's *drawn* extent (geometry plus
stroke, mapped through the viewBox), not its element box, because equal CSS
gaps do not produce equal visible gaps when an icon carries transparent
padding.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry
from tests.e2e_ui.github.test_github_tab import _INFO, _PR_NUMBER

_WORKSPACE = "/workspace/demo-app"
_BRANCH = "feature/metadata-alignment"
_HOST_ID = "composer-metadata-host"

# Snapshot-seeded context figures: 84,000 / 200,000 tokens -> a 42% ring.
_CONTEXT_WINDOW = 200_000
_LAST_TOTAL_TOKENS = 84_000

# One shared centerline: a metadata indicator's vertical center may drift at
# most this far from the workspace control's center (subpixel slack only).
_CENTERLINE_TOLERANCE_PX = 1.0
# Equal visible icon-to-label gaps: the two pairs may differ at most this much.
_PAIR_GAP_TOLERANCE_PX = 1.0
# Grouping slack: the wheel may sit at most this much closer to the unrelated
# PR label than to its own percentage before the grouping reads wrong.
_GROUPING_TOLERANCE_PX = 0.5

# Drawn extent of an SVG icon in viewport pixels: the union of its shapes'
# geometry boxes, widened by half of any stroke, mapped through the viewBox.
# getBoundingClientRect alone would include transparent padding (the ring's
# 16px box only draws from x=1.5 to x=14.5), which is exactly what the bug
# hides behind.
_SVG_DRAWN_RECT_JS = """
(svg) => {
  const rect = svg.getBoundingClientRect();
  const vb =
    svg.viewBox && svg.viewBox.baseVal && svg.viewBox.baseVal.width
      ? svg.viewBox.baseVal
      : { x: 0, y: 0, width: rect.width, height: rect.height };
  const scaleX = rect.width / vb.width;
  const scaleY = rect.height / vb.height;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  const shapes = svg.querySelectorAll(
    "circle, ellipse, line, path, polygon, polyline, rect"
  );
  for (const shape of shapes) {
    const box = shape.getBBox();
    const style = getComputedStyle(shape);
    const stroke =
      style.stroke !== "none" ? parseFloat(style.strokeWidth) || 0 : 0;
    minX = Math.min(minX, box.x - stroke / 2);
    minY = Math.min(minY, box.y - stroke / 2);
    maxX = Math.max(maxX, box.x + box.width + stroke / 2);
    maxY = Math.max(maxY, box.y + box.height + stroke / 2);
  }
  return {
    left: rect.left + (minX - vb.x) * scaleX,
    right: rect.left + (maxX - vb.x) * scaleX,
    top: rect.top + (minY - vb.y) * scaleY,
    bottom: rect.top + (maxY - vb.y) * scaleY,
  };
}
"""


def _box(locator: Locator) -> dict[str, float]:
    """The locator's border box as left/right/top/bottom viewport pixels."""
    box = locator.bounding_box()
    assert box is not None, f"no bounding box for {locator}"
    return {
        "left": box["x"],
        "right": box["x"] + box["width"],
        "top": box["y"],
        "bottom": box["y"] + box["height"],
    }


def _drawn_rect(svg: Locator) -> dict[str, float]:
    """The SVG icon's *drawn* (visible-ink) rect in viewport pixels."""
    rect = svg.evaluate(_SVG_DRAWN_RECT_JS)
    assert all(v == v for v in rect.values()), f"unrendered svg: {rect}"  # NaN guard
    return rect


def _center_y(rect: dict[str, float]) -> float:
    return (rect["top"] + rect["bottom"]) / 2


@pytest.mark.parametrize(
    "viewport_width",
    [1280, pytest.param(390, marks=pytest.mark.browser_context_args(has_touch=True))],
    ids=["desktop", "mobile"],
)
@pytest.mark.parametrize("font_size", [13, 18], ids=["default-font", "large-font"])
def test_composer_metadata_centerline_and_grouping(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    viewport_width: int,
    font_size: int,
) -> None:
    """Composer metadata shares the workspace controls' centerline, and the
    visible gaps group each icon with its own label — not with a neighbor."""
    base_url, session_id = seeded_session
    is_mobile = viewport_width < 768

    # ── Step 1: a session with workspace, branch, PR, and context usage ──
    # Stub the data sources the bar reads, exactly as the sibling GitHub-tab
    # tests do: the session snapshot carries workspace/branch/host plus the
    # context figures the ring seeds from; the host worktrees list echoes the
    # branch; /resources/github reports one associated PR.
    def session_details(route: Route) -> None:
        response = fetch_with_retry(route)
        snapshot = response.json()
        snapshot.update(
            workspace=_WORKSPACE,
            git_branch=_BRANCH,
            host_id=_HOST_ID,
            context_window=_CONTEXT_WINDOW,
            last_total_tokens=_LAST_TOTAL_TOKENS,
        )
        route.fulfill(response=response, json=snapshot)

    page.route(re.compile(rf"/v1/sessions/{session_id}(?:\?.*)?$"), session_details)
    page.route(
        f"**/v1/hosts/{_HOST_ID}/worktrees?*",
        lambda route: route.fulfill(
            json={
                "data": [
                    {"path": _WORKSPACE, "branch": _BRANCH, "is_main": True, "detached": False}
                ]
            }
        ),
    )
    page.route(re.compile(r"/resources/github(?:\?|$)"), lambda r: r.fulfill(json=_INFO))

    page.add_init_script(f"localStorage.setItem('omnigent:ui-font-size', '{font_size}')")
    page.set_viewport_size({"width": viewport_width, "height": 844 if is_mobile else 900})
    page.goto(f"{base_url}/c/{session_id}")

    # ── Step 2: the gray bar, with every indicator and no task badges ──
    bar = page.get_by_test_id("composer-workspace-controls")
    workspace_dir = page.get_by_test_id("composer-workspace-dir")
    branch = page.get_by_test_id("composer-git-branch")
    pr_link = page.get_by_test_id("composer-pr-link")
    ring = page.get_by_test_id("composer-context-ring")

    expect(bar).to_be_visible(timeout=30_000)
    expect(workspace_dir).to_have_text("demo-app", timeout=30_000)
    expect(branch).to_have_text(_BRANCH)
    expect(pr_link).to_be_visible(timeout=30_000)
    expect(pr_link).to_have_accessible_name(f"#{_PR_NUMBER}")
    expect(ring).to_be_visible(timeout=30_000)
    expect(ring).to_have_attribute("aria-label", "42% of context used")
    # The report's precondition: task badges can mask the vertical offset,
    # so this journey runs with neither badge present.
    expect(page.get_by_test_id("background-task-pill")).to_have_count(0)
    expect(page.get_by_test_id("subagent-task-pill")).to_have_count(0)

    page.screenshot(path=tmp_path / "composer-metadata-bar.png", animations="disabled")

    # ── Step 3: one vertical centerline across the whole bar ──
    dir_center = _center_y(_box(workspace_dir))
    branch_center = _center_y(_box(branch))
    pr_center = _center_y(_box(pr_link))
    ring_center = _center_y(_box(ring))
    centers = {
        "workspace-dir": dir_center,
        "git-branch": branch_center,
        "pr-link": pr_center,
        "context-ring": ring_center,
    }
    print(f"Composer metadata centers ({viewport_width}px, {font_size}px): {centers}")

    violations: list[str] = []
    if abs(pr_center - dir_center) > _CENTERLINE_TOLERANCE_PX:
        violations.append(
            f"GitHub PR indicator is vertically misaligned: its center sits "
            f"{dir_center - pr_center:+.2f}px above the workspace control's "
            f"({centers})"
        )
    if abs(ring_center - dir_center) > _CENTERLINE_TOLERANCE_PX:
        violations.append(
            f"Context ring is vertically misaligned: its center sits "
            f"{dir_center - ring_center:+.2f}px above the workspace control's "
            f"({centers})"
        )
    if abs(pr_center - branch_center) > _CENTERLINE_TOLERANCE_PX:
        violations.append(
            f"GitHub PR indicator is vertically misaligned with the branch control ({centers})"
        )

    # ── Step 4: visible gaps group each icon with its own label ──
    gh_icon = _drawn_rect(pr_link.locator("svg"))
    pr_label = _box(pr_link.locator("span"))
    ring_icon = _drawn_rect(ring.locator("svg"))
    ring_label = _box(ring.locator("span[aria-hidden]"))

    gh_pair_gap = pr_label["left"] - gh_icon["right"]
    ring_pair_gap = ring_label["left"] - ring_icon["right"]
    between_pairs_gap = ring_icon["left"] - pr_label["right"]
    gaps = {
        "github-icon->pr-label": gh_pair_gap,
        "ring->percentage": ring_pair_gap,
        "pr-label->ring (unrelated)": between_pairs_gap,
    }
    print(f"Composer metadata visible gaps ({viewport_width}px, {font_size}px): {gaps}")

    if ring_pair_gap > between_pairs_gap + _GROUPING_TOLERANCE_PX:
        violations.append(
            f"Context wheel is misgrouped: it sits {ring_pair_gap:.2f}px from "
            f"its own percentage but only {between_pairs_gap:.2f}px from the "
            f"unrelated PR count ({gaps})"
        )
    if abs(gh_pair_gap - ring_pair_gap) > _PAIR_GAP_TOLERANCE_PX:
        violations.append(
            f"Visible icon-to-label gaps are unequal across the metadata "
            f"pairs: GitHub pair {gh_pair_gap:.2f}px vs context pair "
            f"{ring_pair_gap:.2f}px ({gaps})"
        )

    # All measurements are reported together so a partial fix surfaces every
    # remaining violation in one run.
    assert not violations, "composer metadata layout violations:\n- " + "\n- ".join(violations)
