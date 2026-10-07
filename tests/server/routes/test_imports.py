"""Tests for importing normalized local harness sessions."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.db.utils import builtin_agent_id
from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.imports import (
    LocalImportRequest,
    _stream_local_sessions_from_host,
    create_imports_router,
)
from omnigent.session_import import IMPORT_SOURCE_LABEL_KEY
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


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
    assert "already exists" in repeated.text

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


async def test_import_dedupes_against_native_session(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Importing a session already run natively dedupes against the native row.

    A native run records the harness session id on the metadata column but
    carries no import-provenance labels. Dedup keys off that shared column, so
    re-importing the same transcript must find the native session, not spawn a
    duplicate.
    """
    agent_id = _seed_claude_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    native = store.create_conversation(agent_id=agent_id, title="closing tickets")
    store.set_external_session_id(native.id, "9d74df82-native")

    resp = await client.post(
        "/v1/imports",
        json={
            "source": "claude",
            "external_session_id": "9d74df82-native",
            "items": [
                {
                    "type": "message",
                    "response_id": "claude:turn-1",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hi"}],
                    },
                }
            ],
        },
    )

    assert resp.status_code == 409
    assert native.id in resp.text
    # Still exactly one row for the id: no duplicate was created.
    found = store.find_conversation_by_external_session_id("9d74df82-native")
    assert found is not None
    assert found.id == native.id


