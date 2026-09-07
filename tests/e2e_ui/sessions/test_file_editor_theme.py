"""E2E: Monaco file surfaces follow Omnigent's theme, not the OS scheme."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PY_PATH = "theme_probe.py"


@pytest.fixture
def seeded_python(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    response = httpx.put(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{_PY_PATH}",
        json={"content": "answer = 42\n", "encoding": "utf-8"},
        timeout=10.0,
    )
    response.raise_for_status()
    try:
        yield base_url, session_id
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def test_file_editor_uses_explicit_omnigent_theme(
    page: Page, seeded_python: tuple[str, str]
) -> None:
    """A light Omnigent palette overrides a dark system for Monaco surfaces."""
    page.emulate_media(color_scheme="dark")
    page.add_init_script(
        """
        localStorage.setItem("web-theme", "light");
        localStorage.setItem("omnigent:ui-theme-palette", JSON.stringify("gruvbox"));
        """
    )

    base_url, session_id = seeded_python
    page.goto(f"{base_url}/c/{session_id}?file={_PY_PATH}")
    editor = page.locator('[data-testid="file-viewer"]:visible .monaco-editor')
    expect(editor).to_be_visible(timeout=30_000)

    assert not page.evaluate("document.documentElement.classList.contains('dark')")
    editor_background, app_card_background = editor.evaluate(
        """element => {
          const probe = document.createElement("div");
          probe.style.backgroundColor = "var(--card)";
          document.body.appendChild(probe);
          const result = [
            getComputedStyle(element).backgroundColor,
            getComputedStyle(probe).backgroundColor,
          ];
          probe.remove();
          return result;
        }"""
    )
    assert editor_background == app_card_background
