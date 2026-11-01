"""E2E: a cursor-native multi-select AskQuestion renders checkboxes in the web UI.

Guards against a multi-select question rendering as single-select in the web
UI. cursor-agent's ``AskQuestion`` tool marks a question multi-select with
``allowMultiple`` (protobuf field ``allow_multiple``, JSON name ``allowMultiple``
— verified against the cursor-agent 2026.08.31 bundle, whose TUI renders such a
question with a "(multi-select)" suffix). The runner-side mirror translates the
transcript-detected call into the web ``AskUserQuestion`` shape in
``omnigent.harnesses.cursor_native.permissions._askquestion_payload`` and parks it on the
``cursor-permission-request`` hook; the SPA renders ``AskUserQuestionForm`` from
that payload — checkboxes when ``multiSelect`` is true, radios otherwise.

The user-observable failure: the agent asks a multi-select question, but the
Omnigent chat card only allows selecting ONE option (radio semantics — picking a
second option deselects the first).

This test drives the REAL production path end-to-end minus the Cursor TUI
itself (CI has no Cursor login, so a live ``cursor-agent`` turn cannot run —
the same gap that makes ``test_cursor_native_approval.py`` skip): a seeded
cursor-shaped ``store.db`` goes through ``read_cursor_pending_tool_calls``, and a
background thread runs the actual runner mirror coroutine ``_run_one_question``
on the detected call, pointed at the live spawned server. The server parks the
elicitation, the SPA renders the form in a real browser, and the test asserts
the multi-select contract: the options render as checkboxes and BOTH selected
options stay selected. On the buggy build the options render as radio inputs
(the translation reads ``multiSelect``, a field cursor never sends, so the flag
is dropped), and the checkbox assertion fails — exactly this bug.

The question text is non-ASCII and stored as raw UTF-8, like cursor-agent writes
it, so the card must also show the exact characters rather than mojibake.

Sibling of ``test_opencode_question.py``, which covers the same multi-select
web-form contract for the OpenCode forwarder (whose ``multiple`` flag IS
translated correctly).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.cursor_native.permissions import (
    _run_one_question,
    cursor_tool_call_elicitation_id,
    read_cursor_pending_tool_calls,
)

_APPROVAL_CARD = '[data-testid="approval-card"]'
_FORM = '[data-testid="ask-user-question-form"]'
_SUBMIT = '[data-testid="ask-user-question-submit"]'

_MOCK_ELICITATION_TIMEOUT_MS = 15_000

# The exact text asserted in the form.
_PROMPT = "Which café features should I enable — “all” or ‘some’?"
_OPTION_A = "Café — “classic”"
_OPTION_B = "Crème brûlée"
_OPTION_C = "Feature C"

# The args shape cursor-agent (2026.08.31) persists for a multi-select
# AskQuestion call: question text is ``prompt`` (not ``question``), options
# carry ``id`` + ``label``, and the multi-select flag is ``allowMultiple``
# (proto ``allow_multiple``). The TUI renders this question with a
# "(multi-select)" suffix and lets the user Space-toggle several options.
_ASKQUESTION_ARGS: dict[str, object] = {
    "title": "Choose features",
    "questions": [
        {
            "id": "features",
            "prompt": _PROMPT,
            "allowMultiple": True,
            "options": [
                {"id": "a", "label": _OPTION_A},
                {"id": "b", "label": _OPTION_B},
                {"id": "c", "label": _OPTION_C},
            ],
        }
    ],
}


def _seed_pending_question_store(store: Path) -> None:
    """Write a cursor-shaped ``store.db`` holding one pending ``AskQuestion`` call.

    The JSON sits in binary checkpoint noise, with non-ASCII as raw UTF-8.
    """
    message = {
        "id": "1",
        "role": "assistant",
        "content": [
            {
                "type": "tool-call",
                "toolCallId": "toolu_cursor_multiselect",
                "toolName": "AskQuestion",
                "args": _ASKQUESTION_ARGS,
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
def test_cursor_multiselect_question_renders_checkboxes(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """A cursor ``allowMultiple`` question must render checkboxes, not radios.

    Detects the pending call from a seeded ``store.db``, runs the real runner
    mirror (``_run_one_question``) against the live server, then asserts in the
    browser that the form shows the stored text exactly and honors multi-select:
    the options are checkboxes and two of them can be selected at once.
    """
    base_url, session_id = seeded_session

    store = tmp_path / "store.db"
    _seed_pending_question_store(store)
    calls = read_cursor_pending_tool_calls(store)
    assert len(calls) == 1 and calls[0].tool_name == "AskQuestion"
    call = calls[0]
    elicitation_id = cursor_tool_call_elicitation_id(session_id, call.tool_call_id)
    mirror_result: dict[str, object] = {}

    async def _mirror() -> None:
        # The exact coroutine the cursor-native supervisor runs for a detected
        # AskQuestion: translate args -> POST the cursor-permission-request
        # hook -> park for the web verdict -> drive the TUI picker. The tmp
        # bridge dir advertises no tmux pane, so the final keystroke delivery
        # degrades benignly (logged + skipped) exactly as on a dead pane.
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
    browser_failure: BaseException | None = None
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
        rendered = form.inner_text()
        assert _PROMPT in rendered, f"question card shows {rendered!r}"

        # THE BUG: cursor marked this question multi-select (``allowMultiple``),
        # so the form must render checkboxes. On the buggy build the flag is
        # dropped in translation (``_askquestion_payload`` reads ``multiSelect``,
        # a field cursor never sends) and these options render as radio inputs
        # — single-select — so this assertion fails.
        option_a = form.get_by_role("checkbox", name=_OPTION_A)
        option_b = form.get_by_role("checkbox", name=_OPTION_B)
        expect(option_a).to_be_visible(timeout=5_000)
        expect(option_b).to_be_visible()

        # The user-observable multi-select contract: selecting a second option
        # keeps the first selected (radios would drop it).
        option_a.check()
        option_b.check()
        expect(option_a).to_be_checked()
        expect(option_b).to_be_checked()

        form.locator(_SUBMIT).click()

        # The verdict drains the parked hook: the card settles to responded and
        # the mirror coroutine returns (its TUI keystroke delivery is benignly
        # skipped — no pane is advertised in the tmp bridge dir).
        responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
        expect(responded).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    except BaseException as exc:
        browser_failure = exc
        raise
    finally:
        thread.join(timeout=10)
        fallback_note = ""
        if thread.is_alive():
            # The form assertions failed before a verdict was submitted (the
            # buggy build's radio render) — resolve the parked elicitation so
            # the mirror thread exits instead of parking for the hook timeout.
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
        stuck_note = f"cursor question mirror never received a web verdict{fallback_note}"
        if browser_failure is not None and thread.is_alive():
            browser_failure.add_note(stuck_note)

    assert not thread.is_alive(), stuck_note
