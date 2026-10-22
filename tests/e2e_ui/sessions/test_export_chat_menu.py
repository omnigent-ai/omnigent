"""E2E: the session kebab menu offers Export and it saves the transcript.

The chat-header session actions menu is the only in-app place to act on a
conversation; without an Export item a conversation cannot be saved as a
local file from the UI (the CLI ``omnigent session export`` is the only
path). The first test pins the Export affordance in the kebab menu; the
second drives the full journey — click Export, receive a file download
whose content preserves the transcript: both turns in order, the code
block, and the link.

Content markers are single-line and quote-free so they survive any export
format unchanged (a JSONL export JSON-escapes newlines and quotes).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_items

_PROMPT_ONE = "Export repro first turn: write a hello constant"
_CODE_LINE = "hello_export_repro = 4646"
_REPLY_ONE = f"Here it is:\n\n```python\n{_CODE_LINE}\n```\n"
_PROMPT_TWO = "Export repro second turn: link me the export docs"
_LINK_URL = "https://example.com/session-export-docs"
_REPLY_TWO = f"They live at [the export docs]({_LINK_URL})."
_MARKERS_IN_ORDER = (_PROMPT_ONE, _CODE_LINE, _PROMPT_TWO, _LINK_URL)


@pytest.fixture
def transcript_session(seeded_session: tuple[str, str]) -> tuple[str, str]:
    """Seed a settled two-turn transcript (code block + link) to export."""
    from omnigent.entities import MessageData, NewConversationItem

    base_url, session_id = seeded_session
    seed_committed_items(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_export_turn_1",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": _PROMPT_ONE}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_export_turn_1",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": _REPLY_ONE}],
                    agent="hello_world",
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_export_turn_2",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": _PROMPT_TWO}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_export_turn_2",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": _REPLY_TWO}],
                    agent="hello_world",
                ),
            ),
        ],
    )
    return base_url, session_id


def _open_session_menu(page: Page, base_url: str, session_id: str) -> None:
    page.goto(f"{base_url}/c/{session_id}")
    trigger = page.get_by_test_id("header-conversation-actions")
    expect(trigger).to_be_visible(timeout=30_000)
    trigger.click()
    expect(page.get_by_role("menu")).to_be_visible()


def test_header_session_menu_offers_export(
    page: Page,
    transcript_session: tuple[str, str],
) -> None:
    """The session kebab menu contains an Export action."""
    base_url, session_id = transcript_session
    _open_session_menu(page, base_url, session_id)
    expect(page.get_by_role("menuitem", name="Export")).to_be_visible()


def test_header_export_downloads_transcript(
    page: Page,
    transcript_session: tuple[str, str],
) -> None:
    """Export saves a local file preserving turn order, code, and links."""
    base_url, session_id = transcript_session
    _open_session_menu(page, base_url, session_id)

    export_item = page.get_by_role("menuitem", name="Export")
    expect(export_item).to_be_visible()
    with page.expect_download() as download_info:
        export_item.click()
    download = download_info.value
    assert download.suggested_filename

    exported = Path(str(download.path())).read_text(encoding="utf-8")
    positions = [exported.find(marker) for marker in _MARKERS_IN_ORDER]
    missing = [m for m, pos in zip(_MARKERS_IN_ORDER, positions, strict=True) if pos == -1]
    assert not missing, f"export is missing transcript content: {missing}"
    assert positions == sorted(positions), f"export lost turn order: {positions}"