async def test_import_binds_session_to_supplied_host(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A CLI ``host_id`` binds the imported session to the origin machine.

    The transcript's workspace lives on that host, so resume defaults there.
    ``_persist_import`` binds only alongside a workspace (the check constraint).
    """
    _seed_claude_agent(db_uri)
    payload = {
        "source": "claude",
        "external_session_id": "claude-host-1",
        "workspace": "/repo",
        "host_id": "a1b2c3d4e5f67890abcdef1234567890",
        "items": [
            {
                "type": "message",
                "response_id": "claude:turn-1",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "bind me"}],
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
    assert conversation.host_id == "a1b2c3d4e5f67890abcdef1234567890"
    assert conversation.workspace == "/repo"


async def test_import_host_id_without_workspace_stays_unbound(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """No workspace means no host bind (the check constraint forbids it)."""
    _seed_claude_agent(db_uri)
    payload = {
        "source": "claude",
        "external_session_id": "claude-host-2",
        "host_id": "a1b2c3d4e5f67890abcdef1234567890",
        "items": [
            {
                "type": "message",
                "response_id": "claude:turn-1",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "no workspace"}],
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
    assert conversation.host_id is None


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
    imported = SqlAlchemyConversationStore(db_uri).find_conversation_by_external_session_id(
        "claude-concurrent-1"
    )
    assert imported is not None


async def test_force_import_replaces_existing_session(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A forced retry recreates the import with the requested metadata and stable id."""
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


@pytest.mark.parametrize("scenario", ["running", "launching"])
async def test_force_import_rejects_active_session(
    client: httpx.AsyncClient,
    db_uri: str,
    scenario: str,
) -> None:
    """Replacement must not delete a conversation with a live or launching runner."""
    from omnigent.server.routes._sessions.common import _session_status_cache

    _seed_claude_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    payload = {
        "source": "claude",
        "external_session_id": "claude-force-active",
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
    assert created.status_code == 201
    session_id = created.json()["session_id"]
    if scenario == "running":
        store.set_session_live_status(session_id, "running")
    else:
        # ``launching`` is a transient relay/cache status and is not persisted
        # by the live-status codec, but it still means the runner is starting.
        _session_status_cache[session_id] = "launching"

    payload["force"] = True
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
    try:
        replaced = await client.post("/v1/imports", json=payload)
    finally:
        _session_status_cache.pop(session_id, None)

    assert replaced.status_code == 409
    existing = store.get_conversation(session_id)
    assert existing is not None
    assert existing.live_status == ("running" if scenario == "running" else None)
    assert [item.data.content[0]["text"] for item in store.list_items(session_id).data] == [
        "old prompt"
    ]


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


def test_exact_local_import_requires_one_harness_and_trims_id() -> None:
    """An exact id is normalized and cannot be paired with the all selector."""
    request = LocalImportRequest(host_id="h1", source="claude", session_id="  exact-id  ")
    assert request.session_id == "exact-id"

    with pytest.raises(ValueError, match="requires a specific harness"):
        LocalImportRequest(host_id="h1", source="all", session_id="exact-id")

    with pytest.raises(ValueError, match="requires an exact session id"):
        LocalImportRequest(host_id="h1", source="claude", force=True)


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
            from omnigent.host.frames import HostImportLocalFrame, decode_host_frame

            decoded = decode_host_frame(frame)
            assert isinstance(decoded, HostImportLocalFrame)
            assert decoded.allow_session_chunks is True
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


async def test_stream_local_sessions_treats_chunk_progress_as_liveness() -> None:
    """Chunk heartbeats reset the wait without yielding malformed sessions."""
    conn = SimpleNamespace(host_id="h1", pending_import_local={})

    class _Reg:
        def send_text(self, host_conn: object, frame: str) -> None:
            (queue,) = conn.pending_import_local.values()
            queue.put_nowait(("progress", {}))
            queue.put_nowait(("done", {"status": "ok", "error": None}))

    got = [
        session
        async for session in _stream_local_sessions_from_host(
            host_registry=_Reg(),  # type: ignore[arg-type]
            host_conn=conn,  # type: ignore[arg-type]
            source="claude",
            limit=1,
        )
    ]

    assert got == []
    assert conn.pending_import_local == {}


async def test_stream_local_sessions_sends_exact_session_id() -> None:
    """The server carries an exact id through the host tunnel request."""
    from omnigent.host.frames import HostImportLocalByIdFrame, decode_host_frame

    conn = SimpleNamespace(host_id="h1", pending_import_local={})
    sent: list[HostImportLocalByIdFrame] = []

    class _Reg:
        def send_text(self, host_conn: object, frame: str) -> None:
            decoded = decode_host_frame(frame)
            assert isinstance(decoded, HostImportLocalByIdFrame)
            sent.append(decoded)
            (queue,) = conn.pending_import_local.values()
            queue.put_nowait(("done", {"status": "ok", "error": None}))

    got = [
        session
        async for session in _stream_local_sessions_from_host(
            host_registry=_Reg(),  # type: ignore[arg-type]
            host_conn=conn,  # type: ignore[arg-type]
            source="codex",
            limit=10,
            session_id="session-exact",
        )
    ]

    assert got == []
    assert sent[0].source == "codex"
    assert sent[0].session_id == "session-exact"
    assert sent[0].allow_session_chunks is True


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


async def test_local_import_binds_session_to_importing_host(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host-mediated batch import binds each read session to that host.

    The transcript (and its recorded workspace) live on the importing host, so
    that host is the natural place to resume. A session whose transcript had no
    cwd stays unbound: the workspace-required check constraint forbids a host
    without one.
    """
    from fastapi import FastAPI

    from omnigent.server.routes import imports as imports_module

    _seed_claude_agent(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)

    async def _fake_stream(**_kwargs: object):
        yield {
            "external_session_id": "claude-with-cwd",
            "workspace": "/repo/on/host",
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
            "title": "Bound thread",
            "source": "claude",
        }
        yield {
            "external_session_id": "claude-no-cwd",
            "workspace": None,
            "items": [
                {
                    "type": "message",
                    "response_id": "claude:turn-1",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "no workspace here"}],
                    },
                }
            ],
            "title": "Unbound thread",
            "source": "claude",
        }

    monkeypatch.setattr(imports_module, "_stream_local_sessions_from_host", _fake_stream)

    host_conn = SimpleNamespace(
        host_id="host_0123456789abcdef0123456789abcdef", pending_import_local={}
    )
    host_registry = SimpleNamespace(get=lambda host_id: host_conn)
    host_store = SimpleNamespace(get_host=lambda host_id: SimpleNamespace(user_id=None))

    app = FastAPI()
    app.include_router(
        imports_module.create_imports_router(
            conversation_store,
            SqlAlchemyAgentStore(db_uri),
            host_registry=host_registry,  # type: ignore[arg-type]
            host_store=host_store,  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/v1/imports/local",
            json={
                "host_id": "host_0123456789abcdef0123456789abcdef",
                "source": "claude",
                "limit": 5,
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["imported"] == 2
    by_title = {ref["title"]: ref["session_id"] for ref in body["sessions"]}

    bound = conversation_store.get_conversation(by_title["Bound thread"])
    assert bound is not None
    # The store canonicalizes host_id to bare 32-hex (the "host_" prefix is
    # stripped on read), matching every other host-bound conversation.
    assert bound.host_id == "0123456789abcdef0123456789abcdef"
    assert bound.workspace == "/repo/on/host"

    unbound = conversation_store.get_conversation(by_title["Unbound thread"])
    assert unbound is not None
    assert unbound.host_id is None
    assert unbound.workspace is None


@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "stream"])
async def test_local_import_force_replaces_exact_snapshot(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """Exact local replacement keeps the stable id in both response modes."""
    from fastapi import FastAPI

    from omnigent.server.routes import imports as imports_module

    agent_id = _seed_claude_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    external_id = "claude-local-force"
    from omnigent.server.routes.imports import _import_conversation_id

    existing = store.create_conversation(
        agent_id=agent_id,
        title="Old snapshot",
        conversation_id=_import_conversation_id("claude", external_id),
    )
    store.set_external_session_id(existing.id, external_id)
    # A resumed import may retain a runner binding after that runner has died;
    # cleared liveness makes the safe replacement path explicit.
    store.replace_runner_id(existing.id, "runner_dead")
    store.clear_runner_liveness("runner_dead")

    async def _fake_stream(**_kwargs: object):
        yield {
            "external_session_id": external_id,
            "workspace": None,
            "items": [
                {
                    "type": "message",
                    "response_id": "claude:new",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "latest prompt"}],
                    },
                }
            ],
            "title": "Latest snapshot",
            "source": "claude",
        }

    monkeypatch.setattr(imports_module, "_stream_local_sessions_from_host", _fake_stream)
    host_conn = SimpleNamespace(
        host_id="host_0123456789abcdef0123456789abcdef", pending_import_local={}
    )
    app = FastAPI()
    app.include_router(
        imports_module.create_imports_router(
            store,
            SqlAlchemyAgentStore(db_uri),
            host_registry=SimpleNamespace(get=lambda _host_id: host_conn),  # type: ignore[arg-type]
            host_store=SimpleNamespace(  # type: ignore[arg-type]
                get_host=lambda _host_id: SimpleNamespace(user_id=None)
            ),
        ),
        prefix="/v1",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        deduped = await client.post(
            f"/v1/imports/local{'/stream' if stream else ''}",
            json={
                "host_id": host_conn.host_id,
                "source": "claude",
                "session_id": external_id,
            },
        )
        assert deduped.status_code == 200
        if stream:
            deduped_events = [
                json.loads(line) for line in deduped.text.splitlines() if line.strip()
            ]
            assert not [event for event in deduped_events if event["event"] == "session"]
            assert deduped_events[-1]["already_imported"] == 1
        else:
            assert deduped.json()["already_imported"] == 1

        response = await client.post(
            f"/v1/imports/local{'/stream' if stream else ''}",
            json={
                "host_id": host_conn.host_id,
                "source": "claude",
                "session_id": external_id,
                "force": True,
            },
        )

    assert response.status_code == 200
    if stream:
        events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        session_id = next(event["session_id"] for event in events if event["event"] == "session")
        assert events[-1]["already_imported"] == 0
    else:
        body = response.json()
        session_id = body["sessions"][0]["session_id"]
        assert body["already_imported"] == 0
    assert session_id == existing.id

    replaced = store.get_conversation(existing.id)
    assert replaced is not None
    assert replaced.title == "Old snapshot"
    assert [item.data.content[0]["text"] for item in store.list_items(existing.id).data] == [
        "latest prompt"
    ]


@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "stream"])
async def test_local_exact_import_rejects_mismatched_host_identity(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """An exact request never looks up or replaces a host-returned mismatch."""
    from fastapi import FastAPI

    from omnigent.server.routes import imports as imports_module
    from omnigent.server.routes.imports import _import_conversation_id

    agent_id = _seed_claude_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    requested_id = "claude-local-exact"
    other_id = "claude-local-other"
    for external_id, text in ((requested_id, "requested old"), (other_id, "other old")):
        conversation = store.create_conversation(
            agent_id=agent_id,
            title=text,
            conversation_id=_import_conversation_id("claude", external_id),
        )
        store.set_external_session_id(conversation.id, external_id)
        store.append(
            conversation.id,
            [
                NewConversationItem(
                    type="message",
                    response_id="old",
                    data=MessageData(
                        role="user",
                        content=[{"type": "input_text", "text": text}],
                    ),
                )
            ],
        )

    async def _fake_stream(**_kwargs: object):
        # Wrong external id, then wrong harness with the requested id. Both
        # payloads contain valid replacement items, so lookup ordering is the
        # only thing preventing a mutation.
        for external_id, source in ((other_id, "claude"), (requested_id, "codex")):
            yield {
                "external_session_id": external_id,
                "workspace": None,
                "items": [
                    {
                        "type": "message",
                        "response_id": "new",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "must not land"}],
                        },
                    }
                ],
                "title": "Mismatch",
                "source": source,
            }

    monkeypatch.setattr(imports_module, "_stream_local_sessions_from_host", _fake_stream)
    lookup_calls: list[str] = []
    original_find = store.find_conversation_by_external_session_id

    def _record_lookup(external_id: str):
        lookup_calls.append(external_id)
        return original_find(external_id)

    monkeypatch.setattr(store, "find_conversation_by_external_session_id", _record_lookup)
    host_conn = SimpleNamespace(
        host_id="host_0123456789abcdef0123456789abcdef", pending_import_local={}
    )
    app = FastAPI()
    app.include_router(
        imports_module.create_imports_router(
            store,
            SqlAlchemyAgentStore(db_uri),
            host_registry=SimpleNamespace(get=lambda _host_id: host_conn),  # type: ignore[arg-type]
            host_store=SimpleNamespace(  # type: ignore[arg-type]
                get_host=lambda _host_id: SimpleNamespace(user_id=None)
            ),
        ),
        prefix="/v1",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/v1/imports/local{'/stream' if stream else ''}",
            json={
                "host_id": host_conn.host_id,
                "source": "claude",
                "session_id": requested_id,
                "force": True,
            },
        )

    assert response.status_code == 200
    if stream:
        events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        assert [event["event"] for event in events if event["event"] == "session"] == []
        assert events[-1]["failed"] == 2
        assert all(event["event"] == "failed" for event in events[:-1])
    else:
        body = response.json()
        assert (body["imported"], body["already_imported"], body["failed"]) == (0, 0, 2)
    assert lookup_calls == []
    for external_id, text in ((requested_id, "requested old"), (other_id, "other old")):
        conversation = store.find_conversation_by_external_session_id(external_id)
        assert conversation is not None
        assert [
            item.data.content[0]["text"] for item in store.list_items(conversation.id).data
        ] == [text]


@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "stream"])
@pytest.mark.parametrize("force", [False, True], ids=["ordinary", "replacement"])
async def test_local_exact_import_accepts_qwen_bare_id_qualified_response(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    force: bool,
) -> None:
    """A bare Qwen id resolves to its project-qualified identity, not a mismatch."""
    from omnigent.server.routes import imports as imports_module
    from omnigent.server.routes.imports import _import_conversation_id

    agent_id = builtin_agent_id("qwen-native-ui")
    SqlAlchemyAgentStore(db_uri).create(
        agent_id, name="qwen-native-ui", bundle_location="builtin://qwen-native-ui"
    )
    store = SqlAlchemyConversationStore(db_uri)
    bare_id = "019f8648-2797-7170-bf73-837f2655c47e"
    # Qwen qualifies a bare recording id by project before returning it.
    qualified_id = f"-repo:{bare_id}"
    if force:
        existing = store.create_conversation(
            agent_id=agent_id,
            title="Old snapshot",
            conversation_id=_import_conversation_id("qwen", qualified_id),
        )
        store.set_external_session_id(existing.id, qualified_id)
        store.set_labels(existing.id, {IMPORT_SOURCE_LABEL_KEY: "qwen"})
        store.append(
            existing.id,
            [
                NewConversationItem(
                    type="message",
                    response_id="qwen:old",
                    data=MessageData(
                        role="user",
                        content=[{"type": "input_text", "text": "old prompt"}],
                    ),
                )
            ],
        )

    async def _fake_stream(**_kwargs: object):
        yield {
            "external_session_id": qualified_id,
            "workspace": None,
            "items": [_user_message_item("qwen:new", "latest prompt")],
            "title": "Qwen snapshot",
            "source": "qwen",
        }

    monkeypatch.setattr(imports_module, "_stream_local_sessions_from_host", _fake_stream)
    host_conn = SimpleNamespace(
        host_id="host_0123456789abcdef0123456789abcdef", pending_import_local={}
    )
    async with _local_import_client(store, db_uri, host_conn) as client:
        response = await client.post(
            f"/v1/imports/local{'/stream' if stream else ''}",
            json={
                "host_id": host_conn.host_id,
                "source": "qwen",
                "session_id": bare_id,
                "force": force,
            },
        )

    assert response.status_code == 200
    if stream:
        events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        assert [event for event in events if event["event"] == "failed"] == []
        assert (events[-1]["imported"], events[-1]["failed"]) == (1, 0)
    else:
        body = response.json()
        assert (body["imported"], body["failed"]) == (1, 0)
    conversation = store.find_conversation_by_external_session_id(qualified_id)
    assert conversation is not None
    assert [item.data.content[0]["text"] for item in store.list_items(conversation.id).data] == [
        "latest prompt"
    ]


@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "stream"])
async def test_local_import_force_rejects_active_snapshot(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """An active exact snapshot is never replaced, and the user is told why."""
    from fastapi import FastAPI

    from omnigent.server.routes import imports as imports_module
    from omnigent.server.routes.imports import _import_conversation_id

    agent_id = _seed_claude_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    external_id = "claude-local-force-active"
    existing = store.create_conversation(
        agent_id=agent_id,
        title="Active snapshot",
        conversation_id=_import_conversation_id("claude", external_id),
    )
    store.set_external_session_id(existing.id, external_id)
    store.set_session_live_status(existing.id, "running")

    async def _fake_stream(**_kwargs: object):
        yield {
            "external_session_id": external_id,
            "workspace": None,
            "items": [
                {
                    "type": "message",
                    "response_id": "claude:new",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "latest prompt"}],
                    },
                }
            ],
            "title": "Should not replace",
            "source": "claude",
        }

    monkeypatch.setattr(imports_module, "_stream_local_sessions_from_host", _fake_stream)
    host_conn = SimpleNamespace(
        host_id="host_0123456789abcdef0123456789abcdef", pending_import_local={}
    )
    app = FastAPI()
    app.include_router(
        imports_module.create_imports_router(
            store,
            SqlAlchemyAgentStore(db_uri),
            host_registry=SimpleNamespace(get=lambda _host_id: host_conn),  # type: ignore[arg-type]
            host_store=SimpleNamespace(  # type: ignore[arg-type]
                get_host=lambda _host_id: SimpleNamespace(user_id=None)
            ),
        ),
        prefix="/v1",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/v1/imports/local{'/stream' if stream else ''}",
            json={
                "host_id": host_conn.host_id,
                "source": "claude",
                "session_id": external_id,
                "force": True,
            },
        )

    assert response.status_code == 200
    failure = _single_local_import_failure(response, stream)
    assert failure["external_session_id"] == external_id
    assert "Cannot replace an active session" in failure["reason"]
    preserved = store.get_conversation(existing.id)
    assert preserved is not None
    assert preserved.title == "Active snapshot"
    assert preserved.live_status == "running"


def _user_message_item(response_id: str, text: str) -> dict[str, object]:
    """One normalized user message as the host/CLI posts it."""
    return {
        "type": "message",
        "response_id": response_id,
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }


def _single_local_import_failure(response: httpx.Response, stream: bool) -> dict[str, object]:
    """The one per-session failure a buffered or streamed local import reported."""
    if stream:
        events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        assert not [event for event in events if event["event"] in ("session", "error")]
        assert (events[-1]["imported"], events[-1]["failed"]) == (0, 1)
        (failure,) = [event for event in events if event["event"] == "failed"]
        return failure
    body = response.json()
    assert (body["imported"], body["failed"]) == (0, 1)
    (failure,) = body["failures"]
    return failure


def _local_import_client(
    store: SqlAlchemyConversationStore,
    db_uri: str,
    host_conn: SimpleNamespace,
    *,
    host_user_id: str | None = None,
    **router_kwargs: object,
) -> httpx.AsyncClient:
    """Mount the imports router against one fake connected host."""
    app = FastAPI()
    app.include_router(
        create_imports_router(
            store,
            SqlAlchemyAgentStore(db_uri),
            host_registry=SimpleNamespace(get=lambda _host_id: host_conn),  # type: ignore[arg-type]
            host_store=SimpleNamespace(  # type: ignore[arg-type]
                get_host=lambda _host_id: SimpleNamespace(user_id=host_user_id)
            ),
            **router_kwargs,  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class _FixedUserAuth(AuthProvider):
    def __init__(self, user_id: str) -> None:
        self._user_id = user_id

    def get_user_id(self, request: object) -> str | None:
        return self._user_id


@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "stream"])
async def test_local_import_force_rejects_non_owner(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """Only the snapshot's owner may replace it; others get the redacted error."""
    from omnigent.server.routes import imports as imports_module
    from omnigent.server.routes.imports import _import_conversation_id

    agent_id = _seed_claude_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    external_id = "claude-local-owned-elsewhere"
    existing = store.create_conversation(
        agent_id=agent_id,
        title="Owned by alice",
        conversation_id=_import_conversation_id("claude", external_id),
    )
    store.set_external_session_id(existing.id, external_id)
    store.append(
        existing.id,
        [
            NewConversationItem(
                type="message",
                response_id="claude:old",
                data=MessageData(
                    role="user", content=[{"type": "input_text", "text": "old prompt"}]
                ),
            )
        ],
    )
    permissions.ensure_user("alice")
    permissions.grant("alice", existing.id, LEVEL_OWNER)

    async def _fake_stream(**_kwargs: object):
        yield {
            "external_session_id": external_id,
            "workspace": None,
            "items": [_user_message_item("claude:new", "new prompt")],
            "title": "Should not replace",
            "source": "claude",
        }

    monkeypatch.setattr(imports_module, "_stream_local_sessions_from_host", _fake_stream)
    host_conn = SimpleNamespace(
        host_id="host_0123456789abcdef0123456789abcdef", pending_import_local={}
    )
    async with _local_import_client(
        store,
        db_uri,
        host_conn,
        host_user_id="bob",
        auth_provider=_FixedUserAuth("bob"),
        permission_store=permissions,
    ) as client:
        response = await client.post(
            f"/v1/imports/local{'/stream' if stream else ''}",
            json={
                "host_id": host_conn.host_id,
                "source": "claude",
                "session_id": external_id,
                "force": True,
            },
        )

    if stream:
        events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        assert not [event for event in events if event["event"] == "session"]
        (error,) = [event for event in events if event["event"] == "error"]
        message = error["message"]
    else:
        assert response.status_code == 404
        message = response.json()["error"]["message"]
    assert message.startswith("The local session import stopped unexpectedly.")
    assert existing.id not in response.text
    assert [item.data.content[0]["text"] for item in store.list_items(existing.id).data] == [
        "old prompt"
    ]


async def test_force_import_rejects_mismatched_existing_source(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A forced import under another harness never replaces a Claude import."""
    _seed_claude_agent(db_uri)
    payload = {
        "source": "claude",
        "external_session_id": "shared-external-id",
        "items": [_user_message_item("claude:old", "old prompt")],
    }
    created = await client.post("/v1/imports", json=payload)
    assert created.status_code == 201

    replaced = await client.post(
        "/v1/imports",
        json={
            **payload,
            "source": "codex",
            "force": True,
            "items": [_user_message_item("codex:new", "new prompt")],
        },
    )

    assert replaced.status_code == 409
    assert "did not come from codex" in replaced.json()["error"]["message"]
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.get_conversation(created.json()["session_id"])
    assert conversation is not None
    assert conversation.labels[IMPORT_SOURCE_LABEL_KEY] == "claude"
    assert [item.data.content[0]["text"] for item in store.list_items(conversation.id).data] == [
        "old prompt"
    ]


@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "stream"])
async def test_local_import_force_rejects_mismatched_existing_source(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """A forced Codex request cannot overwrite a session imported from Claude."""
    from omnigent.server.routes import imports as imports_module

    _seed_claude_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    external_id = "shared-local-external-id"
    frame: dict[str, object] = {
        "external_session_id": external_id,
        "workspace": None,
        "items": [_user_message_item("claude:old", "old prompt")],
        "title": "Claude snapshot",
        "source": "claude",
    }

    async def _fake_stream(**_kwargs: object):
        yield dict(frame)

    monkeypatch.setattr(imports_module, "_stream_local_sessions_from_host", _fake_stream)
    host_conn = SimpleNamespace(
        host_id="host_0123456789abcdef0123456789abcdef", pending_import_local={}
    )
    async with _local_import_client(store, db_uri, host_conn) as client:
        created = await client.post(
            "/v1/imports/local",
            json={"host_id": host_conn.host_id, "source": "claude", "session_id": external_id},
        )
        assert created.status_code == 200
        assert created.json()["imported"] == 1

        frame.update(source="codex", items=[_user_message_item("codex:new", "new prompt")])
        response = await client.post(
            f"/v1/imports/local{'/stream' if stream else ''}",
            json={
                "host_id": host_conn.host_id,
                "source": "codex",
                "session_id": external_id,
                "force": True,
            },
        )

    assert response.status_code == 200
    failure = _single_local_import_failure(response, stream)
    assert "did not come from codex" in failure["reason"]
    conversation = store.find_conversation_by_external_session_id(external_id)
    assert conversation is not None
    assert conversation.labels[IMPORT_SOURCE_LABEL_KEY] == "claude"
    assert [item.data.content[0]["text"] for item in store.list_items(conversation.id).data] == [
        "old prompt"
    ]


@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "stream"])
async def test_local_import_force_rejects_empty_transcript(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """An empty host transcript never replaces an existing snapshot."""
    from omnigent.server.routes import imports as imports_module

    _seed_claude_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    external_id = "claude-local-empty-replacement"
    frame: dict[str, object] = {
        "external_session_id": external_id,
        "workspace": None,
        "items": [_user_message_item("claude:old", "old prompt")],
        "title": "Snapshot",
        "source": "claude",
    }

    async def _fake_stream(**_kwargs: object):
        yield dict(frame)

    monkeypatch.setattr(imports_module, "_stream_local_sessions_from_host", _fake_stream)
    host_conn = SimpleNamespace(
        host_id="host_0123456789abcdef0123456789abcdef", pending_import_local={}
    )
    async with _local_import_client(store, db_uri, host_conn) as client:
        created = await client.post(
            "/v1/imports/local",
            json={"host_id": host_conn.host_id, "source": "claude", "session_id": external_id},
        )
        assert created.status_code == 200
        assert created.json()["imported"] == 1

        frame["items"] = []
        response = await client.post(
            f"/v1/imports/local{'/stream' if stream else ''}",
            json={
                "host_id": host_conn.host_id,
                "source": "claude",
                "session_id": external_id,
                "force": True,
            },
        )

    assert response.status_code == 200
    failure = _single_local_import_failure(response, stream)
    assert failure["reason"] == "An empty transcript cannot replace an existing snapshot."
    conversation = store.find_conversation_by_external_session_id(external_id)
    assert conversation is not None
    assert [item.data.content[0]["text"] for item in store.list_items(conversation.id).data] == [
        "old prompt"
    ]


@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "stream"])
async def test_local_import_skips_already_imported_before_validating_items(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """A plain re-import stays already-imported even when its host payload no longer parses."""
    from omnigent.server.routes import imports as imports_module

    _seed_claude_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    external_id = "claude-local-already-imported-malformed"
    frame: dict[str, object] = {
        "external_session_id": external_id,
        "workspace": None,
        "items": [_user_message_item("claude:old", "old prompt")],
        "title": "Snapshot",
        "source": "claude",
    }

    async def _fake_stream(**_kwargs: object):
        yield dict(frame)

    monkeypatch.setattr(imports_module, "_stream_local_sessions_from_host", _fake_stream)
    host_conn = SimpleNamespace(
        host_id="host_0123456789abcdef0123456789abcdef", pending_import_local={}
    )
    async with _local_import_client(store, db_uri, host_conn) as client:
        created = await client.post(
            "/v1/imports/local",
            json={"host_id": host_conn.host_id, "source": "claude", "session_id": external_id},
        )
        assert created.status_code == 200
        assert created.json()["imported"] == 1

        frame["items"] = [{"type": "message", "response_id": "claude:new", "data": {}}]
        response = await client.post(
            f"/v1/imports/local{'/stream' if stream else ''}",
            json={"host_id": host_conn.host_id, "source": "claude", "limit": 5},
        )
        # The same payload is rejected once a replacement has to read it.
        forced = await client.post(
            "/v1/imports/local",
            json={
                "host_id": host_conn.host_id,
                "source": "claude",
                "session_id": external_id,
                "force": True,
            },
        )

    assert response.status_code == 200
    if stream:
        events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        assert [event["event"] for event in events] == ["done"]
        tally = events[-1]
    else:
        tally = response.json()
    assert (tally["imported"], tally["already_imported"], tally["failed"]) == (0, 1, 0)
    assert tally["failures"] == []
    assert (forced.json()["imported"], forced.json()["failed"]) == (0, 1)
    conversation = store.find_conversation_by_external_session_id(external_id)
    assert conversation is not None
    assert [item.data.content[0]["text"] for item in store.list_items(conversation.id).data] == [
        "old prompt"
    ]


@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "stream"])
async def test_local_import_force_reports_snapshot_deleted_before_replacement(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """A snapshot deleted after the lookup is one per-session failure, not a stream error."""
    from omnigent.server.routes import imports as imports_module

    _seed_claude_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    external_id = "claude-local-deleted-replacement"

    async def _fake_stream(**_kwargs: object):
        yield {
            "external_session_id": external_id,
            "workspace": None,
            "items": [_user_message_item("claude:new", "latest prompt")],
            "title": "Snapshot",
            "source": "claude",
        }

    monkeypatch.setattr(imports_module, "_stream_local_sessions_from_host", _fake_stream)
    host_conn = SimpleNamespace(
        host_id="host_0123456789abcdef0123456789abcdef", pending_import_local={}
    )
    replace = store.replace_imported_transcript

    def _delete_then_replace(conversation_id: str, *args: object, **kwargs: object) -> object:
        # Runs on the route's worker thread, after the lookup and guards passed.
        asyncio.run(store.delete_conversation(conversation_id))
        return replace(conversation_id, *args, **kwargs)  # type: ignore[arg-type]

    async with _local_import_client(store, db_uri, host_conn) as client:
        created = await client.post(
            "/v1/imports/local",
            json={"host_id": host_conn.host_id, "source": "claude", "session_id": external_id},
        )
        assert created.status_code == 200
        assert created.json()["imported"] == 1

        monkeypatch.setattr(store, "replace_imported_transcript", _delete_then_replace)
        response = await client.post(
            f"/v1/imports/local{'/stream' if stream else ''}",
            json={
                "host_id": host_conn.host_id,
                "source": "claude",
                "session_id": external_id,
                "force": True,
            },
        )

    assert response.status_code == 200
    failure = _single_local_import_failure(response, stream)
    assert failure["reason"] == "This session was deleted before its snapshot could be replaced."
    assert store.find_conversation_by_external_session_id(external_id) is None


@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "stream"])
async def test_local_import_force_serializes_same_source(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """Concurrent exact replacements do not race delete/recreate."""
    from fastapi import FastAPI

    from omnigent.server.routes import imports as imports_module
    from omnigent.server.routes.imports import _import_conversation_id

    agent_id = _seed_claude_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    external_id = "claude-local-force-race"
    existing = store.create_conversation(
        agent_id=agent_id,
        title="Old snapshot",
        conversation_id=_import_conversation_id("claude", external_id),
    )
    store.set_external_session_id(existing.id, external_id)
    active = 0
    maximum_active = 0

    async def _fake_stream(**_kwargs: object):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        try:
            await asyncio.sleep(0.05)
            yield {
                "external_session_id": external_id,
                "workspace": None,
                "items": [
                    {
                        "type": "message",
                        "response_id": "claude:new",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "latest prompt"}],
                        },
                    }
                ],
                "title": "Latest snapshot",
                "source": "claude",
            }
        finally:
            active -= 1

    monkeypatch.setattr(imports_module, "_stream_local_sessions_from_host", _fake_stream)
    host_conn = SimpleNamespace(
        host_id="host_0123456789abcdef0123456789abcdef", pending_import_local={}
    )
    app = FastAPI()
    app.include_router(
        imports_module.create_imports_router(
            store,
            SqlAlchemyAgentStore(db_uri),
            host_registry=SimpleNamespace(get=lambda _host_id: host_conn),  # type: ignore[arg-type]
            host_store=SimpleNamespace(  # type: ignore[arg-type]
                get_host=lambda _host_id: SimpleNamespace(user_id=None)
            ),
        ),
        prefix="/v1",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    f"/v1/imports/local{'/stream' if stream else ''}",
                    json={
                        "host_id": host_conn.host_id,
                        "source": "claude",
                        "session_id": external_id,
                        "force": True,
                    },
                )
                for _ in range(2)
            )
        )

    assert [response.status_code for response in responses] == [200, 200]
    assert maximum_active == 1
    if stream:
        session_ids = [
            next(
                event["session_id"]
                for event in (
                    json.loads(line) for line in response.text.splitlines() if line.strip()
                )
                if event["event"] == "session"
            )
            for response in responses
        ]
    else:
        session_ids = [response.json()["sessions"][0]["session_id"] for response in responses]
    assert session_ids == [existing.id, existing.id]
    assert store.get_conversation(existing.id) is not None


async def test_local_import_stream_emits_ndjson_session_then_done(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The streaming endpoint emits one ``session`` line per import, then ``done``.

    Same import as the buffered ``/imports/local`` but wire-framed as NDJSON so
    the caller can list sessions as they land.
    """
    from fastapi import FastAPI

    from omnigent.server.routes import imports as imports_module

    _seed_claude_agent(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)

    async def _fake_stream(**_kwargs: object):
        for i in (1, 2):
            yield {
                "external_session_id": f"claude-stream-{i}",
                "workspace": "/repo/on/host",
                "items": [
                    {
                        "type": "message",
                        "response_id": "claude:turn-1",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": f"thread {i}"}],
                        },
                    }
                ],
                "title": f"Streamed {i}",
                "source": "claude",
            }

    monkeypatch.setattr(imports_module, "_stream_local_sessions_from_host", _fake_stream)

    host_conn = SimpleNamespace(
        host_id="host_0123456789abcdef0123456789abcdef", pending_import_local={}
    )
    host_registry = SimpleNamespace(get=lambda host_id: host_conn)
    host_store = SimpleNamespace(get_host=lambda host_id: SimpleNamespace(user_id=None))

    app = FastAPI()
    app.include_router(
        imports_module.create_imports_router(
            conversation_store,
            SqlAlchemyAgentStore(db_uri),
            host_registry=host_registry,  # type: ignore[arg-type]
            host_store=host_store,  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/v1/imports/local/stream",
            json={
                "host_id": "host_0123456789abcdef0123456789abcdef",
                "source": "claude",
                "limit": 5,
            },
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    session_events = [e for e in events if e["event"] == "session"]
    assert [e["title"] for e in session_events] == ["Streamed 1", "Streamed 2"]
    # The terminal line carries the tally plus the (here empty) failures list.
    assert events[-1] == {
        "event": "done",
        "imported": 2,
        "already_imported": 0,
        "failed": 0,
        "failures": [],
    }
    # Each streamed session was actually persisted.
    for e in session_events:
        assert conversation_store.get_conversation(e["session_id"]) is not None


async def test_local_import_stream_reports_failure_reasons(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed sessions surface a per-session reason, not just a count.

    Covers both failure origins: a session the server can't normalize (malformed
    frame) and one the host couldn't read (reported on the done frame). Each gets
    a ``failed`` line, the tally counts them, and ``done.failures`` lists reasons.
    """
    from omnigent.server.routes import imports as imports_module

    _seed_claude_agent(db_uri)

    async def _fake_stream(**kwargs: object):
        stats = kwargs.get("stats")
        yield {
            "external_session_id": "ok-1",
            "workspace": None,
            "items": [
                {
                    "type": "message",
                    "response_id": "claude:1",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                }
            ],
            "title": "Good one",
            "source": "claude",
        }
        # Malformed: items is not a list → server-side failure with a reason.
        yield {
            "external_session_id": "bad-1",
            "workspace": None,
            "items": "not-a-list",
            "title": None,
            "source": "claude",
        }
        # A session the host enumerated but couldn't read (no frame, only a
        # reason on the terminal done frame).
        if isinstance(stats, dict):
            stats["host_failed"] = 1
            stats["host_failures"] = [
                {
                    "external_session_id": "unreadable-1",
                    "source": "codex",
                    "reason": "No visible messages to import.",
                }
            ]

    monkeypatch.setattr(imports_module, "_stream_local_sessions_from_host", _fake_stream)

    host_conn = SimpleNamespace(
        host_id="host_0123456789abcdef0123456789abcdef", pending_import_local={}
    )
    app = FastAPI()
    app.include_router(
        imports_module.create_imports_router(
            SqlAlchemyConversationStore(db_uri),
            SqlAlchemyAgentStore(db_uri),
            host_registry=SimpleNamespace(get=lambda _host_id: host_conn),  # type: ignore[arg-type]
            host_store=SimpleNamespace(  # type: ignore[arg-type]
                get_host=lambda _host_id: SimpleNamespace(user_id=None)
            ),
        ),
        prefix="/v1",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/v1/imports/local/stream",
            json={
                "host_id": "host_0123456789abcdef0123456789abcdef",
                "source": "all",
                "limit": 5,
            },
        )

    assert resp.status_code == 200
    events = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    assert [e["title"] for e in events if e["event"] == "session"] == ["Good one"]
    failed = [e for e in events if e["event"] == "failed"]
    assert {f["reason"] for f in failed} == {
        "This session's transcript was malformed or too large to import.",
        "No visible messages to import.",
    }
    # The host-read failure keeps its source-session identity through the wire.
    assert {(f["external_session_id"], f["source"]) for f in failed} == {
        ("bad-1", "claude"),
        ("unreadable-1", "codex"),
    }
    done = events[-1]
    assert done["event"] == "done"
    assert (done["imported"], done["failed"]) == (1, 2)
    assert len(done["failures"]) == 2


async def test_local_import_id_collision_counts_as_already_imported(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create that collides on the deterministic id is already-imported, not failed.

    A concurrent batch can persist the session (and its external id) after this
    batch's pre-check missed but before its create, so the create then hits the
    deterministic conversation id. That is the same source session, so it must
    count as already-imported and never as a failure or a duplicate.
    """
    from omnigent.server.routes import imports as imports_module
    from omnigent.server.routes.imports import _import_conversation_id

    agent_id = _seed_claude_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    # Occupy the deterministic id but leave the external id unindexed, so the
    # pre-check misses and only the create surfaces the collision (the race).
    store.create_conversation(
        agent_id=agent_id,
        title="pre-existing",
        conversation_id=_import_conversation_id("claude", "race-1"),
    )

    async def _fake_stream(**_kwargs: object):
        yield {
            "external_session_id": "race-1",
            "workspace": None,
            "items": [
                {
                    "type": "message",
                    "response_id": "claude:1",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                }
            ],
            "title": "Racing",
            "source": "claude",
        }

    monkeypatch.setattr(imports_module, "_stream_local_sessions_from_host", _fake_stream)

    host_conn = SimpleNamespace(
        host_id="host_0123456789abcdef0123456789abcdef", pending_import_local={}
    )
    app = FastAPI()
    app.include_router(
        imports_module.create_imports_router(
            store,
            SqlAlchemyAgentStore(db_uri),
            host_registry=SimpleNamespace(get=lambda _host_id: host_conn),  # type: ignore[arg-type]
            host_store=SimpleNamespace(  # type: ignore[arg-type]
                get_host=lambda _host_id: SimpleNamespace(user_id=None)
            ),
        ),
        prefix="/v1",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/v1/imports/local",
            json={
                "host_id": "host_0123456789abcdef0123456789abcdef",
                "source": "claude",
                "limit": 5,
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert (body["imported"], body["already_imported"], body["failed"]) == (0, 1, 0)
    assert body["failures"] == []


def _claude_message_records(turns: list[tuple[str, str]]) -> list[dict[str, object]]:
    """Normalized items for a Claude transcript of ``(role, text)`` turns."""
    records: list[dict[str, object]] = []
    for index, (role, text) in enumerate(turns):
        if role == "user":
            data: dict[str, object] = {
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            }
        else:
            data = {
                "role": "assistant",
                "agent": "claude-native-ui",
                "content": [{"type": "output_text", "text": text}],
            }
        records.append({"type": "message", "response_id": f"claude:turn-{index}", "data": data})
    return records


async def test_local_import_by_id_refreshes_drifted_session(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain re-import of a drifted session stays skipped; ``force`` refreshes it in place."""
    from omnigent.server.routes import imports as imports_module

    _seed_claude_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    external_id = "65dffbe9-drift"

    pre_drift = [("user", "set up the repo"), ("assistant", "On it.")]
    drifted_turn = "continue: add the follow-up turn from a month later"
    post_drift = [*pre_drift, ("user", drifted_turn), ("assistant", "Added.")]
    # The host re-reads the on-disk transcript per import; the second read sees
    # the session the user kept chatting with (drift).
    source_transcript = {"turns": pre_drift}

    async def _fake_stream(**_kwargs: object):
        yield {
            "external_session_id": external_id,
            "workspace": "/repo",
            "items": _claude_message_records(source_transcript["turns"]),
            "title": "drifting thread",
            "source": "claude",
        }

    monkeypatch.setattr(imports_module, "_stream_local_sessions_from_host", _fake_stream)

    host_conn = SimpleNamespace(
        host_id="host_0123456789abcdef0123456789abcdef", pending_import_local={}
    )
    app = FastAPI()
    app.include_router(
        imports_module.create_imports_router(
            store,
            SqlAlchemyAgentStore(db_uri),
            host_registry=SimpleNamespace(get=lambda _host_id: host_conn),  # type: ignore[arg-type]
            host_store=SimpleNamespace(  # type: ignore[arg-type]
                get_host=lambda _host_id: SimpleNamespace(user_id=None)
            ),
        ),
        prefix="/v1",
    )

    def _stored_texts(conversation_id: str) -> list[str]:
        return [
            item.data.content[0]["text"]
            for item in store.list_items(conversation_id, limit=100).data
        ]

    exact = {"host_id": host_conn.host_id, "source": "claude", "session_id": external_id}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        first = await c.post("/v1/imports/local", json=exact)
        assert first.status_code == 200
        assert (first.json()["imported"], first.json()["already_imported"]) == (1, 0)
        conversation = store.find_conversation_by_external_session_id(external_id)
        assert conversation is not None
        assert drifted_turn not in _stored_texts(conversation.id)

        source_transcript["turns"] = post_drift
        # A plain re-import still deduplicates, so the stale snapshot stays.
        skipped = await c.post("/v1/imports/local", json=exact)
        assert skipped.status_code == 200
        assert (skipped.json()["imported"], skipped.json()["already_imported"]) == (0, 1)
        assert drifted_turn not in _stored_texts(conversation.id)

        refreshed = await c.post("/v1/imports/local", json={**exact, "force": True})
        assert refreshed.status_code == 200

    tally = refreshed.json()
    assert (tally["imported"], tally["already_imported"], tally["failed"]) == (1, 0, 0), (
        f"replacement did not refresh the drifted session: {tally!r}"
    )
    assert tally["sessions"][0]["session_id"] == conversation.id
    assert _stored_texts(conversation.id) == [text for _role, text in post_drift]
    replaced = store.get_conversation(conversation.id)
    assert replaced is not None
    assert replaced.title == "drifting thread"


@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "stream"])
async def test_local_import_endpoints_redact_host_error(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stream: bool,
) -> None:
    """Host failures retain diagnostics in logs but not either API response."""
    from omnigent.server.routes import imports as imports_module

    sensitive_detail = "host read failed at /private/transcripts/session.json\nTraceback: secret"

    async def _fake_stream(**_kwargs: object):
        yield {}
        raise OmnigentError(sensitive_detail, code=ErrorCode.CONFLICT)

    monkeypatch.setattr(imports_module, "_stream_local_sessions_from_host", _fake_stream)

    host_conn = SimpleNamespace(
        host_id="host_0123456789abcdef0123456789abcdef", pending_import_local={}
    )
    app = FastAPI()
    app.include_router(
        imports_module.create_imports_router(
            SqlAlchemyConversationStore(db_uri),
            SqlAlchemyAgentStore(db_uri),
            host_registry=SimpleNamespace(get=lambda _host_id: host_conn),  # type: ignore[arg-type]
            host_store=SimpleNamespace(  # type: ignore[arg-type]
                get_host=lambda _host_id: SimpleNamespace(user_id=None)
            ),
        ),
        prefix="/v1",
    )

    transport = httpx.ASGITransport(app=app)
    with caplog.at_level(logging.ERROR, logger=imports_module.__name__):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/v1/imports/local{'/stream' if stream else ''}",
                json={
                    "host_id": "host_0123456789abcdef0123456789abcdef",
                    "source": "claude",
                    "limit": 5,
                },
            )

    if stream:
        events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        # A degenerate session yielded before the raise surfaces as a benign
        # ``failed`` line; the host read error is a separate ``error`` line.
        error_events = [e for e in events if e["event"] == "error"]
        assert len(error_events) == 1
        error_payload = error_events[0]
    else:
        assert response.status_code == 409
        error_payload = response.json()["error"]
        assert error_payload["code"] == ErrorCode.CONFLICT

    error_id = error_payload["error_id"]
    assert error_id.startswith("err_")
    assert len(error_id) == 36
    int(error_id.removeprefix("err_"), 16)
    assert error_payload["message"] == (
        "The local session import stopped unexpectedly. "
        f"Retry the import or contact an administrator. Error ID: {error_id}."
    )
    assert sensitive_detail not in response.text
    assert sensitive_detail in caplog.text
    assert error_id in caplog.text


def _host_import_client(db_uri: str, host_registry: HostRegistry) -> httpx.AsyncClient:
    """Mount only the imports router with host support wired, auth disabled.

    The default ``client`` fixture builds the app with ``host_store=None``, so
    ``/imports/local`` short-circuits before the host lookup. Here host_store is
    real (holds the seeded row) and the registry is caller-supplied (empty = the
    host's tunnel is not on this replica), which is what exercises the
    wrong-replica-vs-offline classification.
    """
    app = FastAPI()
    app.include_router(
        create_imports_router(
            SqlAlchemyConversationStore(db_uri),
            SqlAlchemyAgentStore(db_uri),
            host_registry=host_registry,
            host_store=HostStore(db_uri),
        ),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def _handle(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_import_local_live_host_off_replica_is_wrong_replica(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live host absent from this replica is WRONG_REPLICA, not "not connected".

    Regression: the route used to flatten every registry miss into a 409
    CONFLICT, so a host live on another replica never got the 400 wrong_replica
    signal the client re-addresses on — the import failed permanently.

    Only a sharded (multi-replica) deployment has an "other replica" to
    re-address to, so force the sharded signal on — the test host stack has no
    lakebox module, which auto-detects as single-replica.
    """
    from omnigent.server.routes import _host_launch

    monkeypatch.setattr(_host_launch, "_deployment_is_sharded", lambda: True)
    host_id = "host_0123456789abcdef0123456789abcdef"
    HostStore(db_uri).upsert_on_connect(host_id, "laptop", "alice@example.com")
    async with _host_import_client(db_uri, HostRegistry()) as client:
        res = await client.post(
            "/v1/imports/local",
            json={"host_id": host_id, "source": "all", "limit": 5},
        )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == ErrorCode.WRONG_REPLICA


async def test_import_local_live_host_single_replica_is_conflict(db_uri: str) -> None:
    """On a single-replica deployment a live-but-absent host is a 409, not a
    WRONG_REPLICA the client can never satisfy (no other replica to re-address
    to). Auto-detection reports single-replica when no lakebox module is present,
    which is the default in the test stack."""
    host_id = "host_0123456789abcdef0123456789abcded"
    HostStore(db_uri).upsert_on_connect(host_id, "laptop", "alice@example.com")
    async with _host_import_client(db_uri, HostRegistry()) as client:
        res = await client.post(
            "/v1/imports/local",
            json={"host_id": host_id, "source": "all", "limit": 5},
        )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == ErrorCode.CONFLICT


async def test_import_local_offline_host_is_conflict(db_uri: str) -> None:
    """A genuinely offline host stays a 409 CONFLICT (not re-addressable)."""
    host_id = "host_fedcba9876543210fedcba9876543210"
    store = HostStore(db_uri)
    store.upsert_on_connect(host_id, "laptop", "alice@example.com")
    store.set_offline(host_id)
    async with _host_import_client(db_uri, HostRegistry()) as client:
        res = await client.post(
            "/v1/imports/local",
            json={"host_id": host_id, "source": "all", "limit": 5},
        )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == ErrorCode.CONFLICT
