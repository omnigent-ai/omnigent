"""Tests for importing normalized local harness sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.db.utils import builtin_agent_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.imports import (
    LocalImportRequest,
    _stream_local_sessions_from_host,
    create_imports_router,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore


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


async def test_import_preserves_source_timestamps(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Imported rows keep the source record times, not the import run time."""
    _seed_claude_agent(db_uri)
    # A historical source window, far before any test run's wall clock.
    first_at, last_at = 1_681_514_000, 1_681_517_600
    payload = {
        "source": "claude",
        "external_session_id": "claude-timed-1",
        "items": [
            {
                "type": "message",
                "response_id": "claude:turn-1",
                "created_at": first_at,
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "inspect TODO.md"}],
                },
            },
            {
                "type": "message",
                "response_id": "claude:turn-1",
                "created_at": last_at,
                "data": {
                    "role": "assistant",
                    "agent": "claude-native-ui",
                    "content": [{"type": "output_text", "text": "Done."}],
                },
            },
        ],
    }

    created = await client.post("/v1/imports", json=payload)

    assert created.status_code == 201
    session_id = created.json()["session_id"]
    conversation = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conversation is not None
    # The session's activity window is the source's, so imported history
    # sorts by when the work happened rather than when it was imported.
    assert conversation.created_at == first_at
    assert conversation.updated_at == last_at
    items = await client.get(f"/v1/sessions/{session_id}/items")
    assert items.status_code == 200
    assert [item["created_at"] for item in items.json()["data"]] == [first_at, last_at]


async def test_import_without_source_times_stamps_import_time(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Sources with no per-record time data keep the import-time fallback."""
    _seed_claude_agent(db_uri)
    payload = {
        "source": "claude",
        "external_session_id": "claude-untimed-1",
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
    before = int(time.time())

    created = await client.post("/v1/imports", json=payload)

    assert created.status_code == 201
    session_id = created.json()["session_id"]
    conversation = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conversation is not None
    assert conversation.created_at >= before
    assert conversation.updated_at >= before
    items = await client.get(f"/v1/sessions/{session_id}/items")
    assert items.status_code == 200
    assert all(item["created_at"] >= before for item in items.json()["data"])


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


def test_exact_local_import_requires_one_harness_and_trims_id() -> None:
    """An exact id is normalized and cannot be paired with the all selector."""
    request = LocalImportRequest(host_id="h1", source="claude", session_id="  exact-id  ")
    assert request.session_id == "exact-id"

    with pytest.raises(ValueError, match="requires a specific harness"):
        LocalImportRequest(host_id="h1", source="all", session_id="exact-id")


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
    # The terminal line carries the tally.
    assert events[-1] == {
        "event": "done",
        "imported": 2,
        "already_imported": 0,
        "failed": 0,
    }
    # Each streamed session was actually persisted.
    for e in session_events:
        assert conversation_store.get_conversation(e["session_id"]) is not None


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
        error_payload = events[0]
        assert error_payload["event"] == "error"
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
