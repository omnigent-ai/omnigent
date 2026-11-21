"""An automation must stop sending prompts into a session whose context is full.

Journey: an automation continues one claude-native session on every run. The
session's history has outgrown the model's context window and Claude cannot
compact it (auto-compaction is turned off), so a run's prompt fails with
``context_length_exceeded``.

Expected: the next run is recorded as failed with
``continued_session_context_full`` and sends nothing, so the owner sees why
the automation stopped working. Without the check, every run is delivered and
recorded as succeeded while Claude refuses each prompt, adding to the full
history each time.

A real server, runner, scheduler and the unmodified Claude Code CLI run
against a local Messages API that only scripts model replies; Claude's token
estimate and refusal run live.

Run with: uv run --no-sync pytest tests/e2e/test_claude_native_continued_context_full_e2e.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests._helpers.native_session import create_native_session
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner
from tests.e2e.conftest import set_fallback_mock_llm

_MODEL = "claude-sonnet-4-20250514[1m]"
# 360 x 15 KB of history is ~1.35M estimated tokens, past the 1M-window
# model's blocking limit, so a cold resume starts with a full context.
_HISTORY_ITEMS = 360
_HISTORY_ITEM_CHARS = 15_000
_PROMPT = "Check the queue for new requests."
_BIND_TIMEOUT_S = 180.0
_RUN_TIMEOUT_S = 180.0

pytestmark = [
    pytest.mark.timeout(600),
    pytest.mark.skipif(
        shutil.which("claude") is None or shutil.which("tmux") is None,
        reason="requires the real Claude Code CLI and tmux",
    ),
]


def _seed_history(database_uri: str, session_id: str) -> None:
    """Append a history larger than the model's context window to the session.

    Writes go straight to the server's store, as in the other cold-resume E2Es;
    there is no REST bulk-append.

    :param database_uri: The spawned server's SQLite URI.
    :param session_id: Conversation to append to.
    """
    store = SqlAlchemyConversationStore(database_uri)
    filler = "x" * _HISTORY_ITEM_CHARS
    items = []
    for index in range(_HISTORY_ITEMS):
        role = "user" if index % 2 == 0 else "assistant"
        items.append(
            NewConversationItem(
                type="message",
                response_id=f"resp_history_{index // 2}",
                data=MessageData(
                    role=role,
                    content=[
                        {
                            "type": "input_text" if role == "user" else "output_text",
                            "text": f"earlier run {index // 2}: {filler}",
                        }
                    ],
                    agent="claude" if role == "assistant" else None,
                ),
            )
        )
    for start in range(0, len(items), 20):
        store.append(session_id, items[start : start + 20])


def _prompts_sent(client: httpx.Client, session_id: str) -> int:
    """Count the automation prompts the session has received."""
    response = client.get(
        f"/v1/sessions/{session_id}/items", params={"limit": 1000, "order": "desc"}
    )
    response.raise_for_status()
    return sum(
        1
        for item in response.json()["data"]
        if item.get("type") == "message"
        and item.get("role") == "user"
        and _PROMPT in json.dumps(item.get("content"))
    )


def _run_now(client: httpx.Client, task_id: str) -> dict[str, Any]:
    """Fire the automation and return its run once the run is finished."""
    response = client.post(f"/v1/scheduled-tasks/{task_id}/run")
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]

    def finished() -> dict[str, Any] | None:
        runs = client.get(f"/v1/scheduled-tasks/{task_id}/runs").json()["runs"]
        run = next((row for row in runs if row["id"] == run_id), None)
        return run if run is not None and run["status"] in {"succeeded", "failed"} else None

    poll_until(finished, timeout_s=_RUN_TIMEOUT_S)
    run = finished()
    assert run is not None
    return run


def test_automation_stops_sending_into_a_full_context(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
) -> None:
    """After a run overflows the session's context, the next run fails without sending."""
    mock_url = isolated_mock_llm_server_url
    set_fallback_mock_llm(mock_url, key="default", text="Queue checked.")
    set_fallback_mock_llm(mock_url, key="_policy_llm_", text='{"action":"allow","reason":""}')

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    # Claude's own environment auth, as a launcher such as a desktop app provides it.
    runner_env = {
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config"),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ANTHROPIC_BASE_URL": mock_url,
        "ANTHROPIC_AUTH_TOKEN": "mock-key",
        "ANTHROPIC_MODEL": _MODEL,
        # Claude cannot compact, so the full context refuses every prompt.
        "DISABLE_AUTO_COMPACT": "1",
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
        created = create_native_session(
            client, stack.base_url, harness="claude", metadata={"workspace": str(workspace)}
        )
        session_id = str(created["session_id"])
        client.patch(
            f"/v1/sessions/{session_id}", json={"external_session_id": str(uuid.uuid4())}
        ).raise_for_status()
        _seed_history(stack.database_uri, session_id)
        # Binding cold-resumes Claude from a transcript rebuilt out of that history.
        bind_session_runner(
            client.patch, stack.base_url, session_id, stack.runner_id, timeout=_BIND_TIMEOUT_S
        )
        agent_id = client.get(f"/v1/sessions/{session_id}").json()["agent_id"]
        response = client.post(
            "/v1/scheduled-tasks",
            json={
                "name": "Queue watcher",
                "prompt": _PROMPT,
                "rrule": "FREQ=DAILY",
                "timezone": "UTC",
                "agent_id": agent_id,
                "session_mode": "continue",
                "session_id": session_id,
            },
        )
        assert response.status_code == 200, response.text
        task_id = response.json()["id"]

        first = _run_now(client, task_id)
        assert first["status"] == "succeeded", first

        def overflowed() -> bool:
            error = client.get(f"/v1/sessions/{session_id}").json().get("last_task_error")
            return bool(error) and error.get("code") == "context_length_exceeded"

        # Claude itself refuses the delivered prompt: its context is full.
        poll_until(overflowed, timeout_s=_RUN_TIMEOUT_S)
        assert _prompts_sent(client, session_id) == 1

        second = _run_now(client, task_id)
        assert (second["status"], second["error_code"]) == (
            "failed",
            "continued_session_context_full",
        ), (
            "the automation delivered another prompt into a session whose context is full: "
            f"{second}"
        )
        assert _prompts_sent(client, session_id) == 1
