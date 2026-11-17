"""E2E: a real host daemon imports a whole compacted, archived Codex thread.

Fork and revert lineage, the item cap, and transcripts above 2 MiB are covered by the
loader tests in tests/test_session_import.py.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest

from tests._helpers.codex_rollout import (
    CodexRollout,
    CodexThreadRow,
    codex_message,
    write_codex_thread_store,
)
from tests._helpers.host import isolated_host

pytestmark = pytest.mark.timeout(300)

_HOST_ENV_STRIP = ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "QWEN_HOME", "PI_CODING_AGENT_DIR")
_MESSAGE_COUNT = 12
_COMPACTED_AFTER = (4, 8)


def _seed_codex_home(home: Path) -> str:
    """Write a ``~/.codex`` holding one archived thread with two compactions; return its id."""
    codex = home / ".codex"
    thread_id = str(uuid.uuid4())
    rollout = CodexRollout(thread_id, cwd="/repo", source="vscode", history_mode="paginated")
    for number in range(1, _MESSAGE_COUNT + 1):
        text = f"message {number}"
        if number % 2 == 1:
            rollout.append("turn_context", {"turn_id": f"turn_{(number + 1) // 2}"})
            rollout.append("response_item", codex_message("user", text))
        else:
            rollout.append("response_item", codex_message("assistant", text))
        if number in _COMPACTED_AFTER:
            summary = f"Summary of the first {number} messages."
            rollout.append(
                "compacted",
                {"message": summary, "replacement_history": [codex_message("user", summary)]},
            )
    # `codex archive` moves the rollout under archived_sessions/ and sets threads.archived.
    path = codex / "archived_sessions" / f"rollout-2026-10-02T11-00-00-{thread_id}.jsonl"
    rollout.write(path)
    write_codex_thread_store(
        codex, [CodexThreadRow(thread_id, "message 1", "message 1", path, archived=True)]
    )
    return thread_id


def test_import_keeps_a_compacted_archived_thread_whole(
    live_server: str, http_client: httpx.Client, tmp_path: Path
) -> None:
    """Every message and compaction baseline crosses host, tunnel and store; it stays archived."""
    thread_id = _seed_codex_home(tmp_path / "home")
    with isolated_host(tmp_path, live_server, env=dict.fromkeys(_HOST_ENV_STRIP)) as host:
        host.wait_online(http_client)
        response = http_client.post(
            "/v1/imports/local",
            json={"host_id": host.host_id, "source": "codex", "session_id": thread_id},
            timeout=120.0,
        )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["imported"] == 1, result
    session_id = result["sessions"][0]["session_id"]
    page = http_client.get(
        f"/v1/sessions/{session_id}/items", params={"limit": 100, "order": "asc"}, timeout=30
    )
    page.raise_for_status()
    items = page.json()["data"]
    texts = [
        block["text"]
        for item in items
        if item.get("type") == "message" and not item.get("is_meta")
        for block in item.get("content") or []
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    assert texts == [f"message {number}" for number in range(1, _MESSAGE_COUNT + 1)]
    # Each compaction keeps its baseline, which a cold resume rebuilds from.
    summaries = [f"Summary of the first {number} messages." for number in _COMPACTED_AFTER]
    compactions = [item for item in items if item.get("type") == "compaction"]
    assert [item["summary"] for item in compactions] == summaries
    assert [item["compacted_messages"] for item in compactions] == [
        [codex_message("user", summary)] for summary in summaries
    ]
    session = http_client.get(
        f"/v1/sessions/{session_id}",
        params={"include_items": "false", "include_liveness": "false"},
        timeout=30,
    )
    session.raise_for_status()
    assert session.json()["archived"] is True
