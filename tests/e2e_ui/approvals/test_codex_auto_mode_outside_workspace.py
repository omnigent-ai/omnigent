"""E2E regression: Codex Auto mode still asks for user approval.

A codex-native session created with the default permission stance runs
Codex's **Auto** approval preset (``workspace-write`` sandbox +
``on-request`` approval — the ``/permissions`` popup's "Auto" entry, the
New Chat dialog's "Default"). The reported bug: the session still parks
turns on a human approval prompt, which the reporter pinned to "working on
a folder that is outside of the working dir that codex started in" — an
out-of-workspace write makes Codex escalate the command
(``sandbox_permissions: "require_escalated"``), which raises
``item/commandExecution/requestApproval``; the forwarder publishes it as a
web elicitation and the SPA parks the turn on an ApprovalCard.

Journey (all user-observable):

1. create a codex-native session (default = Auto permission mode) whose
   workspace is folder A;
2. ask Codex to create a file in folder B, outside A;
3. the turn stops on an approval card instead of completing — despite the
   "auto" permission mode.

With ``LLM_API_KEY`` set (CI / local dev) the real gateway model drives the
journey — asked to create a file outside the workspace, it escalates the
command itself, which is exactly the reported trigger. Without the key, the
in-process mock LLM serves a scripted turn with the same escalated
``exec_command``, so the journey stays deterministic. Either way the
assertion encodes the DESIRED behavior — the turn completes with no
approval card — so this test FAILS while the bug is live and passes once
auto mode stops parking on approval.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _create_native_codex_session,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)

from ..messages.test_message_render_parity import _ASSISTANT, _ensure_chat_view, _send

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None or shutil.which("tmux") is None,
    reason="codex-native e2e needs the `codex` CLI and `tmux` on PATH.",
)

_APPROVAL_CARD = '[data-testid="approval-card"]'
# Must match the model in the mock openai provider config written by
# _temp_omnigent_mock_config (conftest._CODEX_MOCK_MODEL).
_CODEX_MOCK_MODEL = "gpt-4o"
# Codex boots in the session terminal on bind; cold CI runners are slow, and
# the mock turn itself is instant once the bridge is up.
_TURN_OUTCOME_TIMEOUT_MS = 240_000


@pytest.fixture
def codex_auto_mode_mock_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A codex-native session that ALWAYS runs against the mock Responses API.

    Mirrors ``native_codex_mock_session``'s shape but always writes the mock
    provider config, so environments without ``LLM_API_KEY`` get the scripted
    escalated command below. When the runner resolves a real gateway (e.g.
    ``LLM_API_KEY`` is set), the real model handles the same journey — the
    out-of-workspace instruction makes it escalate on its own — and the mock
    queue simply goes unconsumed.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    with _temp_omnigent_mock_config(mock_llm_server_url, "codex"):
        session_id = _create_native_codex_session(live_server, runner_id)
        try:
            yield (live_server, session_id)
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


@pytest.mark.timeout(600)
def test_codex_auto_mode_outside_workspace_completes_without_approval(
    page: Page,
    codex_auto_mode_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """An Auto-mode codex turn touching a folder outside the workspace
    must complete without parking on a human approval card."""
    base_url, session_id = codex_auto_mode_mock_session

    nonce = uuid.uuid4().hex[:8]
    user_marker = f"autoesc-{nonce}"
    assistant_token = f"autoesc-done-{nonce}"
    # A folder OUTSIDE the session workspace (the repo root): the reporter's
    # trigger. $HOME is not inside the workspace-write sandbox's writable
    # roots, so Codex escalates this command.
    outside_dir = Path.home() / f"autoesc-outside-{nonce}"
    outside_cmd = f"mkdir -p {outside_dir} && echo notes > {outside_dir}/notes.txt"

    reset_mock_llm(mock_llm_server_url)
    # Main-turn script: (1) the escalated out-of-workspace command — exactly
    # what Codex's model emits after the sandbox denies a plain write outside
    # the workspace; then completion tokens. Every internal request that
    # embeds the chat transcript (Codex helper threads, title generation)
    # also matches this marker queue, so pad with extra token entries — a
    # stray consumer draining one must not starve the main turn's completion.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call-{nonce}",
                        "name": "exec_command",
                        "arguments": json.dumps(
                            {
                                "cmd": outside_cmd,
                                "sandbox_permissions": "require_escalated",
                                "justification": ("Create the notes file outside the workspace."),
                            }
                        ),
                    }
                ]
            },
        ]
        + [{"text": assistant_token}] * 6,
        key=user_marker,
        match=user_marker,
    )
    # The automatic approval reviewer's queue. The reviewer's request embeds
    # the escalated command verbatim, and only the reviewer sees the full
    # command string (the chat message names just the directory), so matching
    # on it routes reviewer calls here. Content matches pick the LONGEST
    # matching token, so this queue beats the marker queue for reviewer
    # requests even though they also carry the transcript marker.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": '{"outcome":"allow"}'}] * 6,
        key=f"auto-reviewer-{nonce}",
        match=outside_cmd,
    )
    # Stray internal Codex calls (model-routed, no transcript) must not stall.
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)
    _send(
        page,
        f"Context marker {user_marker}. Create a notes file in {outside_dir} "
        f"(outside this workspace), then reply with exactly: {assistant_token}",
    )

    approval_card = page.locator(_APPROVAL_CARD)
    reply = page.locator(_ASSISTANT, has_text=assistant_token)

    # Wait for the turn to reach a decisive state: either it completed (the
    # token bubble rendered) or it parked on the approval card.
    expect(approval_card.or_(reply).first).to_be_visible(timeout=_TURN_OUTCOME_TIMEOUT_MS)

    # THE BUG: despite the Auto permission mode, the escalated
    # out-of-workspace command parks the turn on a human approval card.
    expect(
        approval_card,
        "Codex Auto permission mode still asked for user approval: an "
        "out-of-workspace command raised an approval card instead of running.",
    ).to_have_count(0)

    # And the turn must actually have completed.
    expect(reply.first).to_be_visible(timeout=60_000)
