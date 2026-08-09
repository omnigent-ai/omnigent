"""Tests for importing normalized local harness sessions."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from omnigent.db.utils import builtin_agent_id
from omnigent.server.routes import imports as imports_routes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def _seed_claude_agent(db_uri: str) -> str:
    """Seed the built-in agent because focused app tests skip lifespan startup."""
    agent_id = builtin_agent_id("claude-native-ui")
    SqlAlchemyAgentStore(db_uri).create(
        agent_id,
        name="claude-native-ui",
        bundle_location="builtin://claude-native-ui",
    )
    return agent_id


@pytest.fixture()
def claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point local Claude discovery at a throwaway home, never the developer's."""
    home = tmp_path / "claude-home"
    (home / "projects" / "-repo").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


def _write_claude_transcript(
    home: Path,
    session_id: str,
    *,
    text: str,
    workspace: str = "/repo",
    modified_at: int | None = None,
) -> Path:
    """Write one minimal parent transcript under a fake Claude home."""
    transcript = home / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": f"user-{session_id}",
                "cwd": workspace,
                "message": {"role": "user", "content": text},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if modified_at is not None:
        os.utime(transcript, (modified_at, modified_at))
    return transcript


async def test_import_session_creates_normal_session_and_blocks_duplicate(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """An import creates one native session and a retry is rejected."""
    agent_id = _seed_claude_agent(db_uri)
    payload = {
        "source": "claude",
        "external_session_id": "claude-session-1",
        "workspace": "/repo",
        "items": [
            {
                "type": "message",
                "response_id": "claude:turn-1",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "inspect TODO.md"}],
                },
            },
            {
                "type": "message",
                "response_id": "claude:turn-1",
                "data": {
                    "role": "assistant",
                    "agent": "claude-native-ui",
                    "content": [{"type": "output_text", "text": "Done."}],
                },
            },
        ],
    }

    created = await client.post("/v1/imports", json=payload)
    repeated = await client.post("/v1/imports", json=payload)

    assert created.status_code == 201
    assert created.json()["status"] == "imported"
    assert repeated.status_code == 409
    assert created.json()["session_id"] in repeated.text
    assert "already been imported" in repeated.text

    session_id = created.json()["session_id"]
    conversation = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conversation is not None
    assert conversation.agent_id == agent_id
    assert conversation.external_session_id == "claude-session-1"
    assert conversation.workspace == "/repo"
    assert conversation.title == "inspect TODO.md"
    assert conversation.labels["omnigent.wrapper"] == "claude-code-native-ui"
    items = await client.get(f"/v1/sessions/{session_id}/items")
    assert items.status_code == 200
    assert [item["type"] for item in items.json()["data"]] == ["message", "message"]


async def test_concurrent_identical_imports_return_one_session(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Concurrent retries serialize on source identity and one is rejected."""
    _seed_claude_agent(db_uri)
    payload = {
        "source": "claude",
        "external_session_id": "claude-concurrent-1",
        "items": [
            {
                "type": "message",
                "response_id": "claude:turn-1",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            }
        ],
    }

    first, second = await asyncio.gather(
        client.post("/v1/imports", json=payload),
        client.post("/v1/imports", json=payload),
    )

    assert {first.status_code, second.status_code} == {201, 409}
    imported = SqlAlchemyConversationStore(db_uri).find_imported_conversation(
        "claude", "claude-concurrent-1"
    )
    assert imported is not None


async def test_force_import_replaces_existing_session(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A forced retry replaces the transcript while retaining its stable id."""
    _seed_claude_agent(db_uri)
    payload = {
        "source": "claude",
        "external_session_id": "claude-force-1",
        "workspace": "/repo/old",
        "items": [
            {
                "type": "message",
                "response_id": "claude:old",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "old prompt"}],
                },
            }
        ],
    }
    created = await client.post("/v1/imports", json=payload)
    payload["force"] = True
    payload["workspace"] = "/repo/new"
    payload["items"] = [
        {
            "type": "message",
            "response_id": "claude:new",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "new prompt"}],
            },
        }
    ]

    replaced = await client.post("/v1/imports", json=payload)

    assert created.status_code == 201
    assert replaced.status_code == 201
    assert replaced.json()["session_id"] == created.json()["session_id"]
    conversation = SqlAlchemyConversationStore(db_uri).get_conversation(
        replaced.json()["session_id"]
    )
    assert conversation is not None
    assert conversation.workspace == "/repo/new"
    assert conversation.title == "new prompt"
    items = await client.get(f"/v1/sessions/{conversation.id}/items")
    assert items.status_code == 200
    assert [item["content"][0]["text"] for item in items.json()["data"]] == ["new prompt"]


async def test_import_session_rejects_empty_history(client: httpx.AsyncClient) -> None:
    """An empty parser result cannot create a permanently claimed session."""
    response = await client.post(
        "/v1/imports",
        json={
            "source": "codex",
            "external_session_id": "empty-codex-session",
            "items": [],
        },
    )

    assert response.status_code == 422


