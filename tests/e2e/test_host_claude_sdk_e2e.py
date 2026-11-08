"""Host end-to-end tests for claude-sdk sessions, on a real Claude login.

These need a real logged-in ``claude`` CLI (the SDK harness drives it) but none
of claude-native's terminal prerequisites, so they have their own opt-in.
Shared host and session helpers live in ``test_host_claude_native_fork_e2e``::

    OMNIGENT_E2E_CLAUDE_SDK=1 \\
    .venv/bin/python -m pytest tests/e2e/test_host_claude_sdk_e2e.py \\
        --llm-api-key "mock-key" -v
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import httpx
import pytest

from tests.e2e.test_host_claude_native_e2e import (
    _assistant_text,
    _online_host_id,
    _poll_for_assistant_marker,
)
from tests.e2e.test_host_claude_native_fork_e2e import (
    _builtin_agent_id,
    _host_daemon,
    _send_user_message,
    _wait_until_idle,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CLAUDE_SDK") != "1" or shutil.which("claude") is None,
    reason=(
        "claude-sdk host e2e needs a logged-in Claude CLI; set "
        "OMNIGENT_E2E_CLAUDE_SDK=1 (and have `claude` installed + logged in) to run"
    ),
)


def _create_sdk_session_on_host(client: httpx.Client, *, host_id: str, workspace: Path) -> str:
    """Create a ``sdk-chat-builtin`` claude-sdk session on *host_id*.

    The built-in pins a dated model; the ``sonnet`` alias resolves on any
    Claude login.
    """
    created = client.post(
        "/v1/sessions",
        json={
            "agent_id": _builtin_agent_id(client, "sdk-chat-builtin"),
            "host_id": host_id,
            "workspace": str(workspace),
            "model_override": "sonnet",
        },
        timeout=60.0,
    )
    created.raise_for_status()
    return str(created.json()["id"])


def test_clear_claude_sdk_continues_empty_on_the_same_host(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    claude-sdk ``/clear`` continues in an empty session on the same host and runner.

    The SDK replays the Omnigent transcript into a fresh client, so a cleared
    session that still recalled the planted word would mean the history came
    along. The original keeps a notice linking to the new session, and
    resuming it still recalls the word.
    """
    workspace = tmp_path / "clear_sdk_ws"
    workspace.mkdir()
    word = f"CLEARWORD_{uuid.uuid4().hex[:6].upper()}"

    with _host_daemon(tmp_path, live_server):
        host_id = _online_host_id(http_client, timeout=30.0)
        source_id = _create_sdk_session_on_host(http_client, host_id=host_id, workspace=workspace)
        _send_user_message(
            http_client,
            session_id=source_id,
            text=f"Remember this code word for later: {word}. Reply with exactly: ACK0",
        )
        _poll_for_assistant_marker(http_client, session_id=source_id, marker="ACK0", timeout=180.0)
        # /clear refuses a running turn.
        _wait_until_idle(http_client, session_id=source_id)
        # /clear moves the runner to the new session, so capture it first.
        placed = http_client.get(f"/v1/sessions/{source_id}", timeout=30.0).json()
        assert placed["runner_id"] is not None, placed

        cleared = http_client.post(
            f"/v1/sessions/{source_id}/events", json={"type": "clear", "data": {}}, timeout=60.0
        )
        assert cleared.status_code == 202, cleared.text
        cleared_id = str(cleared.json()["session_id"])
        new = http_client.get(f"/v1/sessions/{cleared_id}", timeout=30.0).json()
        assert (new["runner_id"], new["host_id"], new["workspace"], new["model_override"]) == (
            placed["runner_id"],
            placed["host_id"],
            placed["workspace"],
            "sonnet",
        )
        # The original keeps its host and workspace to resume on, but not the runner.
        source = http_client.get(f"/v1/sessions/{source_id}", timeout=30.0).json()
        assert (source["runner_id"], source["host_id"], source["workspace"]) == (
            None,
            placed["host_id"],
            placed["workspace"],
        )
        items = http_client.get(
            f"/v1/sessions/{source_id}/items", params={"limit": 5, "order": "desc"}, timeout=30.0
        ).json()["data"]
        assert any(f"/c/{cleared_id}" in _assistant_text(item) for item in items), items

        _send_user_message(
            http_client,
            session_id=cleared_id,
            text=(
                "Reply with the code word I gave you earlier, or exactly NOCODEWORD if I "
                "have not given you one."
            ),
        )
        reply = _poll_for_assistant_marker(
            http_client, session_id=cleared_id, marker="NOCODEWORD", timeout=180.0
        )
        assert word not in reply, f"cleared claude-sdk session recalled {word!r}: {reply!r}"

        # The original can still be resumed, with its earlier context intact.
        _send_user_message(
            http_client,
            session_id=source_id,
            text="Reply with exactly the code word I gave you earlier, and nothing else.",
        )
        recalled = _poll_for_assistant_marker(
            http_client, session_id=source_id, marker=word, timeout=240.0
        )
        assert word in recalled, recalled
