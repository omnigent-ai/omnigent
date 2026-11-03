"""E2E: real Codex permission prompts in a "Read only" session offer a remembered approval."""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _CODEX_MOCK_MODEL,
    configure_mock_llm,
    native_codex_mock_session_context,
    reset_mock_llm,
    set_fallback_mock_llm,
)

from ..messages.test_message_render_parity import _ASSISTANT, _ensure_chat_view, _send

pytestmark = [
    pytest.mark.skipif(
        shutil.which("codex") is None or shutil.which("tmux") is None,
        reason="codex-native e2e needs the `codex` CLI and `tmux` on PATH.",
    ),
    pytest.mark.timeout(600),
]

_PENDING_CARD = '[data-testid="approval-card"][data-state="pending"]'
_RESPONDED_CARD = '[data-testid="approval-card"][data-state="responded"]'
_QUESTION_FORM = '[data-testid="ask-user-question-form"]'
_REMEMBER_BUTTON = "Approve for this session"
_READ_ONLY_LAUNCH_ARGS = ["--sandbox", "read-only", "--ask-for-approval", "on-request"]
_CODEX_TURN_TIMEOUT_MS = 240_000
_HOLD_MS = 10_000


@pytest.fixture
def codex_read_only_session(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    workspace = tmp_path_factory.mktemp("codex-permission-workspace")
    with native_codex_mock_session_context(
        live_server,
        mock_llm_server_url,
        tmp_path_factory,
        workspace=workspace,
        terminal_launch_args=_READ_ONLY_LAUNCH_ARGS + getattr(request, "param", []),
    ) as (base_url, session_id):
        yield base_url, session_id, workspace


def _open_chat(request: pytest.FixtureRequest, base_url: str, session_id: str) -> Page:
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=60_000)
    _ensure_chat_view(page)
    return page


def _script_turn(mock_llm_server_url: str, marker: str, tool_turns: list[dict], done: str) -> None:
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        tool_turns + [{"text": done}] * 6,
        key=marker,
        match=marker,
        required_tools=["exec_command"],
    )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")


def _apply_patch_call(call_id: str, patch_body: str) -> dict:
    patch = f"apply_patch <<'EOF'\n*** Begin Patch\n{patch_body}*** End Patch\nEOF"
    return {
        "tool_calls": [
            {"call_id": call_id, "name": "exec_command", "arguments": json.dumps({"cmd": patch})}
        ]
    }


def _output_text(output: str | list[dict[str, str]]) -> str:
    # Newer Codex CLIs send tool output as a list of input_text items.
    if isinstance(output, str):
        return output
    return "\n".join(part.get("text", "") for part in output)


def _tool_outputs(mock_llm_server_url: str, marker: str) -> dict[str, str]:
    """Return the tool output text Codex sent the mock model for this turn, keyed by call id."""
    response = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0)
    response.raise_for_status()
    return {
        item["call_id"]: _output_text(item["output"])
        for captured in response.json()["requests"]
        if marker in json.dumps(captured)
        for item in captured.get("input", [])
        if item.get("type") == "function_call_output"
    }


