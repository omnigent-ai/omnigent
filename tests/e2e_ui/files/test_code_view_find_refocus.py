"""E2E: Cmd+F in the code view refocuses the find field when it is already open.

FileViewer claims Cmd+F at the window level for Monaco surfaces and opens the
find widget through its ``searchOpen`` toggle. The first press must open and
focus the widget; a second press with the widget still open and focus back in
the editor must return focus to the find field so typing updates the query
rather than editing the file.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail

# The hello_world runner writes seeded files into the server's cwd, the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]

_FILE_PATH = "search_target.py"
_FILE_CONTENT = """\
def greet(name):
    return "hello " + name


def farewell(name):
    return "goodbye " + name
"""

# isMacPlatform() reads userAgentData.platform with navigator.platform as the
# fallback, so both are pinned (mirrors test_macos_ctrl_f_stays_in_composer.py).
_MACOS_INIT_SCRIPT = """
Object.defineProperty(navigator, "platform", { value: "MacIntel" });
Object.defineProperty(navigator, "userAgentData", { value: { platform: "macOS" } });
Object.defineProperty(navigator, "userAgent", { value: "Mozilla/5.0 (Macintosh)" });
"""


@pytest.fixture
def seeded_code_file(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_FILE_PATH}",
        json={"content": _FILE_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def _open_code_file(page: Page, base_url: str, session_id: str) -> Locator:
    """Open the seeded file from the Files panel; return the visible file viewer."""
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    file_button = page.get_by_role("button", name=re.compile(re.escape(_FILE_PATH))).filter(
        has_text=_FILE_PATH
    )
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(viewer).to_be_visible()
    expect(viewer.locator(".monaco-editor")).to_be_visible(timeout=30_000)
    expect(viewer.locator(".view-lines")).to_contain_text("greet", timeout=10_000)
    return viewer


def test_cmd_f_refocuses_open_find_widget_on_macos(
    request: pytest.FixtureRequest, seeded_code_file: tuple[str, str]
) -> None:
    base_url, session_id = seeded_code_file
    page: Page = request.getfixturevalue("page")
    page.add_init_script(_MACOS_INIT_SCRIPT)
    viewer = _open_code_file(page, base_url, session_id)
    code = viewer.locator(".view-lines")
    editor_input = viewer.get_by_role("textbox", name="Editor content")
    find_input = viewer.locator(".find-widget").get_by_role("textbox", name="Find", exact=True)

    code.click()
    expect(editor_input).to_be_focused()
    page.keyboard.press("Meta+f")
    expect(find_input).to_be_visible()
    expect(find_input).to_be_focused()
    page.keyboard.type("greet")
    expect(find_input).to_have_value("greet")

    code.click()
    expect(editor_input).to_be_focused()
    expect(viewer.locator(".find-widget.visible")).to_be_visible()

    page.keyboard.press("Meta+f")
    expect(find_input).to_be_focused()
    page.keyboard.type("zq")
    expect(find_input).to_have_value(re.compile(r"zq$"))
    expect(code).not_to_contain_text("zq")
