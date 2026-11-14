"""Android shell: a file opened in the Workspace rail must not re-add the OS inset.

Plain Chromium stands in for the WebView, running the real injected bridge
script with the OS insets written the way ``MainActivity.emitInsets()`` does.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import FloatRect, Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail
from tests.e2e_ui.mobile._android_bridge import android_bridge_script, emit_android_insets

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Unfolded foldable / tablet class (>= the 768px ``md`` breakpoint, where the
# Workspace rail docks) and a phone, where the viewer is a standalone overlay.
_UNFOLDED_VIEWPORT = {"width": 1024, "height": 768}
_PHONE_VIEWPORT = {"width": 390, "height": 844}
_STATUS_BAR_PX = 52
_NAV_BAR_PX = 20

_SEEDED_FILE = "android_workspace_viewer_inset.txt"
_SEEDED_CONTENT = "Body of the file opened in the Workspace rail."


@pytest.fixture
def session_with_workspace_file(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed one text file into the session workspace so the Files tab has a row to open.

    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    :returns: The same ``(base_url, session_id)`` pair.
    """
    base_url, session_id = seeded_session
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_SEEDED_FILE}",
        json={"content": f"{_SEEDED_CONTENT}\n", "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield seeded_session
    finally:
        # With ``os_env.cwd: .`` a spawned runner writes under <repo-root>/<session_id>/.
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def _open_android_session(request: pytest.FixtureRequest, base_url: str, session_id: str) -> Page:
    """Create the recorded page after setup and open the session under the Android shell.

    :param request: Pytest request used to create the ``page`` fixture lazily.
    :param base_url: Base URL of the e2e server.
    :param session_id: Session to open.
    :returns: The page, with the shell marker live and the OS insets applied.
    """
    page: Page = request.getfixturevalue("page")
    page.add_init_script(android_bridge_script())
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator(".app-shell")).to_have_attribute("data-android-native", "true")
    expect(page.locator("style#omnigent-android-insets")).to_have_count(1)
    emit_android_insets(page, _STATUS_BAR_PX, _NAV_BAR_PX)
    return page


def _box(locator: Locator) -> FloatRect:
    box = locator.bounding_box()
    assert box is not None, f"element {locator} has no bounding box"
    return box


@pytest.mark.browser_context_args(viewport=_UNFOLDED_VIEWPORT)
def test_rail_file_viewer_keeps_single_status_bar_inset(
    request: pytest.FixtureRequest,
    session_with_workspace_file: tuple[str, str],
) -> None:
    """A file opened in the Workspace rail starts right under the tab strip.

    :param request: Pytest request (the page is created after the file is seeded).
    :param session_with_workspace_file: ``(base_url, session_id)`` with one seeded file.
    """
    base_url, session_id = session_with_workspace_file
    page = _open_android_session(request, base_url, session_id)

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    # The rail owns the status-bar clearance; a nested panel has nothing left to clear.
    expect(rail).to_have_css("padding-top", f"{_STATUS_BAR_PX}px")

    rail.get_by_role("tab", name=re.compile("^Files")).click()
    row = rail.get_by_role("button", name=re.compile(re.escape(_SEEDED_FILE))).filter(
        has_text=_SEEDED_FILE
    )
    expect(row).to_be_visible(timeout=30_000)
    row.click()

    viewer = rail.get_by_test_id("file-viewer")
    expect(viewer).to_be_visible()
    expect(viewer.get_by_text(_SEEDED_CONTENT).first).to_be_visible(timeout=20_000)
    header = viewer.locator(":scope > div").first
    expect(header).to_contain_text(_SEEDED_FILE)
    expect(viewer).to_have_css("padding-top", "0px")
    expect(viewer).to_have_css("padding-bottom", "0px")

    strip_box = _box(rail.locator(".workspace-tab-strip"))
    header_box = _box(header)
    gap = header_box["y"] - (strip_box["y"] + strip_box["height"])
    assert abs(gap) <= 1.0, f"viewer header sits {gap:.0f}px below the Workspace tab strip"


@pytest.mark.browser_context_args(viewport=_PHONE_VIEWPORT)
def test_standalone_file_viewer_keeps_one_inset_on_a_phone(
    request: pytest.FixtureRequest,
    session_with_workspace_file: tuple[str, str],
) -> None:
    """Below ``md`` the full-screen viewer is not nested in the rail and keeps one inset each.

    :param request: Pytest request (the page is created after the file is seeded).
    :param session_with_workspace_file: ``(base_url, session_id)`` with one seeded file.
    """
    base_url, session_id = session_with_workspace_file
    page = _open_android_session(request, base_url, session_id)

    page.get_by_role("button", name="Conversation actions").click()
    page.get_by_role("menuitem", name="Files", exact=True).click()
    expect(page.get_by_test_id("files-panel-drawer")).to_have_attribute("data-state", "open")
    row = page.get_by_role("button", name=re.compile(rf"^{re.escape(_SEEDED_FILE)}"))
    expect(row).to_be_visible(timeout=30_000)
    row.click()

    viewer = page.locator('aside[data-testid="file-viewer"]')
    expect(viewer).to_be_visible()
    expect(viewer.get_by_text(_SEEDED_CONTENT).first).to_be_visible(timeout=20_000)
    expect(viewer).to_have_css("padding-top", f"{_STATUS_BAR_PX}px")
    expect(viewer).to_have_css("padding-bottom", f"{_NAV_BAR_PX}px")
