"""E2E: Excalidraw scene files render in the FileViewer.

A seeded ``.excalidraw`` file should open on the read-only interactive canvas,
not as raw JSON. The file viewer must also preserve the source/preview toggle.
Seeded via the filesystem PUT endpoint (no agent run).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

_FILE_PATH = "architecture.excalidraw"
_SCENE_CONTENT = json.dumps(
    {
        "type": "excalidraw",
        "version": 2,
        "source": "https://excalidraw.com",
        "elements": [],
        "appState": {"viewBackgroundColor": "#ffffff"},
        "files": {},
    }
)


@pytest.fixture
def seeded_excalidraw_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed an Excalidraw scene and yield ``(base_url, session_id, path)``."""
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_FILE_PATH}"
    )
    response = httpx.put(
        file_url,
        json={"content": _SCENE_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    response.raise_for_status()
    yield (base_url, session_id, _FILE_PATH)


def test_excalidraw_renders_preview_and_source_toggle(
    page: Page,
    seeded_excalidraw_session: tuple[str, str, str],
) -> None:
    """The scene renders on a canvas and can switch to JSON source and back."""
    base_url, session_id, file_path = seeded_excalidraw_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")

    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(file_path)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    preview = file_viewer.get_by_test_id("excalidraw-viewer")
    expect(preview).to_be_visible(timeout=15_000)
    expect(preview.locator("canvas").first).to_be_visible(timeout=15_000)
    expect(file_viewer.get_by_text("Unable to render diagram", exact=False)).to_have_count(0)

    file_viewer.get_by_role("button", name="View source").click()
    expect(file_viewer.get_by_text('"type": "excalidraw"', exact=False).first).to_be_visible(
        timeout=10_000
    )
    expect(file_viewer.get_by_test_id("excalidraw-viewer")).to_have_count(0)

    file_viewer.get_by_role("button", name="View preview").click()
    expect(file_viewer.get_by_test_id("excalidraw-viewer")).to_be_visible(timeout=15_000)
