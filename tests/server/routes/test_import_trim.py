"""Server-side stream import with trim and budget: cap items, notice, event stream."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from omnigent.entities import ErrorData, MessageData, NewConversationItem
from omnigent.session_import import local as local_module
from omnigent.session_import.local import IMPORT_TRIMMED_NOTICE_CODE, load_local_session
from tests.server.import_tunnel_harness import (
    HOST_ID,
    FakeConversationStore,
    TunnelPair,
    client,
    host_record,
    imports_app,
    ndjson,
    serve_local_sessions,
)

_CLAUDE_ID = "a1b2c3d4-1234-5678-9abc-def012345670"


def _claude_record(record: dict[str, Any], session_id: str, n: int) -> dict[str, Any]:
    return {
        "uuid": str(UUID(int=n + 1)),
        "parentUuid": str(UUID(int=n)) if n else None,
        "sessionId": session_id,
        "cwd": "/repo",
        "timestamp": "2026-10-01T00:00:00.000Z",
        "isSidechain": False,
        "userType": "external",
        "version": "2.1.0",
        **record,
    }


def write_claude_transcript(
    home: Path, session_id: str, *, turns: int, title: str | None = None
) -> Path:
    """A Claude Code transcript of ``turns`` 4-item turns (prompt "turn N")."""
    records: list[dict[str, Any]] = []
    for n in range(turns):
        records.append({"type": "user", "message": {"role": "user", "content": f"turn {n}"}})
        records.append(
            {
                "type": "assistant",
                "message": {
                    "id": f"msg_{n}",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [
                        {"type": "text", "text": f"ok {n}"},
                        {
                            "type": "tool_use",
                            "id": f"toolu_{n}",
                            "name": "Bash",
                            "input": {"command": "true"},
                        },
                    ],
                },
            }
        )
        records.append(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": f"toolu_{n}", "content": f"out {n}"}
                    ],
                },
            }
        )
    lines = [json.dumps(_claude_record(r, session_id, i)) for i, r in enumerate(records)]
    if title is not None:
        lines.append(json.dumps({"type": "ai-title", "aiTitle": title, "sessionId": session_id}))
    path = home / ".claude" / "projects" / "-repo" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _err(item: NewConversationItem) -> ErrorData:
    assert isinstance(item.data, ErrorData), item
    return item.data


def _text(item: NewConversationItem) -> str:
    assert isinstance(item.data, MessageData)
    return str(item.data.content[0]["text"])


@pytest.fixture
def stream_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME with small import cap for stream tests."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(local_module, "IMPORT_MAX_ITEMS", 10)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    return tmp_path


class TestStreamTrim:
    async def _stream(
        self, monkeypatch: pytest.MonkeyPatch, sessions: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], FakeConversationStore]:
        store = FakeConversationStore()
        pair = TunnelPair()
        app = imports_app(store, host_registry=pair.registry, host=host_record())
        # Mock the session loader to return the provided sessions (loaded from file system)
        serve_local_sessions(monkeypatch, sessions)
        async with pair:
            async with client(app) as http:
                response = await http.post(
                    "/v1/imports/local/stream",
                    json={
                        "host_id": f"host_{HOST_ID}",
                        "source": "claude",
                        "session_id": _CLAUDE_ID,
                    },
                )
        assert response.status_code == 200, response.text
        events = ndjson(response)
        assert events[-1]["event"] == "done", events
        assert (events[-1]["imported"], events[-1]["failed"]) == (1, 0), events
        return events, store

    @pytest.mark.asyncio
    async def test_host_stream_imports_the_trimmed_history(
        self, stream_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_claude_transcript(stream_home, _CLAUDE_ID, turns=10)
        monkeypatch.setattr(local_module, "IMPORT_MAX_ITEMS", 12)
        # Load the session from the file system now that it's been written
        session = load_local_session("claude", _CLAUDE_ID)
        events, store = await self._stream(monkeypatch, {_CLAUDE_ID: session})
        session_event = next(e for e in events if e.get("event") == "session")
        assert session_event["title"] == "turn 0"
        (items,) = store.items.values()
        assert len(items) == 12
        assert _text(items[0]) == "turn 0"
        assert _err(items[-1]).code == IMPORT_TRIMMED_NOTICE_CODE
        # Cut after a tool call whose output was left out; it is still stored.
        assert items[-2].type == "function_call"
        (conversation,) = store.conversations.values()
        assert conversation.title == "turn 0"

    @pytest.mark.asyncio
    async def test_host_stream_imports_a_budgeted_read(
        self, stream_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = write_claude_transcript(stream_home, _CLAUDE_ID, turns=40)
        monkeypatch.setattr(local_module, "IMPORT_READ_BUDGET_BYTES", path.stat().st_size // 4)
        monkeypatch.setattr(local_module, "IMPORT_MAX_ITEMS", 1000)
        # Load the session from the file system now that it's been written
        session = load_local_session("claude", _CLAUDE_ID)
        events, store = await self._stream(monkeypatch, {_CLAUDE_ID: session})
        (items,) = store.items.values()
        assert _err(items[-1]).message.endswith("later history was left out.")
        assert next(e for e in events if e.get("event") == "session")["title"] == "turn 0"
