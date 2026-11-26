"""Shared mechanics for the tabs sidebar's Files page journeys.

Each journey keeps its own files, faults and assertions; this module only seeds a
session's workspace, opens the sidebar with ``workspace_sidebar_v2`` on, walks the tree
and runs a ⌘K row.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import httpx
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui._quick_pick_palette import enable_quick_pick_palette
from tests.e2e_ui.conftest import open_right_rail
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import _set_flag

_DESKTOP = {"width": 1440, "height": 900}


def session_workspace(base_url: str, session_id: str) -> Path:
    """The session's workspace on its host, where the filesystem PUTs write."""
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/shell",
        json={"command": "pwd", "timeout": 30},
        timeout=60.0,
    )
    response.raise_for_status()
    return Path(response.json()["stdout"].strip().splitlines()[-1])


@contextmanager
def seeded_workspace(
    session: tuple[str, str], files: Mapping[str, str]
) -> Iterator[tuple[str, str]]:
    """Write ``files`` (path to text) into the session's workspace, and remove them after."""
    base_url, session_id = session
    workspace = session_workspace(base_url, session_id)
    # Only what was written is removed, so a write failing partway leaves the files after it.
    written: list[str] = []
    try:
        for path, content in files.items():
            httpx.put(
                f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}",
                json={"content": content, "encoding": "utf-8"},
                timeout=10.0,
            ).raise_for_status()
            written.append(path)
        yield session
    finally:
        remove_seeded(workspace, written)


def remove_seeded(workspace: Path, paths: Iterable[str]) -> None:
    """Remove the seeded ``paths``, and the folders they leave empty, but nothing else.

    The workspace may be a directory the runner was handed, holding files of its own.
    """
    for path in paths:
        target = workspace / path
        target.unlink(missing_ok=True)
        folder = target.parent
        while folder != workspace and folder.is_relative_to(workspace):
            try:
                folder.rmdir()
            except OSError:
                break  # Not empty: something else lives here.
            folder = folder.parent


def is_mac(page: Page) -> bool:
    return bool(page.evaluate("() => /Mac|iPhone|iPad/.test(navigator.platform)"))


def open_workspace(
    page: Page, base_url: str, session_id: str, *, quick_pick: bool = False
) -> Locator:
    """Open the session with ``workspace_sidebar_v2`` on; returns its Workspace sidebar.

    ``quick_pick`` also turns on the QuickPick ⌘K palette, for journeys that run its settings.
    """
    if quick_pick:
        enable_quick_pick_palette(page, "workspace_sidebar_v2")
    else:
        _set_flag(page, True)
    page.set_viewport_size(_DESKTOP)
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    sidebar = page.get_by_role("complementary", name="Workspace")
    expect(sidebar.get_by_role("group", name="Workspace history")).to_be_visible(timeout=60_000)
    return sidebar


def open_from_tree(tree: Locator, path: str) -> None:
    """Open the folders above ``path`` in a Files tree, then the file itself."""
    parts = path.split("/")
    for depth in range(1, len(parts)):
        folder_path = "/".join(parts[:depth]) + "/"
        folder = tree.locator(f'[role="treeitem"][data-item-path="{folder_path}"]')
        expect(folder).to_be_visible(timeout=30_000)
        if folder.get_attribute("aria-expanded") != "true":
            folder.click()
    row = tree.locator(f'[role="treeitem"][data-item-path="{path}"]')
    expect(row).to_be_visible(timeout=30_000)
    row.click()


def run_palette(page: Page, label: str) -> None:
    """Run the one ⌘K row starting with ``label``, and wait for the palette to close."""
    page.keyboard.press("ControlOrMeta+k")
    palette = page.get_by_test_id("command-palette-input")
    expect(palette).to_be_focused(timeout=10_000)
    palette.fill(label)
    option = page.get_by_role("option", name=re.compile(rf"^{re.escape(label)}"))
    expect(option).to_have_count(1)
    option.click()
    expect(palette).to_have_count(0)


def set_rail_width(page: Page, rail: Locator, width: int) -> None:
    """Drag the rail's resize handle until the rail is ``width`` pixels wide."""
    handle = page.get_by_role("separator", name="Resize panel").first
    handle_box, rail_box = handle.bounding_box(), rail.bounding_box()
    assert handle_box is not None and rail_box is not None
    y = handle_box["y"] + 200
    page.mouse.move(handle_box["x"] + 2, y)
    page.mouse.down()
    page.mouse.move(rail_box["x"] + rail_box["width"] - width, y, steps=8)
    page.mouse.up()
    for _ in range(20):
        box = rail.bounding_box()
        if box is not None and abs(box["width"] - width) <= 2:
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"the rail didn't settle at {width}px")
