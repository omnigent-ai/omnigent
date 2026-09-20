"""E2E: assistant single newlines render as distinct lines in the chat view.

Assistant messages can carry meaningful single newlines (task status and
handoff output forwarded from CLI harnesses). The stored transcript preserves
them, but the chat view renders assistant markdown with default CommonMark
paragraph folding, collapsing each single newline into a space, so a
three-line status reads as one wrapped paragraph.

The test drives a real turn through the composer against the mock LLM and
asserts the rendered bubble keeps the reply's line structure.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

_PROMPT = "Report the current task status."
_STATUS_LINES = [
    "Current task: complete",
    "Next task: review the draft",
    "Final task: publish after approval",
]
_REPLY = "\n".join(_STATUS_LINES)


def test_assistant_single_newlines_render_as_line_breaks(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _REPLY}],
        key="newline-collapse-status",
        match=_PROMPT,
    )

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(_PROMPT)
    page.get_by_role("button", name="Send", exact=True).click()

    bubble = page.locator(_ASSISTANT).last
    expect(bubble).to_contain_text(_STATUS_LINES[-1], timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    # inner_text() keeps rendered hard breaks (<br>/pre-wrap newlines) but not
    # CSS soft-wrapping, so this fails only when the line structure is lost.
    section = bubble.get_by_test_id("assistant-text-section").last
    rendered_lines = [line.strip() for line in section.inner_text().splitlines() if line.strip()]
    assert rendered_lines == _STATUS_LINES
