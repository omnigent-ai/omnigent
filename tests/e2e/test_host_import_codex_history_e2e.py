"""E2E: a real host daemon imports full Codex threads and their archive state.

Fork and revert lineage is covered by the loader tests in tests/test_session_import.py.
"""

from __future__ import annotations

import signal
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests._helpers.codex_rollout import (
    CodexRollout,
    CodexThreadRow,
    codex_message,
    write_codex_thread_store,
)
from tests.e2e.test_host_e2e import _spawn_host_daemon, _wait_for_host_online

pytestmark = pytest.mark.timeout(300)

_CWD = "/repo"
_HOST_ENV_STRIP = ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "QWEN_HOME", "PI_CODING_AGENT_DIR")
_BIG_MESSAGE_COUNT = 425


def _rollout(thread_id: str, **meta: object) -> CodexRollout:
    """A Codex Desktop rollout for ``thread_id``."""
    return CodexRollout(thread_id, cwd=_CWD, source="vscode", history_mode="paginated", **meta)


@dataclass(frozen=True)
class _Seeded:
    compacted: str
    archived: str


def _seed_codex_home(home: Path) -> _Seeded:
    """Write a ``~/.codex`` holding a large compacted thread and an archived one."""
    codex = home / ".codex"
    sessions = codex / "sessions" / "2026" / "10" / "02"
    rows: list[CodexThreadRow] = []

    def name(thread_id: str, stamp: str) -> str:
        return f"rollout-2026-10-02T{stamp}-{thread_id}.jsonl"

    # Above the 2 MiB threshold, with compactions after messages 100, 200 and 310.
    compacted = str(uuid.uuid4())
    big = _rollout(compacted)
    padding = " lorem" * 1900
    for number in range(1, _BIG_MESSAGE_COUNT + 1):
        text = f"compacted thread message {number}{padding}"
        if number % 2 == 1:
            big.append("turn_context", {"turn_id": f"turn_{(number + 1) // 2}"})
            big.append("response_item", codex_message("user", text))
        else:
            big.append("response_item", codex_message("assistant", text))
        if number in (100, 200, 310):
            summary = f"Summary of the first {number} messages."
            big.append(
                "compacted",
                {"message": summary, "replacement_history": [codex_message("user", summary)]},
            )
    big_path = sessions / name(compacted, "11-00-00")
    big.write(big_path)
    title = "compacted thread message 1"
    rows.append(CodexThreadRow(compacted, title, title, big_path))

    # `codex archive` moves the rollout under archived_sessions/ and sets threads.archived.
    archived = str(uuid.uuid4())
    archived_rollout = _rollout(archived)
    archived_rollout.turn(1, "archived thread question", "archived thread answer")
    archived_path = codex / "archived_sessions" / name(archived, "12-00-00")
    archived_rollout.write(archived_path)
    title = "archived thread question"
    rows.append(CodexThreadRow(archived, title, title, archived_path, archived=True))

    write_codex_thread_store(codex, rows)
    return _Seeded(compacted, archived)


@dataclass(frozen=True)
class _ImportHost:
    host_id: str
    seeded: _Seeded


@pytest.fixture(scope="module")
def import_host(
    live_server: str,
    http_client: httpx.Client,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_ImportHost]:
    """A real host daemon whose ``HOME`` holds the seeded Codex threads."""
    home = tmp_path_factory.mktemp("codex-history-host")
    seeded = _seed_codex_home(home)
    with pytest.MonkeyPatch.context() as patch:
        for name in _HOST_ENV_STRIP:
            patch.delenv(name, raising=False)
        daemon = _spawn_host_daemon(
            tmp_path=home, live_server=live_server, mock_llm_server_url=mock_llm_server_url
        )
    try:
        _wait_for_host_online(http_client, daemon.host_id)
        yield _ImportHost(daemon.host_id, seeded)
    finally:
        daemon.proc.send_signal(signal.SIGTERM)
        try:
            daemon.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.proc.kill()
            daemon.proc.wait(timeout=5)


def _import(http_client: httpx.Client, host: _ImportHost, thread_id: str) -> str:
    """Import one Codex thread through the host; return the new session id."""
    response = http_client.post(
        "/v1/imports/local",
        json={"host_id": host.host_id, "source": "codex", "session_id": thread_id},
        timeout=120.0,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["imported"] == 1, result
    return result["sessions"][0]["session_id"]


def _items(http_client: httpx.Client, session_id: str) -> list[dict[str, object]]:
    """Every persisted item of a session, oldest first."""
    items: list[dict[str, object]] = []
    after: str | None = None
    while True:
        params: dict[str, object] = {"limit": 1000, "order": "asc"}
        if after is not None:
            params["after"] = after
        response = http_client.get(f"/v1/sessions/{session_id}/items", params=params, timeout=30)
        response.raise_for_status()
        body = response.json()
        items.extend(body["data"])
        if not body.get("has_more") or not body["data"]:
            return items
        after = body["data"][-1]["id"]


def _message_texts(items: list[dict[str, object]]) -> list[str]:
    """Text of every visible message item."""
    return [
        block["text"]
        for item in items
        if item.get("type") == "message" and not item.get("is_meta")
        for block in item.get("content") or []
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]


def test_import_keeps_every_message_of_a_compacted_thread(
    http_client: httpx.Client, import_host: _ImportHost
) -> None:
    """A transcript above 2 MiB with several compactions imports every visible message."""
    session_id = _import(http_client, import_host, import_host.seeded.compacted)

    items = _items(http_client, session_id)
    texts = _message_texts(items)
    assert len(texts) == _BIG_MESSAGE_COUNT, f"imported {len(texts)} of {_BIG_MESSAGE_COUNT}"
    assert texts[0].startswith("compacted thread message 1 ")
    # Each compaction keeps its baseline, which a cold resume rebuilds from.
    compactions = [item for item in items if item.get("type") == "compaction"]
    summaries = [f"Summary of the first {number} messages." for number in (100, 200, 310)]
    assert [item["summary"] for item in compactions] == summaries
    assert [item["compacted_messages"] for item in compactions] == [
        [codex_message("user", summary)] for summary in summaries
    ]


def test_import_keeps_a_codex_archived_thread_archived(
    http_client: httpx.Client, import_host: _ImportHost
) -> None:
    """A thread archived in Codex is imported as an archived session."""
    session_id = _import(http_client, import_host, import_host.seeded.archived)

    response = http_client.get(
        f"/v1/sessions/{session_id}",
        params={"include_items": "false", "include_liveness": "false"},
        timeout=30,
    )
    response.raise_for_status()
    assert response.json()["archived"] is True
