"""Composer Undo (Cmd/Ctrl+Z) must survive an ``@``-mention insert.

A controlled-value rewrite drops the textarea's native undo history, which only a
real browser models. Needs a native coding-agent session and a workspace file.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

_REPORT_PATH = "report.md"
_REPORT_CONTENT = "# Report\nalpha bravo charlie\n"


def _seed_report(base_url: str, session_id: str) -> None:
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_REPORT_PATH}",
        json={"content": _REPORT_CONTENT, "encoding": "utf-8"},
        timeout=15.0,
    )
    resp.raise_for_status()


def _composer(page: Page):
    textarea = page.get_by_label("Message the agent")
    expect(textarea).to_be_visible(timeout=45_000)
    expect(textarea).to_be_enabled(timeout=45_000)
    return textarea


def test_composer_undo_reverts_typed_text(
    request: pytest.FixtureRequest,
    native_claude_session: tuple[str, str],
) -> None:
    """Control: plain typed composer text is undoable with Ctrl/Cmd+Z."""
    base_url, session_id = native_claude_session
    page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")

    textarea = _composer(page)
    textarea.click()
    page.keyboard.type("alpha bravo", delay=40)
    expect(textarea).to_have_value("alpha bravo")

    page.keyboard.press("ControlOrMeta+z")
    expect(textarea).not_to_have_value("alpha bravo")


def test_undo_still_works_after_mention_insert(
    request: pytest.FixtureRequest,
    native_claude_session: tuple[str, str],
) -> None:
    """Ctrl/Cmd+Z must still undo typed text after an ``@``-mention insert."""
    base_url, session_id = native_claude_session
    _seed_report(base_url, session_id)

    page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")

    textarea = _composer(page)
    textarea.click()
    page.keyboard.type("alpha bravo", delay=40)
    page.keyboard.type(" @rep", delay=60)

    first_item = page.locator('[data-testid="file-mention-item-0"]')
    expect(first_item).to_be_visible(timeout=10_000)
    expect(first_item).to_contain_text("report.md")

    page.keyboard.press("Tab")
    expect(textarea).to_have_value("alpha bravo ")

    # The attach must keep the native undo history, so the first Undo restores
    # the deleted ``@rep`` token instead of leaving the attached draft.
    page.keyboard.press("ControlOrMeta+z")
    expect(textarea).to_have_value("alpha bravo @rep")
