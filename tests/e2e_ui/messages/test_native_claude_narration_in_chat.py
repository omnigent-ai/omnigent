"""UI journey: Claude Code narration shown in the terminal must reach the chat.

Claude Code renders narration-tagged thinking blocks as ordinary assistant
prose with a trailing "summary" hint. The chat must show those messages, and
any command they ask the user to run, in the terminal's order around the
answered AskUserQuestion cards.

Requires the mock LLM lane (``LLM_API_KEY`` unset).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e._native_resume_helpers import wait_for_terminal_ready
from tests.e2e_ui.conftest import (
    configure_mock_llm,
    native_claude_mock_session_context,
    reset_mock_llm,
    set_fallback_mock_llm,
)

from .test_message_render_parity import _ASSISTANT, _WORKING, _ensure_chat_view, _select_view_mode
from .test_native_claude_reasoning_in_chat import _transcript_has_thinking
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _pane_text,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

pytestmark = pytest.mark.skipif(
    bool(os.environ.get("LLM_API_KEY")),
    reason="requires the mock LLM lane: the turn scripts narration blocks deterministically",
)

# Claude Code reads the block kind from signature field 2 -> 1 -> 8.
_NARRATION_SIGNATURE = base64.b64encode(b"\x12\x0d\x0a\x0b\x42\x09narration").decode()
_SIGN_IN_COMMAND = (
    "! databricks auth login --host https://workspace.example.com --profile example-profile"
)
# Claude Code only offers auto mode on models newer than claude-opus-4-6.
_AUTO_MODE_MODEL = "claude-opus-4-8"
_TURN_TIMEOUT_MS = 90_000
_TRANSCRIPT_SETTLE_TIMEOUT_S = 30.0


@pytest.fixture
def auto_mode_claude_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    claude_managed_settings: None,
) -> Iterator[tuple[str, str]]:
    """A claude-native session whose Claude Code runs in auto permission mode."""
    with native_claude_mock_session_context(
        live_server,
        mock_llm_server_url,
        tmp_path_factory,
        terminal_launch_args=["--model", _AUTO_MODE_MODEL, "--permission-mode", "auto"],
    ) as session:
        yield session


def _ask(call_id: str, question: str, options: tuple[str, str]) -> dict[str, str]:
    return {
        "call_id": call_id,
        "name": "AskUserQuestion",
        "arguments": json.dumps(
            {
                "questions": [
                    {
                        "header": "Choice",
                        "question": question,
                        "multiSelect": False,
                        "options": [
                            {"label": label, "description": f"Choose {label}"} for label in options
                        ],
                    }
                ]
            }
        ),
    }


def _answer_question(
    page: Page,
    base_url: str,
    session_id: str,
    question: str,
    option: str,
    panes: list[str],
    *,
    shown_while_pending: tuple[Locator, ...] = (),
) -> None:
    """Answer *question* from its chat card, sampling the TUI until the card appears.

    Each *shown_while_pending* entry must be visible in chat before the answer.
    """
    form = page.get_by_test_id("ask-user-question-form").filter(has_text=question)
    deadline = time.monotonic() + _TURN_TIMEOUT_MS / 1000
    while form.count() == 0 and time.monotonic() < deadline:
        panes.append(_pane_text(base_url, session_id))
        page.wait_for_timeout(250)
    expect(form).to_be_visible(timeout=10_000)
    for entry in shown_while_pending:
        expect(entry.filter(visible=True).first).to_be_visible(timeout=10_000)
    panes.append(_pane_text(base_url, session_id))
    form.get_by_role("radio", name=option).check()
    form.get_by_test_id("ask-user-question-submit").click()
    expect(form).to_have_count(0, timeout=30_000)


def _chat_layout(page: Page, entries: dict[str, Locator]) -> dict[str, object]:
    """Which entries are visible, which exist only hidden, and the visible document order."""
    shown = {name: locator.filter(visible=True) for name, locator in entries.items()}
    visible = {name: locator.count() > 0 for name, locator in shown.items()}
    hidden_only = [
        name for name, locator in entries.items() if locator.count() and not visible[name]
    ]
    order = page.evaluate(
        "(items) => items.sort((a, b) =>"
        " a[1].compareDocumentPosition(b[1]) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1)"
        ".map((item) => item[0])",
        [[name, shown[name].first.element_handle()] for name in entries if visible[name]],
    )
    containers = {
        name: shown[name].first.evaluate(
            "(node) => { const ids = []; for (let el = node; el; el = el.parentElement)"
            " { if (el.dataset?.testid) ids.push(el.dataset.testid); } return ids.join(' < '); }"
        )
        for name in entries
        if visible[name]
    }
    return {
        "visible": visible,
        "hidden_only": hidden_only,
        "order": order,
        "containers": containers,
    }


def _item_summary(base_url: str, session_id: str) -> list[str]:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    summary = []
    for item in resp.json().get("data", []):
        text = json.dumps(item.get("content") or item.get("summary") or item.get("name") or "")
        summary.append(f"{item.get('type')}/{item.get('role') or '-'}: {text[:160]}")
    return summary


@pytest.mark.nightly
@pytest.mark.timeout(300)
def test_native_claude_narration_shows_in_chat_in_terminal_order(
    request: pytest.FixtureRequest,
    auto_mode_claude_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Narration the terminal shows must render as chat messages, in terminal order."""
    base_url, session_id = auto_mode_claude_session
    nonce = uuid.uuid4().hex[:8]
    user_marker = f"usr{nonce}"
    first_narration = f"narrone{nonce}"
    second_narration = f"narrtwo{nonce}"
    first_question = f"Which workspace should host the app ({nonce})?"
    second_question = f"Have you run the sign-in command ({nonce})?"
    final_marker = f"final{nonce}"

    reset_mock_llm(mock_llm_server_url)
    for key in ("default", _CLAUDE_MOCK_MODEL, _AUTO_MODE_MODEL):
        set_fallback_mock_llm(mock_llm_server_url, key, "ok")
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "thinking": (
                    f"Before asking which workspace you'd like to use, I'll check which "
                    f"signed-in workspaces can host the app. {first_narration}"
                ),
                "thinking_signature": _NARRATION_SIGNATURE,
                "tool_calls": [_ask(f"toolu_first_{nonce}", first_question, ("Alpha", "Bravo"))],
            },
            {
                "thinking": (
                    f"Got it, Alpha needs a fresh sign-in since no CLI profile exists yet. "
                    f"{second_narration}\n\n```\n{_SIGN_IN_COMMAND}\n```\n\n"
                    "Meanwhile, I'll clean up the unused secret scope."
                ),
                "thinking_signature": _NARRATION_SIGNATURE,
                "tool_calls": [_ask(f"toolu_second_{nonce}", second_question, ("Done", "Later"))],
            },
            {"text": f"The unused secret is deleted. {final_marker}"},
        ],
        match=user_marker,
        required_tools=["AskUserQuestion"],
    )
    with httpx.Client(base_url=base_url, timeout=10) as client:
        wait_for_terminal_ready(client, conversation_id=session_id, harness="claude", timeout=90)

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    deadline = time.monotonic() + 30
    while "auto mode on" not in _pane_text(base_url, session_id) and time.monotonic() < deadline:
        page.wait_for_timeout(250)
    panes = [_pane_text(base_url, session_id)]
    _log.info("TUI before the turn:\n%s", panes[0])
    assert "auto mode on" in panes[0], "Claude Code did not start in auto mode"

    card = page.get_by_test_id("approval-card")
    entries = {
        "first narration": page.get_by_text(first_narration),
        "first answered question": card.filter(has_text=first_question),
        "second narration": page.get_by_text(second_narration),
        "sign-in command block": page.locator("pre", has_text=_SIGN_IN_COMMAND.removeprefix("! ")),
        "second answered question": card.filter(has_text=second_question),
        "final message": page.locator(_ASSISTANT, has_text=final_marker),
    }

    _ensure_chat_view(page)
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(f"Move the app to a workspace that can host it ({user_marker}).")
    page.get_by_role("button", name="Send", exact=True).click()

    _answer_question(
        page,
        base_url,
        session_id,
        first_question,
        "Alpha",
        panes,
        shown_while_pending=(entries["first narration"],),
    )
    _answer_question(
        page,
        base_url,
        session_id,
        second_question,
        "Done",
        panes,
        shown_while_pending=(entries["second narration"], entries["sign-in command block"]),
    )
    final_bubble = page.locator(_ASSISTANT, has_text=final_marker).first
    expect(final_bubble).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)

    _select_view_mode(page, "Terminal")
    _wait_terminal_connected(page)
    panes.append(_pane_text(base_url, session_id))
    page.wait_for_timeout(2_000)
    _log.info("TUI after the turn:\n%s", panes[-1])

    deadline = time.monotonic() + _TRANSCRIPT_SETTLE_TIMEOUT_S
    while time.monotonic() < deadline and not (
        _transcript_has_thinking(first_narration) and _transcript_has_thinking(second_narration)
    ):
        time.sleep(0.5)
    assert _transcript_has_thinking(first_narration), "narration never reached Claude's transcript"
    assert _transcript_has_thinking(second_narration), (
        "narration never reached Claude's transcript"
    )
    terminal_text = "\n".join(panes)
    assert first_narration in terminal_text and second_narration in terminal_text, (
        "precondition failed: the TUI never showed the narration messages"
    )
    _log.info("items API: %s", json.dumps(_item_summary(base_url, session_id), indent=1))

    terminal_order = list(entries)
    layouts = {}
    for view in ("settled", "reloaded"):
        if view == "reloaded":
            page.reload()
        _ensure_chat_view(page)
        expect(entries["final message"].first).to_be_visible(timeout=_TURN_TIMEOUT_MS)
        page.wait_for_timeout(2_000)
        layouts[view] = _chat_layout(page, entries)
        _log.info("%s chat layout: %s", view, layouts[view])
    for view, layout in layouts.items():
        missing = [name for name, shown in layout["visible"].items() if not shown]
        assert not missing, f"{view} chat does not show {missing}; layout: {layout}"
        assert layout["order"] == terminal_order, (
            f"{view} chat order differs from the terminal: {layout}"
        )