async def test_list_local_sessions_describes_recent_transcripts(
    client: httpx.AsyncClient,
    claude_home: Path,
) -> None:
    """The picker list carries the title, workspace, and size of each chat."""
    _write_claude_transcript(claude_home, "older-session", text="old prompt", modified_at=1000)
    _write_claude_transcript(
        claude_home,
        "newer-session",
        text="inspect TODO.md",
        workspace="/repo/web",
        modified_at=2000,
    )

    response = await client.get("/v1/imports/local/sessions?source=claude")

    assert response.status_code == 200
    sessions = response.json()["sessions"]
    assert [session["session_id"] for session in sessions] == [
        "newer-session",
        "older-session",
    ]
    assert sessions[0] == {
        "session_id": "newer-session",
        "title": "inspect TODO.md",
        "workspace": "/repo/web",
        "item_count": 1,
        "modified_at": 2000,
    }


async def test_list_local_sessions_applies_limit_and_rejects_unknown_source(
    client: httpx.AsyncClient,
    claude_home: Path,
) -> None:
    """The list is bounded by `limit` and only serves supported harnesses."""
    for index, session_id in enumerate(("first", "second", "third")):
        _write_claude_transcript(
            claude_home, session_id, text=session_id, modified_at=1000 + index
        )

    limited = await client.get("/v1/imports/local/sessions?source=claude&limit=2")
    unknown = await client.get("/v1/imports/local/sessions?source=cursor")

    assert limited.status_code == 200
    assert [session["session_id"] for session in limited.json()["sessions"]] == [
        "third",
        "second",
    ]
    assert unknown.status_code == 422


async def test_import_local_session_reads_the_transcript_off_disk(
    client: httpx.AsyncClient,
    db_uri: str,
    claude_home: Path,
) -> None:
    """Importing by id parses the local transcript server-side."""
    agent_id = _seed_claude_agent(db_uri)
    _write_claude_transcript(claude_home, "local-session-1", text="inspect TODO.md")

    created = await client.post(
        "/v1/imports/local",
        json={"source": "claude", "external_session_id": "local-session-1"},
    )

    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "imported"
    assert body["item_count"] == 1
    conversation = SqlAlchemyConversationStore(db_uri).get_conversation(body["session_id"])
    assert conversation is not None
    assert conversation.agent_id == agent_id
    assert conversation.external_session_id == "local-session-1"
    assert conversation.workspace == "/repo"
    assert conversation.title == "inspect TODO.md"
    items = await client.get(f"/v1/sessions/{body['session_id']}/items")
    assert items.status_code == 200
    assert [item["content"][0]["text"] for item in items.json()["data"]] == ["inspect TODO.md"]


async def test_import_local_session_reports_a_missing_transcript(
    client: httpx.AsyncClient,
    db_uri: str,
    claude_home: Path,
) -> None:
    """An id with no transcript on disk is a 404, not a blank session."""
    _seed_claude_agent(db_uri)

    response = await client.post(
        "/v1/imports/local",
        json={"source": "claude", "external_session_id": "never-existed"},
    )

    assert response.status_code == 404
    assert "was not found" in response.text


async def test_import_local_session_blocks_duplicates_until_forced(
    client: httpx.AsyncClient,
    db_uri: str,
    claude_home: Path,
) -> None:
    """A second import is rejected, and forcing it replaces the transcript."""
    _seed_claude_agent(db_uri)
    _write_claude_transcript(claude_home, "local-session-2", text="old prompt")

    created = await client.post(
        "/v1/imports/local",
        json={"source": "claude", "external_session_id": "local-session-2"},
    )
    repeated = await client.post(
        "/v1/imports/local",
        json={"source": "claude", "external_session_id": "local-session-2"},
    )
    _write_claude_transcript(
        claude_home, "local-session-2", text="new prompt", workspace="/repo/new"
    )
    replaced = await client.post(
        "/v1/imports/local",
        json={"source": "claude", "external_session_id": "local-session-2", "force": True},
    )

    assert created.status_code == 201
    assert repeated.status_code == 409
    assert "already been imported" in repeated.text
    assert replaced.status_code == 201
    assert replaced.json()["session_id"] == created.json()["session_id"]
    conversation = SqlAlchemyConversationStore(db_uri).get_conversation(
        replaced.json()["session_id"]
    )
    assert conversation is not None
    assert conversation.workspace == "/repo/new"
    assert conversation.title == "new prompt"


async def test_local_import_routes_refuse_a_shared_server(
    client: httpx.AsyncClient,
    db_uri: str,
    claude_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a multi-user server the disk belongs to the operator, not the caller."""
    _seed_claude_agent(db_uri)
    _write_claude_transcript(claude_home, "local-session-3", text="inspect TODO.md")
    monkeypatch.setattr(imports_routes, "local_single_user_enabled", lambda: False)

    listed = await client.get("/v1/imports/local/sessions?source=claude")
    imported = await client.post(
        "/v1/imports/local",
        json={"source": "claude", "external_session_id": "local-session-3"},
    )

    assert listed.status_code == 403
    assert imported.status_code == 403
    assert (
        SqlAlchemyConversationStore(db_uri).find_imported_conversation("claude", "local-session-3")
        is None
    )