@pytest.mark.parametrize(
    ("remember", "expected_prompts"),
    [(True, 1), (False, 2)],
    ids=["remember", "one-time-control"],
)
def test_codex_file_change_prompt_offers_remembered_approval(
    request: pytest.FixtureRequest,
    codex_read_only_session: tuple[str, str, Path],
    mock_llm_server_url: str,
    remember: bool,
    expected_prompts: int,
) -> None:
    """A remembered approval of the first edit lets Codex edit the file again unprompted."""
    base_url, session_id, workspace = codex_read_only_session
    nonce = uuid.uuid4().hex[:8]
    marker = f"filechange-{nonce}"
    done = f"filechange-done-{nonce}"
    notes_name = f"notes-{nonce}.md"
    _script_turn(
        mock_llm_server_url,
        marker,
        [
            _apply_patch_call(f"add-{nonce}", f"*** Add File: {notes_name}\n+notes\n"),
            _apply_patch_call(
                f"update-{nonce}", f"*** Update File: {notes_name}\n@@\n notes\n+more notes\n"
            ),
        ],
        done,
    )
    page = _open_chat(request, base_url, session_id)
    _send(page, f"Context marker {marker}. Add {notes_name}, extend it, then reply: {done}")

    for answered in range(1, expected_prompts + 1):
        card = page.locator(_PENDING_CARD).filter(has_text="modify files").first
        expect(card).to_be_visible(timeout=_CODEX_TURN_TIMEOUT_MS)
        approve = card.get_by_role("button", name="Approve", exact=True)
        expect(approve).to_be_visible()
        expect(card.get_by_role("button", name="Reject", exact=True)).to_be_visible()
        if remember:
            remember_button = card.get_by_role("button", name=_REMEMBER_BUTTON, exact=True)
            expect(remember_button).to_be_visible(timeout=_HOLD_MS)
            remember_button.click()
        else:
            approve.click()
        expect(page.locator(_RESPONDED_CARD)).to_have_count(answered, timeout=15_000)

    reply = page.locator(_ASSISTANT, has_text=done).first
    expect(reply.or_(page.locator(_PENDING_CARD)).first).to_be_visible(
        timeout=_CODEX_TURN_TIMEOUT_MS
    )
    expect(page.locator(_PENDING_CARD), "Codex asked again for the same file").to_have_count(0)
    expect(reply).to_be_visible()
    assert (workspace / notes_name).read_text() == "notes\nmore notes\n"


# With this feature off, Codex 0.158 asks MCP tool approvals through request_user_input.
# Codex 0.142 does not ask this question, so only newer CLIs exercise that path here.
@pytest.mark.parametrize(
    "codex_read_only_session",
    [["--disable", "tool_call_mcp_elicitation"]],
    indirect=True,
    ids=["mcp-approval-as-user-input"],
)
@pytest.mark.parametrize(
    ("remember", "expected_prompts"),
    [(True, 1), (False, 2)],
    ids=["remember", "one-time-control"],
)
def test_codex_mcp_approval_question_remembers_session_approval(
    request: pytest.FixtureRequest,
    codex_read_only_session: tuple[str, str, Path],
    mock_llm_server_url: str,
    remember: bool,
    expected_prompts: int,
) -> None:
    """A question-shaped MCP approval is a permission card; session approval covers a rerun."""
    base_url, session_id, _workspace = codex_read_only_session
    nonce = uuid.uuid4().hex[:8]
    marker = f"mcpsession-{nonce}"
    done = f"mcpsession-done-{nonce}"
    inbox_turns = [
        {
            "tool_calls": [
                {
                    "call_id": f"inbox-{index}-{nonce}",
                    "name": "sys_read_inbox",
                    "namespace": "mcp__omnigent",
                    "arguments": "{}",
                }
            ]
        }
        for index in range(2)
    ]
    _script_turn(mock_llm_server_url, marker, inbox_turns, done)
    page = _open_chat(request, base_url, session_id)
    _send(page, f"Context marker {marker}. Check your inbox twice, then reply: {done}")

    for answered in range(1, expected_prompts + 1):
        card = page.locator(_PENDING_CARD).filter(has_text="sys_read_inbox").first
        expect(card).to_be_visible(timeout=_CODEX_TURN_TIMEOUT_MS)
        expect(card.locator(_QUESTION_FORM)).to_have_count(0, timeout=_HOLD_MS)
        approve = card.get_by_role("button", name="Approve", exact=True)
        remember_button = card.get_by_role("button", name=_REMEMBER_BUTTON, exact=True)
        expect(approve).to_be_visible()
        expect(remember_button).to_be_visible()
        expect(card.get_by_role("button", name="Reject", exact=True)).to_be_visible()
        (remember_button if remember else approve).click()
        expect(page.locator(_RESPONDED_CARD)).to_have_count(answered, timeout=15_000)

    reply = page.locator(_ASSISTANT, has_text=done).first
    expect(reply.or_(page.locator(_PENDING_CARD)).first).to_be_visible(
        timeout=_CODEX_TURN_TIMEOUT_MS
    )
    expect(page.locator(_PENDING_CARD), "Codex asked again for the same tool").to_have_count(0)
    expect(reply).to_be_visible()
    outputs = _tool_outputs(mock_llm_server_url, marker)
    for turn in inbox_turns:
        call_id = turn["tool_calls"][0]["call_id"]
        assert "Inbox is empty" in outputs.get(call_id, ""), outputs
