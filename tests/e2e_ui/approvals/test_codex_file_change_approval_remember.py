"""E2E: a Codex file-change approval offers the session-scoped "don't ask again" choice.

Codex's own prompt for ``item/fileChange/requestApproval`` offers "Yes, and
don't ask again for these files" (the protocol's ``acceptForSession``
decision). The web card must offer the same choice next to Approve / Reject
and return ``acceptForSession`` to Codex when it is chosen.

Two legs: a synthetic hook POST against ``seeded_session`` (fast; exercises the
production protocol adapter and the SPA) and the real Codex CLI driven through
the composer, which proves Codex accepts the decision and finishes the turn.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.native_session import create_native_session
from tests.e2e_ui.conftest import (
    _bind_session_runner,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    configure_mock_llm,
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
# Must match the model in the mock openai provider config written by
# _temp_omnigent_mock_config (conftest._CODEX_MOCK_MODEL).
_CODEX_MOCK_MODEL = "gpt-4o"
# The New Chat dialog's "Read only" launch args: every edit asks the human.
_READ_ONLY_LAUNCH_ARGS = ["--sandbox", "read-only", "--ask-for-approval", "on-request"]


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _wait_for(predicate, *, timeout_s: float = 30.0, interval_s: float = 0.25) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")


def _expect_session_choice(card) -> None:
    """Assert the pending file-change card offers Approve / session-scoped approve / Reject."""
    expect(card).to_be_visible(timeout=_CODEX_TURN_TIMEOUT_MS)
    expect(card.get_by_role("button", name="Approve", exact=True)).to_be_visible()
    expect(card.get_by_role("button", name="Reject", exact=True)).to_be_visible()
    expect(
        card.get_by_role("button", name=_SESSION_BUTTON, exact=True),
        "file-change approval card offers no session-scoped 'don't ask again' choice",
    ).to_be_visible()


@pytest.mark.timeout(90)
def test_codex_file_change_approval_offers_dont_ask_again(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The file-change card exposes a session-scoped approve that returns acceptForSession."""
    base_url, session_id = seeded_session
    result_holder: dict = {}
    payload = {
        "id": 21,
        "method": "item/fileChange/requestApproval",
        "params": {
            "threadId": "thread_e2e",
            "turnId": "turn_e2e",
            "itemId": "item_patch_e2e",
            "startedAtMs": 1,
            "reason": None,
            "grantRoot": None,
        },
    }

    def _post_hook() -> None:
        try:
            resp = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/hooks/codex-elicitation-request",
                json=payload,
                timeout=60.0,
            )
            resp.raise_for_status()
            result_holder["response"] = resp.json()
        except Exception as exc:
            result_holder["error"] = exc

    hook_thread = threading.Thread(target=_post_hook, daemon=True)
    hook_thread.start()
    _wait_for(lambda: bool(_pending_elicitations(base_url, session_id)))

    page.goto(f"{base_url}/c/{session_id}")
    card = page.locator(_PENDING_CARD).filter(has_text="modify files").first
    _expect_session_choice(card)
    card.get_by_role("button", name=_SESSION_BUTTON, exact=True).click()

    expect(page.locator(_RESPONDED_CARD).filter(has_text=_SESSION_LABEL).first).to_be_visible(
        timeout=_CARD_TIMEOUT_MS
    )
    hook_thread.join(timeout=30)
    if "error" in result_holder:
        raise AssertionError(f"hook thread failed: {result_holder['error']}")
    assert result_holder["response"] == {"decision": "acceptForSession"}
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))


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
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    workspace = tmp_path_factory.mktemp("codex-file-change-workspace")
    with _temp_omnigent_mock_config(
        mock_llm_server_url, "codex", workflow_owned=bool(_server_state.get("workflow_owned"))
    ):
        created = create_native_session(
            httpx,
            live_server,
            harness="codex",
            metadata={
                "workspace": str(workspace),
                "terminal_launch_args": _READ_ONLY_LAUNCH_ARGS,
            },
        )
        session_id = str(created["session_id"])
        _bind_session_runner(live_server, session_id, runner_id)
        try:
            yield (live_server, session_id, workspace)
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


@pytest.mark.skipif(
    shutil.which("codex") is None or shutil.which("tmux") is None,
    reason="codex-native e2e needs the `codex` CLI and `tmux` on PATH.",
)
@pytest.mark.timeout(600)
def test_codex_native_file_change_prompt_offers_dont_ask_again(
    request: pytest.FixtureRequest,
    codex_read_only_mock_session: tuple[str, str, Path],
    mock_llm_server_url: str,
) -> None:
    """Real Codex: an apply_patch under a read-only sandbox parks a file-change card
    with the session-scoped choice, and choosing it lets Codex apply the patch."""
    base_url, session_id, workspace = codex_read_only_mock_session
    nonce = uuid.uuid4().hex[:8]
    marker = f"filechange-{nonce}"
    done = f"filechange-done-{nonce}"
    notes_name = f"notes-{nonce}.md"
    patch = (
        "apply_patch <<'EOF'\n*** Begin Patch\n"
        f"*** Add File: {notes_name}\n+notes\n*** End Patch\nEOF"
    )

    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"patch-{nonce}",
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": patch}),
                    }
                ]
            },
        ]
        + [{"text": done}] * 6,
        key=marker,
        match=marker,
    )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)
    _send(
        page,
        f"Context marker {marker}. Add {notes_name} to the repo, then reply with exactly: {done}",
    )

    card = page.locator(_PENDING_CARD).filter(has_text="modify files").first
    _expect_session_choice(card)
    card.get_by_role("button", name=_SESSION_BUTTON, exact=True).click()

    expect(page.locator(_RESPONDED_CARD).filter(has_text=_SESSION_LABEL).first).to_be_visible(
        timeout=_CARD_TIMEOUT_MS
    )
    # Codex accepted ``acceptForSession``: the patch landed and the turn finished.
    expect(page.locator(_ASSISTANT, has_text=done).first).to_be_visible(
        timeout=_CODEX_TURN_TIMEOUT_MS
    )
    assert (workspace / notes_name).read_text() == "notes\n"
