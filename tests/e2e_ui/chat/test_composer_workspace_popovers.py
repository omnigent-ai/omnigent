"""Composer workspace labels scale and their details wrap without clipping."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry, seed_committed_turn

_WORKSPACE = "/workspace/projects/" + "long-unbroken-directory-name-" * 4 + "/checkout"
_BRANCH = "feature/" + "long-branch-name-" * 5


@pytest.mark.parametrize("viewport_width", [1440, 390], ids=["desktop", "mobile"])
@pytest.mark.parametrize("font_size", [13, 18], ids=["default-font", "large-font"])
@pytest.mark.parametrize("has_binding", [True, False], ids=["long-label", "fallback"])
def test_composer_details_wrap_without_clipping(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    viewport_width: int,
    font_size: int,
    has_binding: bool,
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
            host_id="composer-workspace-host",
        )
        route.fulfill(response=response, json=snapshot)

    page.route(re.compile(rf"/v1/sessions/{session_id}(?:\?.*)?$"), session_details)
    page.route(
        f"**/v1/sessions/{session_id}/resources/github",
        lambda route: route.fulfill(
            json={
                "object": "session.github.info",
                "available": has_binding,
                "repo": {"name_with_owner": "example/repo"} if has_binding else None,
                "prs": [],
            }
        ),
    )
    page.route(
        "**/v1/hosts/composer-workspace-host/worktrees?*",
        lambda route: route.fulfill(
            json={
                "data": [
                    {
                        "path": _WORKSPACE,
                        "branch": _BRANCH,
                        "is_main": True,
                        "detached": False,
                        "remote_provider": "github",
                    }
                ]
            }
        ),
    )
    page.add_init_script(f"localStorage.setItem('omnigent:ui-font-size', '{font_size}')")
    page.set_viewport_size({"width": viewport_width, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    controls = page.get_by_test_id("composer-workspace-controls")
    expect(controls).to_be_visible(timeout=30_000)
    expect(controls.get_by_role("button")).to_have_count(0)
    workspace = controls.get_by_test_id("composer-workspace-dir")
    expect(workspace).to_have_attribute(
        "title", _WORKSPACE if has_binding else "No workspace bound"
    )
    if has_binding:
        worktree = controls.get_by_test_id("composer-git-branch")
        expect(worktree).to_have_attribute("title", _BRANCH)
    else:
        expect(controls.get_by_test_id("composer-git-branch")).to_have_count(0)
    controls.screenshot(path=tmp_path / f"details-{viewport_width}.png", animations="disabled")


@pytest.mark.parametrize(
    "viewport_width", [1440, 3200, 390], ids=["desktop", "ultrawide", "mobile"]
)
@pytest.mark.parametrize("font_size", [13, 18], ids=["default-font", "large-font"])
@pytest.mark.parametrize("long_labels", [False, True], ids=["readable-name", "long-labels"])
def test_composer_workspace_labels_use_available_width(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    viewport_width: int,
    font_size: int,
    long_labels: bool,
) -> None:
    """Names fit wide bars; when they can't, the bar collapses to icons, not ellipses."""
    base_url, session_id = seeded_session
    seed_committed_turn(session_id, prompt="Hello", reply="Inspect the workspace labels.")
    name = "new-composer-width" * (8 if long_labels else 1)

    def session_details(route: Route) -> None:
        response = fetch_with_retry(route)
        snapshot = response.json()
        snapshot.update(
            workspace=f"/workspace/{name}",
            git_branch="creation-branch",
            host_id="composer-workspace-host",
        )
        route.fulfill(response=response, json=snapshot)

    page.route(re.compile(rf"/v1/sessions/{session_id}(?:\?.*)?$"), session_details)
    page.route(
        f"**/v1/sessions/{session_id}/resources/github",
        lambda route: route.fulfill(
            json={
                "object": "session.github.info",
                "available": True,
                "repo": {"name_with_owner": "example/repo"},
                "prs": [],
            }
        ),
    )
    page.route(
        "**/v1/hosts/composer-workspace-host/worktrees?*",
        lambda route: route.fulfill(
            json={
                "data": [
                    {
                        "path": f"/workspace/{name}",
                        "branch": name,
                        "is_main": True,
                        "detached": False,
                        "remote_provider": "github",
                    }
                ]
            }
        ),
    )
    page.add_init_script(f"localStorage.setItem('omnigent:ui-font-size', '{font_size}')")
    page.set_viewport_size({"width": viewport_width, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    controls = page.get_by_test_id("composer-workspace-controls")
    expect(controls).to_be_visible(timeout=30_000)
    expect(controls.get_by_role("button")).to_have_count(0)
    controls.screenshot(path=tmp_path / f"labels-{viewport_width}-{font_size}.png")

    # The bar shows the full names while they fit; once a name would have to
    # truncate (long labels, or the narrow mobile bar) every chip drops to its
    # icon instead of showing clipped text.
    collapsed = long_labels or viewport_width == 390
    if collapsed:
        expect(controls).to_have_attribute("data-labels", "collapsed")
        for label in controls.locator("span.truncate").all():
            expect(label).to_be_hidden()
    else:
        expect(controls).not_to_have_attribute("data-labels", "collapsed")
        expect(controls.locator("span.truncate")).to_have_text([name, name])

    dimensions = controls.evaluate(
        """bar => {
          const bounds = bar.getBoundingClientRect();
          return {
            left: bounds.left,
            right: bounds.right,
            bottom: bounds.bottom,
            viewport: window.innerWidth,
            items: [...bar.querySelectorAll(
              '[data-testid="composer-workspace-dir"], [data-testid="composer-git-branch"]',
            )].map(item => {
              const itemBounds = item.getBoundingClientRect();
              const label = item.querySelector('span.truncate');
              return {
                left: itemBounds.left,
                right: itemBounds.right,
                top: itemBounds.top,
                bottom: itemBounds.bottom,
                labelWidth: label.clientWidth,
                textWidth: label.scrollWidth,
                icons: [...item.querySelectorAll('svg')].map(icon => {
                  const iconBounds = icon.getBoundingClientRect();
                  return { left: iconBounds.left, right: iconBounds.right };
                }),
              };
            }),
          };
        }"""
    )
    assert dimensions["left"] >= 0
    assert dimensions["right"] <= dimensions["viewport"]
    workspace, worktree = dimensions["items"]
    assert workspace["right"] < worktree["left"]
    assert workspace["top"] == pytest.approx(worktree["top"], abs=1)
    for item in dimensions["items"]:
        assert item["left"] >= dimensions["left"]
        assert item["right"] <= dimensions["right"]
        assert item["bottom"] <= dimensions["bottom"]
        if collapsed:
            # Collapsed: the label is hidden, so only the icon remains.
            assert item["labelWidth"] == 0
        else:
            assert item["labelWidth"] > 0
            assert item["textWidth"] <= item["labelWidth"] + 1
        for icon in item["icons"]:
            assert icon["right"] - icon["left"] >= 12
            assert icon["left"] >= item["left"]
            assert icon["right"] <= item["right"]
