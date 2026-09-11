"""Composer workspace and worktree details wrap without clipping text."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry, seed_committed_turn

_WORKSPACE = "/workspace/projects/" + "long-unbroken-directory-name-" * 4 + "/checkout"
_BRANCH = "feature/" + "long-branch-name-" * 5


@pytest.mark.parametrize("viewport_width", [1440, 390], ids=["desktop", "mobile"])
@pytest.mark.parametrize("has_binding", [True, False], ids=["long-label", "fallback"])
@pytest.mark.parametrize("popover", ["workspace", "worktree"])
def test_composer_details_wrap_without_clipping(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    viewport_width: int,
    has_binding: bool,
    popover: str,
) -> None:
    """Long values and fallback explanations fit both informational popovers."""
    base_url, session_id = seeded_session
    seed_committed_turn(session_id, prompt="Hello", reply="Inspect the session details.")

    def session_details(route: Route) -> None:
        response = fetch_with_retry(route)
        snapshot = response.json()
        snapshot.update(
            workspace=_WORKSPACE if has_binding else None,
            git_branch=_BRANCH if has_binding else None,
        )
        route.fulfill(response=response, json=snapshot)

    page.route(re.compile(rf"/v1/sessions/{session_id}(?:\?.*)?$"), session_details)
    page.set_viewport_size({"width": viewport_width, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    controls = page.get_by_test_id("composer-workspace-controls")
    expect(controls).to_be_visible(timeout=30_000)
    controls.get_by_role("button").nth(0 if popover == "workspace" else 1).click()

    menu = page.get_by_role("menu")
    expect(menu.get_by_text(f"Session {popover}", exact=True)).to_be_visible()
    if popover == "workspace":
        detail = _WORKSPACE if has_binding else "This session has no workspace binding."
        explanation = "Choose a different workspace when starting a new session."
    else:
        detail = (
            _BRANCH if has_binding else "The runner has not reported a branch for this session."
        )
        explanation = "The current session keeps its workspace and worktree."
    expect(menu.locator("p")).to_have_text([detail, explanation])

    dimensions = menu.evaluate(
        """menu => {
          const bounds = menu.getBoundingClientRect();
          return {
            left: bounds.left,
            right: bounds.right,
            top: bounds.top,
            bottom: bounds.bottom,
            viewport: window.innerWidth,
            lines: [...menu.querySelectorAll('p')].flatMap(paragraph => {
              const range = document.createRange();
              range.selectNodeContents(paragraph);
              return [...range.getClientRects()].map(line => ({
                left: line.left, right: line.right, top: line.top, bottom: line.bottom,
              }));
            }),
          };
        }"""
    )
    assert dimensions["left"] >= 0
    assert dimensions["right"] <= dimensions["viewport"]
    assert dimensions["lines"]
    for line in dimensions["lines"]:
        assert line["left"] >= dimensions["left"] - 1
        assert line["right"] <= dimensions["right"] + 1
        assert line["top"] >= dimensions["top"] - 1
        assert line["bottom"] <= dimensions["bottom"] + 1

    menu.screenshot(path=tmp_path / f"{popover}-{viewport_width}.png")
