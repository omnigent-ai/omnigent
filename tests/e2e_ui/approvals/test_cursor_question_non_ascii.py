"""E2E: a cursor-native AskQuestion card shows non-ASCII text as cursor wrote it.

Like ``test_cursor_multiselect_question.py``, this runs the real detection and
mirror path minus the Cursor TUI (CI has no Cursor login): a seeded cursor-shaped
``store.db`` goes through ``read_cursor_pending_tool_calls`` and ``_run_one_question``
against the live server, and the browser must show the original characters.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

from omnigent.harnesses.cursor_native.permissions import (
    _run_one_question,
    cursor_tool_call_elicitation_id,
    read_cursor_pending_tool_calls,
)

_APPROVAL_CARD = '[data-testid="approval-card"]'
_FORM = '[data-testid="ask-user-question-form"]'
_SUBMIT = '[data-testid="ask-user-question-submit"]'

_MOCK_ELICITATION_TIMEOUT_MS = 15_000

_PROMPT = "Which café style — “classic” or ‘modern’?"
_OPTION_A = "Café — “classic”"
_OPTION_B = "Crème brûlée"


def _seed_pending_question_store(store: Path) -> None:
    """Write a cursor-shaped ``store.db`` with one pending ``AskQuestion`` call.

    The JSON sits in binary checkpoint noise, serialized like cursor-agent does:
    non-ASCII as raw UTF-8, not ``\\uXXXX`` escapes.
    """
    message = {
        "id": "1",
        "role": "assistant",
        "content": [
            {
                "type": "tool-call",
                "toolCallId": "call_utf8\nfc_1",
                "toolName": "AskQuestion",
                "args": {
                    "title": "Pick a style",
                    "questions": [
                        {
                            "id": "style",
                            "prompt": _PROMPT,
                            "options": [
                                {"id": "a", "label": _OPTION_A},
                                {"id": "b", "label": _OPTION_B},
                            ],
                        }
                    ],
                },
            }
        ],
        "providerOptions": {"cursor": {"pendingToolCallStartedAtMs": 1782373529662}},
    }
    raw = (
        b"\n \x16\xa0 noise {"
        + json.dumps(message, ensure_ascii=False).encode("utf-8")
        + b"*\x8e\x02\xff"
    )
    con = sqlite3.connect(str(store))
    try:
        con.execute("CREATE TABLE blobs (id TEXT, data BLOB)")
        con.execute("INSERT INTO blobs (id, data) VALUES (?, ?)", ("blob0", raw))
        con.commit()
    finally:
        con.close()


@pytest.mark.timeout(120)
def test_cursor_question_card_keeps_non_ascii_text(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """The web question card renders the same characters cursor stored."""
    base_url, session_id = seeded_session

    store = tmp_path / "store.db"
    _seed_pending_question_store(store)
    calls = read_cursor_pending_tool_calls(store)
    assert len(calls) == 1 and calls[0].tool_name == "AskQuestion"
    call = calls[0]
    elicitation_id = cursor_tool_call_elicitation_id(session_id, call.tool_call_id)
    mirror_result: dict[str, object] = {}

    async def _mirror() -> None:
        async with httpx.AsyncClient(base_url=base_url, timeout=120.0) as client:
            await _run_one_question(
                client,
                session_id=session_id,
                bridge_dir=tmp_path,
                call=call,
                elicitation_id=elicitation_id,
            )

    def _run_mirror() -> None:
        try:
            asyncio.run(_mirror())
        except Exception as exc:  # pragma: no cover - surfaced via assertion below
            mirror_result["error"] = exc

    thread = threading.Thread(target=_run_mirror, daemon=True)
    thread.start()
    # Browser setup happens only now so a recording opens on the user's first view.
    page = request.getfixturevalue("page")
    try:
        page.goto(f"{base_url}/c/{session_id}")

        card = (
            page.locator(f'{_APPROVAL_CARD}[data-state="pending"]')
            .filter(has=page.locator(_FORM))
            .first
        )
        expect(card).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
        form = card.locator(_FORM)
        expect(form).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
        card.scroll_into_view_if_needed()
        page.wait_for_timeout(3000)  # hold on the rendered form (recording)

        rendered = form.inner_text()
        assert _PROMPT in rendered, f"question card shows {rendered!r}"
        option_a = form.get_by_role("radio", name=_OPTION_A)
        option_b = form.get_by_role("radio", name=_OPTION_B)
        expect(option_a).to_be_visible(timeout=5_000)
        expect(option_b).to_be_visible()

        option_a.check()
        form.locator(_SUBMIT).click()
        responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
        expect(responded).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    finally:
        thread.join(timeout=10)
        fallback_note = ""
        if thread.is_alive():
            # No verdict reached the mirror (an assertion above failed first):
            # decline the parked elicitation so the thread can exit.
            try:
                httpx.post(
                    f"{base_url}/v1/sessions/{session_id}/events",
                    json={
                        "type": "approval",
                        "data": {"elicitation_id": elicitation_id, "action": "decline"},
                    },
                    timeout=10.0,
                ).raise_for_status()
            except httpx.HTTPError as exc:
                fallback_note = f" (decline fallback failed: {exc!r})"
            thread.join(timeout=30)
        if "error" in mirror_result:
            # Raised here so an early mirror failure is reported as the cause rather
            # than hidden behind the card-visibility timeout it provokes.
            raise AssertionError(
                f"cursor question mirror failed: {mirror_result['error']}"
            ) from mirror_result["error"]  # type: ignore[misc]

    assert not thread.is_alive(), (
        f"cursor question mirror never received a web verdict{fallback_note}"
    )
