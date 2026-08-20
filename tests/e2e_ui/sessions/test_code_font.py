"""E2E: the Settings → Appearance *code* font size drives Monaco and persists.

The code-font controls live on the Settings page (``pages/SettingsPage.tsx``,
``UiCodeFontSizeControl`` / ``UiCodeFontFamilyControl``) as two rows labelled
"Code font size" and "Code font family": a segmented pill (``−`` / value /
``+``) under a ``role="group"`` labelled "Code font size", plus a free-text
family input. Stepping the size writes the px choice to
``localStorage["omnigent:code-font-size"]``.

Unlike the chrome font (which rides the ``--ui-font-scale`` CSS variable), the
code editor (Monaco) and terminal (xterm) are fixed-pixel widgets: they read a
concrete ``fontSize`` at construction and are updated imperatively via the
in-module pub/sub in ``lib/codeFontPreferences.ts``. Monaco applies the size as
an inline ``font-size`` on its ``.view-lines`` element, so opening a non-markdown
file and reading that computed size is the portable signal that the preference
reached a real, mounted editor. The default is 13px; the range is 10–24px, so
the ``−`` / ``+`` buttons disable at the bounds.

No LLM turn is involved — the file is seeded via the filesystem PUT endpoint and
the size is a pure client-side preference.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

STORAGE_KEY = "omnigent:code-font-size"

# The seeded file lands in ``<cwd>/<session_id>/`` (the agent spec uses
# ``os_env.cwd: .``); mirror test_file_autosave.py's per-session cleanup.
_REPO_ROOT = Path(__file__).resolve().parents[2]

_PY_PATH = "code_font_module.py"
_PY_CONTENT = 'def greet(name):\n    return "hello " + name\n'


def _seed_file(base_url: str, session_id: str, path: str, content: str) -> None:
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}",
        json={"content": content, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()


@pytest.fixture
def seeded_python(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    _seed_file(base_url, session_id, _PY_PATH, _PY_CONTENT)
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def _stored_size(page: Page) -> str | None:
    """The persisted code-font size preference, or None when unset (default 13)."""
    return page.evaluate(f"() => window.localStorage.getItem('{STORAGE_KEY}')")


def _open_appearance(page: Page, base_url: str) -> None:
    """Navigate to the Settings Appearance section and wait for the code control."""
    page.goto(f"{base_url}/settings/appearance")
    expect(page.get_by_role("group", name="Code font size", exact=True)).to_be_visible(
        timeout=30_000
    )


def _open_monaco(page: Page, base_url: str, session_id: str) -> Locator:
    """Open the seeded Python file in Monaco and return the file-viewer locator."""
    page.goto(f"{base_url}/c/{session_id}?file={_PY_PATH}")
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible(timeout=30_000)
    # Non-markdown files render in Monaco (lazy-loaded); wait until it has laid
    # out the file so the inline font-size is present on .view-lines.
    expect(file_viewer.locator(".monaco-editor")).to_be_visible(timeout=20_000)
    expect(file_viewer.locator(".view-lines")).to_contain_text("greet", timeout=10_000)
    return file_viewer


def _monaco_font_size(file_viewer: Locator) -> str:
    """The computed ``font-size`` Monaco applied to its ``.view-lines`` element."""
    return file_viewer.locator(".view-lines").first.evaluate("el => getComputedStyle(el).fontSize")


def test_code_font_size_defaults_apply_to_monaco(
    page: Page, seeded_python: tuple[str, str]
) -> None:
    """A fresh context opens Monaco at the 13px code-font default, nothing stored."""
    base_url, session_id = seeded_python

    file_viewer = _open_monaco(page, base_url, session_id)

    # No stored preference → 13px default reaches the mounted editor.
    assert _stored_size(page) is None, "expected no persisted code-font size on a fresh load"
    assert _monaco_font_size(file_viewer) == "13px", "Monaco did not open at the code-font default"


def test_code_font_size_step_applies_to_monaco_and_persists(
    page: Page, seeded_python: tuple[str, str]
) -> None:
    """Stepping the size in Settings re-fonts a Monaco editor and survives reload.

    Steps the code-font size up in Settings, then opens the file: the mounted
    Monaco editor reads the new size at construction and renders its
    ``.view-lines`` at that px. A reload restores it (persisted + re-applied), so
    the editor never flashes back to the default.
    """
    base_url, session_id = seeded_python

    _open_appearance(page, base_url)
    value = page.get_by_test_id("code-font-size-input")
    increase = page.get_by_test_id("code-font-size-inc")

    # Fresh context → default 13px, nothing stored.
    expect(value).to_have_value("13")
    assert _stored_size(page) is None

    # → 15px: two steps up. The value and storage move together.
    increase.click()
    increase.click()
    expect(value).to_have_value("15")
    assert _stored_size(page) == "15"

    # The editor picks up the stepped size (read at construction).
    file_viewer = _open_monaco(page, base_url, session_id)
    assert _monaco_font_size(file_viewer) == "15px", "Monaco did not reflect the stepped size"

    # The choice survives a full reload.
    page.reload()
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer.locator(".view-lines")).to_contain_text("greet", timeout=30_000)
    assert _monaco_font_size(file_viewer) == "15px", "size was not restored after reload"


def test_code_font_size_steppers_clamp_at_bounds(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """The ``−`` / ``+`` buttons disable at the 10px min and 24px max."""
    base_url, _session_id = seeded_session

    # Seed the max before the app boots so the "+" button renders disabled.
    page.goto(base_url)
    page.evaluate(f"() => window.localStorage.setItem('{STORAGE_KEY}', '24')")
    _open_appearance(page, base_url)

    value = page.get_by_test_id("code-font-size-input")
    decrease = page.get_by_test_id("code-font-size-dec")
    increase = page.get_by_test_id("code-font-size-inc")

    # At the 24px max, only "+" is disabled.
    expect(value).to_have_value("24")
    expect(increase).to_be_disabled()
    expect(decrease).to_be_enabled()

    # Hold "−" down to the 10px min; there it flips to "−" disabled, "+" enabled.
    for _ in range(16):
        if decrease.is_disabled():
            break
        decrease.click()
    expect(value).to_have_value("10")
    expect(decrease).to_be_disabled()
    expect(increase).to_be_enabled()
