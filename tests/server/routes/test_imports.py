"""Tests for importing normalized local harness sessions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from omnigent.db.utils import builtin_agent_id
from omnigent.errors import OmnigentError
from omnigent.server.routes.imports import _stream_local_sessions_from_host
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


async def test_import_session_uses_native_title_when_supplied(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A supplied harness title becomes the conversation title over the first message."""
    _seed_claude_agent(db_uri)
    payload = {
        "source": "claude",
        "external_session_id": "claude-titled-1",
        "title": "My renamed thread",
        "items": [
            {
                "type": "message",
                "response_id": "claude:turn-1",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "inspect TODO.md"}],
                },
            }
        ],
    }

    created = await client.post("/v1/imports", json=payload)

    assert created.status_code == 201
    conversation = SqlAlchemyConversationStore(db_uri).get_conversation(
        created.json()["session_id"]
    )
    assert conversation is not None
    assert conversation.title == "My renamed thread"


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


def test_imported_session_ref_allows_null_title() -> None:
    """A batch session with no synthesizable title must not fail the response.

    ``title_from_items`` returns None when there is no first user message to
    derive a title from; the /imports/local batch builds one ImportedSessionRef
    per new session, so a None title must validate instead of 500-ing the run.
    """
    from omnigent.server.routes.imports import ImportedSessionRef

    assert ImportedSessionRef(session_id="conv_x").title is None
    assert ImportedSessionRef(session_id="conv_y", title=None).title is None


async def test_stream_local_sessions_yields_each_then_stops_on_done() -> None:
    """The streaming consumer yields one session per frame, then cleans up on done.

    Fakes the tunnel by having ``send_text`` push session frames + a terminal
    ``done`` onto the per-request queue the generator just registered.
    """
    conn = SimpleNamespace(host_id="h1", pending_import_local={})
    canned = [
        {
            "external_session_id": "c1",
            "workspace": None,
            "items": [],
            "title": "one",
            "source": "claude",
            "total": 2,
        },
        {
            "external_session_id": "c2",
            "workspace": None,
            "items": [],
            "title": None,
            "source": "codex",
            "total": 2,
        },
    ]

    class _Reg:
        def send_text(self, host_conn: object, frame: str) -> None:
            (queue,) = conn.pending_import_local.values()
            for session in canned:
                queue.put_nowait(("session", session))
            queue.put_nowait(("done", {"status": "ok", "error": None}))

    got = [
        session
        async for session in _stream_local_sessions_from_host(
            host_registry=_Reg(),  # type: ignore[arg-type]
            host_conn=conn,  # type: ignore[arg-type]
            source="all",
            limit=5,
        )
    ]

    assert [s["external_session_id"] for s in got] == ["c1", "c2"]
    # The per-request queue is removed once the stream ends.
    assert conn.pending_import_local == {}


async def test_stream_local_sessions_surfaces_host_failed_count() -> None:
    """The done frame's host-side unreadable count is exposed via ``stats``.

    Sessions the host enumerated but could not read send no session frame, only
    a count on the done frame; the consumer must surface it so the route folds
    it into ``failed`` instead of the batch silently under-reporting.
    """
    conn = SimpleNamespace(host_id="h1", pending_import_local={})

    class _Reg:
        def send_text(self, host_conn: object, frame: str) -> None:
            (queue,) = conn.pending_import_local.values()
            queue.put_nowait(("done", {"status": "ok", "error": None, "failed": 3}))

    stats: dict[str, int] = {}
    got = [
        session
        async for session in _stream_local_sessions_from_host(
            host_registry=_Reg(),  # type: ignore[arg-type]
            host_conn=conn,  # type: ignore[arg-type]
            source="all",
            limit=5,
            stats=stats,
        )
    ]

    assert got == []
    assert stats["host_failed"] == 3
    assert conn.pending_import_local == {}


async def test_stream_local_sessions_raises_on_failed_done() -> None:
    """A ``done`` frame with status='failed' surfaces the host's error, not a hang."""
    conn = SimpleNamespace(host_id="h1", pending_import_local={})

    class _Reg:
        def send_text(self, host_conn: object, frame: str) -> None:
            (queue,) = conn.pending_import_local.values()
            queue.put_nowait(("done", {"status": "failed", "error": "host blew up"}))

    with pytest.raises(OmnigentError, match="host blew up"):
        _ = [
            session
            async for session in _stream_local_sessions_from_host(
                host_registry=_Reg(),  # type: ignore[arg-type]
                host_conn=conn,  # type: ignore[arg-type]
                source="claude",
                limit=5,
            )
        ]
    assert conn.pending_import_local == {}
