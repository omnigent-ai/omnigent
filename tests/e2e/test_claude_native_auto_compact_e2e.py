"""A claude-native session that fills its context must keep answering.

Journey: one claude-native session receives prompt after prompt, as it does
when an automation keeps continuing the same session, until its history
outgrows the model's context window. The user's own Claude settings say
``autoCompactEnabled: false``, and nobody is at the terminal to type
``/compact``.

Expected: Claude compacts automatically and every prompt is answered. Without
Omnigent's launch settings keeping auto-compaction on, Claude refuses each turn
with "Prompt is too long" (``context_length_exceeded``) once the context fills,
and every later prompt fails the same way.

A real server, runner and the unmodified Claude Code CLI run against a local
Messages API that scripts only model replies and enforces the context window;
Claude's token accounting, compaction and refusal run live.

Run with: uv run --no-sync pytest tests/e2e/test_claude_native_auto_compact_e2e.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests._helpers.native_session import create_native_session
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, send_user_text
from tests.e2e.conftest import configure_mock_llm, get_mock_requests, set_fallback_mock_llm

_MODEL = "claude-sonnet-4-20250514"
_CONTEXT_WINDOW_TOKENS = 100_000
# Each reply adds ~11K tokens, so the history passes the window well before the
# last turn and Claude must compact (or refuse) along the way.
_TURNS = 12
_REPLY_WORDS = 5_500
_COMPACTION_PROMPT = "Your task is to create a detailed summary of the conversation so far"
# Covers Claude Code boot on the first turn and a compaction on a later one.
_TURN_TIMEOUT_S = 180.0
_BIND_TIMEOUT_S = 180.0

pytestmark = [
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        shutil.which("claude") is None or shutil.which("tmux") is None,
        reason="requires the real Claude Code CLI and tmux",
    ),
]


def _filler(turn: int) -> str:
    return " ".join(f"w{turn}x{index % 997}" for index in range(_REPLY_WORDS))


def _items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    response = client.get(
        f"/v1/sessions/{session_id}/items", params={"limit": 500, "order": "asc"}
    )
    response.raise_for_status()
    return list(response.json()["data"])


def _assistant_text(item: dict[str, Any]) -> str:
    if item.get("type") != "message" or item.get("role") != "assistant":
        return ""
    return " ".join(
        str(block.get("text", ""))
        for block in item.get("content") or []
        if isinstance(block, dict)
    )


def _ask(client: httpx.Client, mock_url: str, session_id: str, turn: int, uid: str) -> None:
    """Send one prompt and wait for its reply, failing fast if Claude refuses the turn.

    :param client: Client bound to the server.
    :param mock_url: Mock Messages API base URL.
    :param session_id: Session that receives the prompt.
    :param turn: 1-based turn number.
    :param uid: Per-test token suffix.
    """
    token = f"turn-{turn:02d}-{uid}"
    marker = f"reply-{turn:02d}-{uid}"
    configure_mock_llm(mock_url, [{"text": f"{marker} {_filler(turn)}"}] * 3, match=token)
    send_user_text(client, session_id=session_id, text=f"Continue the filler. {token}")
    deadline = time.monotonic() + _TURN_TIMEOUT_S
    while time.monotonic() < deadline:
        session = client.get(f"/v1/sessions/{session_id}", params={"include_items": "false"})
        session.raise_for_status()
        error = session.json().get("last_task_error")
        assert error is None, (
            f"turn {turn} failed instead of Claude compacting its full context "
            f"(the user's autoCompactEnabled=false was not overridden): {error}"
        )
        if any(marker in _assistant_text(item) for item in _items(client, session_id)):
            return
        time.sleep(1.0)
    raise AssertionError(f"turn {turn} got no reply within {_TURN_TIMEOUT_S:g}s")


def test_full_context_auto_compacts_despite_user_setting(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
) -> None:
    """Every prompt is answered after the context fills, with auto-compact off for the user."""
    mock_url = isolated_mock_llm_server_url
    uid = uuid.uuid4().hex[:8]
    httpx.post(
        f"{mock_url}/mock/context_window", json={"max_input_tokens": _CONTEXT_WINDOW_TOKENS}
    ).raise_for_status()
    configure_mock_llm(
        mock_url,
        [{"text": "<summary>Earlier turns exchanged filler text.</summary>"}] * 10,
        match=_COMPACTION_PROMPT,
    )
    set_fallback_mock_llm(mock_url, key="_policy_llm_", text='{"action":"allow","reason":""}')

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    claude_config = tmp_path / "claude-config"
    claude_config.mkdir()
    # The user's own setting under test.
    (claude_config / "settings.json").write_text(json.dumps({"autoCompactEnabled": False}))
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    # Claude's own environment auth, as a launcher such as a desktop app provides it.
    runner_env = {
        "CLAUDE_CONFIG_DIR": str(claude_config),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ANTHROPIC_BASE_URL": mock_url,
        "ANTHROPIC_AUTH_TOKEN": "mock-key",
        "ANTHROPIC_MODEL": _MODEL,
        # Claude sizes its context to the window the mock enforces.
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(_CONTEXT_WINDOW_TOKENS),
        # Keep a machine-managed Claude policy from replacing the mock provider.
        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1",
    }

    with (
        server_runner(tmp_path / "stack", base_env=base_env, workspace=workspace) as stack,
        httpx.Client(
            base_url=stack.base_url,
            trust_env=False,
            timeout=60,
            headers={"x-omnigent-background-session-titles": "off"},
        ) as client,
    ):
        stack.start_runner(env=runner_env)
        session_id = str(
            create_native_session(
                client, stack.base_url, harness="claude", metadata={"workspace": str(workspace)}
            )["session_id"]
        )
        bind_session_runner(
            client.patch, stack.base_url, session_id, stack.runner_id, timeout=_BIND_TIMEOUT_S
        )

        for turn in range(1, _TURNS + 1):
            _ask(client, mock_url, session_id, turn, uid)

        items = _items(client, session_id)
        assert len(json.dumps(items)) > 4 * _CONTEXT_WINDOW_TOKENS, (
            "the history never outgrew the context window, so the scenario proves nothing"
        )
        assert any(_COMPACTION_PROMPT in json.dumps(r) for r in get_mock_requests(mock_url)), (
            "every turn was answered but Claude never compacted"
        )
        assert any(item.get("type") == "compaction" for item in items), (
            "Claude compacted but the session recorded no compaction"
        )
