"""E2E: real Codex honours "Approve for this session" on a file-change approval.

Under the "Read only" launch args every edit asks the human. Choosing the
session-scoped approve on the first edit returns ``acceptForSession``; Codex
must then apply a later edit to the same file without asking again. The
synthetic hook round trip lives in ``test_codex_mcp_persistence.py``.
"""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

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

_APPROVAL_CARD = '[data-testid="approval-card"]'
_PENDING_CARD = f'{_APPROVAL_CARD}[data-state="pending"]'
_RESPONDED_CARD = f'{_APPROVAL_CARD}[data-state="responded"]'
_SESSION_BUTTON = "Approve for this session"
_SESSION_LABEL = "Approved for this session"
_CARD_TIMEOUT_MS = 15_000
_CODEX_TURN_TIMEOUT_MS = 240_000
# The New Chat dialog's "Read only" launch args: every edit asks the human.
_READ_ONLY_LAUNCH_ARGS = ["--sandbox", "read-only", "--ask-for-approval", "on-request"]


@pytest.fixture
def codex_read_only_mock_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """A codex-native session created with the New Chat dialog's "Read only" launch args.

    The workspace is a scratch directory so the approved patch lands there
    rather than in the repository.
    """
    workspace = tmp_path_factory.mktemp("codex-file-change-workspace")
    with native_codex_mock_session_context(
        live_server,
        mock_llm_server_url,
        tmp_path_factory,
        workspace=workspace,
        terminal_launch_args=_READ_ONLY_LAUNCH_ARGS,
    ) as (base_url, session_id):
        yield (base_url, session_id, workspace)


def _apply_patch_call(call_id: str, patch_body: str) -> dict:
    """A mocked ``exec_command`` turn that runs Codex's ``apply_patch`` heredoc."""
    patch = f"apply_patch <<'EOF'\n*** Begin Patch\n{patch_body}*** End Patch\nEOF"
    return {
        "tool_calls": [
            {"call_id": call_id, "name": "exec_command", "arguments": json.dumps({"cmd": patch})}
        ]
    }


@pytest.mark.skipif(
    shutil.which("codex") is None or shutil.which("tmux") is None,
    reason="codex-native e2e needs the `codex` CLI and `tmux` on PATH.",
)
@pytest.mark.timeout(600)
@pytest.mark.parametrize(
    ("approve_button", "responded_label", "expected_prompts"),
    [(_SESSION_BUTTON, _SESSION_LABEL, 1), ("Approve", "Approved", 2)],
    ids=["session", "one-time-control"],
)
def test_codex_native_file_change_session_approval_skips_later_prompts(
    request: pytest.FixtureRequest,
    codex_read_only_mock_session: tuple[str, str, Path],
    mock_llm_server_url: str,
    approve_button: str,
    responded_label: str,
    expected_prompts: int,
) -> None:
    """Approving the first edit for the session lets Codex edit that file again unprompted.

    The one-time control proves the second edit would otherwise ask again.
    """
    base_url, session_id, workspace = codex_read_only_mock_session
    nonce = uuid.uuid4().hex[:8]
    marker = f"filechange-{nonce}"
    done = f"filechange-done-{nonce}"
    notes_name = f"notes-{nonce}.md"

    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            _apply_patch_call(f"add-{nonce}", f"*** Add File: {notes_name}\n+notes\n"),
            _apply_patch_call(
                f"update-{nonce}", f"*** Update File: {notes_name}\n@@\n notes\n+more notes\n"
            ),
        ]
        + [{"text": done}] * 6,
        key=marker,
        match=marker,
        required_tools=["exec_command"],
    )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)
    _send(
        page,
        f"Context marker {marker}. Add {notes_name}, extend it, then reply with exactly: {done}",
    )

    for answered in range(1, expected_prompts + 1):
        card = page.locator(_PENDING_CARD).filter(has_text="modify files").first
        expect(card).to_be_visible(timeout=_CODEX_TURN_TIMEOUT_MS)
        for button in ("Approve", _SESSION_BUTTON, "Reject"):
            expect(card.get_by_role("button", name=button, exact=True)).to_be_visible()
        card.get_by_role("button", name=approve_button, exact=True).click()
        expect(page.locator(_RESPONDED_CARD).filter(has_text=responded_label)).to_have_count(
            answered, timeout=_CARD_TIMEOUT_MS
        )

    reply = page.locator(_ASSISTANT, has_text=done).first
    expect(reply.or_(page.locator(_PENDING_CARD)).first).to_be_visible(
        timeout=_CODEX_TURN_TIMEOUT_MS
    )
    expect(page.locator(_PENDING_CARD), "Codex asked for an unexpected approval").to_have_count(0)
    expect(reply).to_be_visible()
    expect(page.locator(_APPROVAL_CARD)).to_have_count(expected_prompts)
    assert (workspace / notes_name).read_text() == "notes\nmore notes\n"
