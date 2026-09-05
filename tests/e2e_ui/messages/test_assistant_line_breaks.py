"""E2E: meaningful single newlines in assistant output remain visible."""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

_AGENT_NAME = "hello_world"
_LINES = (
    "Current task: complete",
    "Next task: review the draft",
    "Final task: publish after approval",
)


def test_assistant_single_newlines_render_as_line_breaks(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A persisted three-line assistant message renders as three visual lines."""
    base_url, session_id = seeded_session
    message = "\n".join(_LINES)
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": _AGENT_NAME, "text": message},
        },
        timeout=10.0,
    )
    response.raise_for_status()

    page.goto(f"{base_url}/c/{session_id}")

    section = page.get_by_test_id("assistant-text-section").filter(has_text=_LINES[0])
    expect(section).to_have_count(1, timeout=30_000)
    expect(section).to_have_text(message)
    expect(section.locator("br")).to_have_count(len(_LINES) - 1)

    line_tops = section.locator("p").evaluate(
        """paragraph => {
            const range = document.createRange();
            return Array.from(paragraph.childNodes)
              .filter(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim())
              .map(node => {
                range.selectNodeContents(node);
                return Math.round(range.getBoundingClientRect().top);
              });
        }"""
    )
    assert line_tops == sorted(set(line_tops))
    assert len(line_tops) == len(_LINES)
