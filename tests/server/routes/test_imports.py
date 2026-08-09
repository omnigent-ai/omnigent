"""Tests for importing normalized local harness sessions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.db.utils import builtin_agent_id
from omnigent.host.frames import HostHelloFrame
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.host_registry import HostRegistry
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore

_HOST_OWNER = "local"
# Host ids are stored as 32-char hex uuids, so the fixtures cannot use
# bare labels.
_HOST_A = "aa" * 16
_HOST_B = "bb" * 16
_HOST_UNKNOWN = "cc" * 16


def _seed_claude_agent(db_uri: str) -> str:
    """Seed the built-in agent because focused app tests skip lifespan startup."""
    agent_id = builtin_agent_id("claude-native-ui")
    SqlAlchemyAgentStore(db_uri).create(
        agent_id,
        name="claude-native-ui",
        bundle_location="builtin://claude-native-ui",
    )
    return agent_id


class _FakeHostWebSocket:
    """Minimal WebSocket stand-in so a host can be registered without a tunnel."""

    async def send_text(self, data: str) -> None:
        """Discard an outbound frame; these tests stub the proxy layer.

        :param data: JSON-encoded frame text.
        """


@dataclass
class _HostEnv:
    """App wired with host support, plus the stores the tests register hosts in.

    :param client: HTTP client bound to the host-enabled app.
    :param host_store: Store backing the ownership/existence checks.
    :param host_registry: Per-replica registry backing the online check.
    """

    client: httpx.AsyncClient
    host_store: HostStore
    host_registry: HostRegistry


@pytest_asyncio.fixture()
async def host_env(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> AsyncIterator[_HostEnv]:
    """Build an app whose imports router has host support configured.

    The shared ``app`` fixture passes no ``host_store``, which is the
    unconfigured (503) posture; the browse/import endpoints need a real
    one, so this builds a parallel app against the same database.

    :param runtime_init: Ensures runtime singletons are initialized.
    :param db_uri: SQLite URI shared by every store in the app.
    :param tmp_path: Per-test scratch dir for artifact/cache stores.
    :returns: Async iterator yielding the assembled environment.
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    host_store = HostStore(db_uri)
    app: FastAPI = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        host_store=host_store,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield _HostEnv(
            client=client,
            host_store=host_store,
            host_registry=app.state.host_registry,
        )


def _register_offline_host(env: _HostEnv, host_id: str) -> None:
    """Record a host the caller owns that has no live connection.

    :param env: The host-enabled environment.
    :param host_id: Host identifier to record.
    """
    env.host_store.upsert_on_connect(host_id, name=host_id, user_id=_HOST_OWNER)
    env.host_store.set_offline(host_id)


def _connect_host(env: _HostEnv, host_id: str) -> None:
    """Record a host the caller owns and put a live connection in the registry.

    :param env: The host-enabled environment.
    :param host_id: Host identifier to connect.
    """
    env.host_store.upsert_on_connect(host_id, name=host_id, user_id=_HOST_OWNER)
    env.host_registry.register(
        host_id=host_id,
        ws=_FakeHostWebSocket(),  # type: ignore[arg-type] — duck-typed
        hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name=host_id),
        owner=_HOST_OWNER,
    )


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


async def test_list_local_sessions_returns_host_summaries(
    host_env: _HostEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browse endpoint returns what the host reported."""
    import omnigent.server.routes.imports as imports_module

    async def _fake_list(
        *, host_registry: object, host_conn: object, source: str, limit: int
    ) -> list[dict[str, object]]:
        """Stand in for the host round-trip with one canned summary."""
        assert source == "claude"
        assert limit == 10
        return [
            {
                "source": "claude",
                "external_session_id": "abc",
                "workspace": "/repo",
                "title": "inspect TODO.md",
                "item_count": 4,
                "preview": [{"role": "user", "text": "inspect TODO.md"}],
            }
        ]

    monkeypatch.setattr(imports_module, "list_local_sessions_on_host", _fake_list)
    _connect_host(host_env, _HOST_A)

    response = await host_env.client.get(
        "/v1/imports/local-sessions",
        params={"host_id": _HOST_A, "source": "claude", "limit": 10},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert body["data"][0]["external_session_id"] == "abc"
    assert body["data"][0]["item_count"] == 4
    assert body["data"][0]["preview"] == [{"role": "user", "text": "inspect TODO.md"}]
    # The picker reads this envelope directly, so lock its exact shape.
    assert body == {
        "object": "list",
        "data": [
            {
                "source": "claude",
                "external_session_id": "abc",
                "workspace": "/repo",
                "title": "inspect TODO.md",
                "item_count": 4,
                "preview": [{"role": "user", "text": "inspect TODO.md"}],
            }
        ],
    }


async def test_list_local_sessions_rejects_unsupported_source(host_env: _HostEnv) -> None:
    """Only Claude and Codex may be browsed from the web picker."""
    _connect_host(host_env, _HOST_A)

    response = await host_env.client.get(
        "/v1/imports/local-sessions", params={"host_id": _HOST_A, "source": "qwen"}
    )

    assert response.status_code == 422


async def test_list_local_sessions_rejects_out_of_range_limit(host_env: _HostEnv) -> None:
    """A deeper listing than the picker supports is refused, not silently capped."""
    _connect_host(host_env, _HOST_A)

    response = await host_env.client.get(
        "/v1/imports/local-sessions",
        params={"host_id": _HOST_A, "source": "claude", "limit": 50},
    )

    assert response.status_code == 422


async def test_list_local_sessions_reports_offline_host(host_env: _HostEnv) -> None:
    """An offline host is a 409, not a hang or a 500."""
    _register_offline_host(host_env, _HOST_B)

    response = await host_env.client.get(
        "/v1/imports/local-sessions", params={"host_id": _HOST_B, "source": "claude"}
    )

    assert response.status_code == 409


async def test_list_local_sessions_reports_unknown_host(host_env: _HostEnv) -> None:
    """A host that was never registered is a 404."""
    response = await host_env.client.get(
        "/v1/imports/local-sessions", params={"host_id": _HOST_UNKNOWN, "source": "claude"}
    )

    assert response.status_code == 404


async def test_list_local_sessions_surfaces_host_failure(
    host_env: _HostEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host-reported listing failure is a 400 carrying the host's message."""
    import omnigent.server.routes.imports as imports_module
    from omnigent.server.routes._host_session_import import LocalSessionProxyError

    async def _fake_list(
        *, host_registry: object, host_conn: object, source: str, limit: int
    ) -> list[dict[str, object]]:
        """Fail the way a host with an unreadable transcript dir would."""
        raise LocalSessionProxyError("the host could not list local sessions")

    monkeypatch.setattr(imports_module, "list_local_sessions_on_host", _fake_list)
    _connect_host(host_env, _HOST_A)

    response = await host_env.client.get(
        "/v1/imports/local-sessions", params={"host_id": _HOST_A, "source": "claude"}
    )

    assert response.status_code == 400
    assert "could not list local sessions" in response.text


async def test_list_local_sessions_maps_lost_connection_to_conflict(
    host_env: _HostEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection lost mid-listing is a 409, not the base error's 400."""
    import omnigent.server.routes.imports as imports_module
    from omnigent.server.routes._host_session_import import LocalSessionHostUnavailableError

    async def _fake_list(
        *, host_registry: object, host_conn: object, source: str, limit: int
    ) -> list[dict[str, object]]:
        """Fail the way a host that dropped its tunnel would."""
        raise LocalSessionHostUnavailableError("host connection lost")

    monkeypatch.setattr(imports_module, "list_local_sessions_on_host", _fake_list)
    _connect_host(host_env, _HOST_A)

    response = await host_env.client.get(
        "/v1/imports/local-sessions", params={"host_id": _HOST_A, "source": "claude"}
    )

    assert response.status_code == 409


async def test_list_local_sessions_skips_malformed_row(
    host_env: _HostEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row this server can't parse is skipped, not a 500 for the whole picker."""
    import omnigent.server.routes.imports as imports_module

    async def _fake_list(
        *, host_registry: object, host_conn: object, source: str, limit: int
    ) -> list[object]:
        """Return well-formed rows around the shapes a skewed host might send."""
        return [
            {
                "source": "claude",
                "external_session_id": "good",
                "workspace": "/repo",
                "title": "inspect TODO.md",
                "item_count": 4,
                "preview": [],
            },
            {"source": "claude", "external_session_id": "bad"},  # missing item_count
            "not-even-a-mapping",
            # Trailing good row: a loop that aborts on the first bad
            # entry would drop this one.
            {"source": "claude", "external_session_id": "also-good", "item_count": 2},
        ]

    monkeypatch.setattr(imports_module, "list_local_sessions_on_host", _fake_list)
    _connect_host(host_env, _HOST_A)

    response = await host_env.client.get(
        "/v1/imports/local-sessions", params={"host_id": _HOST_A, "source": "claude"}
    )

    assert response.status_code == 200
    body = response.json()
    assert [row["external_session_id"] for row in body["data"]] == ["good", "also-good"]


async def test_local_session_endpoints_require_host_support(client: httpx.AsyncClient) -> None:
    """A server built without a host store answers 503 rather than 404."""
    listed = await client.get(
        "/v1/imports/local-sessions", params={"host_id": _HOST_A, "source": "claude"}
    )
    imported = await client.post(
        "/v1/imports/local-sessions",
        json={"host_id": _HOST_A, "source": "claude", "external_session_id": "abc"},
    )

    assert listed.status_code == 503
    assert imported.status_code == 503


async def test_import_local_session_creates_session(
    host_env: _HostEnv,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing from a host creates the same session the CLI would."""
    import omnigent.server.routes.imports as imports_module

    _seed_claude_agent(db_uri)

    async def _fake_load(
        *, host_registry: object, host_conn: object, source: str, external_session_id: str
    ) -> dict[str, object]:
        """Return one normalized item the way the host's loader would."""
        return {
            "source": "claude",
            "external_session_id": external_session_id,
            "workspace": "/repo",
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

    monkeypatch.setattr(imports_module, "load_local_session_on_host", _fake_load)
    _connect_host(host_env, _HOST_A)

    created = await host_env.client.post(
        "/v1/imports/local-sessions",
        json={"host_id": _HOST_A, "source": "claude", "external_session_id": "abc"},
    )
    repeated = await host_env.client.post(
        "/v1/imports/local-sessions",
        json={"host_id": _HOST_A, "source": "claude", "external_session_id": "abc"},
    )

    assert created.status_code == 201
    assert created.json()["status"] == "imported"
    assert created.json()["item_count"] == 1
    assert repeated.status_code == 409

    conversation = SqlAlchemyConversationStore(db_uri).get_conversation(
        created.json()["session_id"]
    )
    assert conversation is not None
    assert conversation.workspace == "/repo"
    assert conversation.external_session_id == "abc"
    assert conversation.labels["omnigent.wrapper"] == "claude-code-native-ui"


async def test_import_local_session_reports_missing_source_session(
    host_env: _HostEnv,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session the host cannot find is a 404."""
    import omnigent.server.routes.imports as imports_module
    from omnigent.server.routes._host_session_import import LocalSessionNotFoundError

    _seed_claude_agent(db_uri)

    async def _fake_load(
        *, host_registry: object, host_conn: object, source: str, external_session_id: str
    ) -> dict[str, object]:
        """Fail the way a host asked for a deleted transcript would."""
        raise LocalSessionNotFoundError("Claude Code session 'gone' was not found")

    monkeypatch.setattr(imports_module, "load_local_session_on_host", _fake_load)
    _connect_host(host_env, _HOST_A)

    response = await host_env.client.post(
        "/v1/imports/local-sessions",
        json={"host_id": _HOST_A, "source": "claude", "external_session_id": "gone"},
    )

    assert response.status_code == 404
    assert "was not found" in response.text


async def test_import_local_session_maps_lost_connection_to_conflict(
    host_env: _HostEnv,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection lost mid-load is a 409, not the base error's 400."""
    import omnigent.server.routes.imports as imports_module
    from omnigent.server.routes._host_session_import import LocalSessionHostUnavailableError

    _seed_claude_agent(db_uri)

    async def _fake_load(
        *, host_registry: object, host_conn: object, source: str, external_session_id: str
    ) -> dict[str, object]:
        """Fail the way a host that dropped its tunnel mid-load would."""
        raise LocalSessionHostUnavailableError("host connection lost during load_local_session")

    monkeypatch.setattr(imports_module, "load_local_session_on_host", _fake_load)
    _connect_host(host_env, _HOST_A)

    response = await host_env.client.post(
        "/v1/imports/local-sessions",
        json={"host_id": _HOST_A, "source": "claude", "external_session_id": "abc"},
    )

    assert response.status_code == 409
    assert "connection lost" in response.text


async def test_import_local_session_surfaces_host_failure(
    host_env: _HostEnv,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host-reported load failure is a 400 carrying the host's message."""
    import omnigent.server.routes.imports as imports_module
    from omnigent.server.routes._host_session_import import LocalSessionProxyError

    _seed_claude_agent(db_uri)

    async def _fake_load(
        *, host_registry: object, host_conn: object, source: str, external_session_id: str
    ) -> dict[str, object]:
        """Fail the way a host with an unparsable transcript would."""
        raise LocalSessionProxyError("the host could not load the session")

    monkeypatch.setattr(imports_module, "load_local_session_on_host", _fake_load)
    _connect_host(host_env, _HOST_A)

    response = await host_env.client.post(
        "/v1/imports/local-sessions",
        json={"host_id": _HOST_A, "source": "claude", "external_session_id": "abc"},
    )

    assert response.status_code == 400
    assert "could not load the session" in response.text


async def test_import_local_session_rejects_empty_history(
    host_env: _HostEnv,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty host payload cannot permanently claim a source session."""
    import omnigent.server.routes.imports as imports_module

    _seed_claude_agent(db_uri)

    async def _fake_load(
        *, host_registry: object, host_conn: object, source: str, external_session_id: str
    ) -> dict[str, object]:
        """Return a transcript the parser found nothing importable in."""
        return {
            "source": "claude",
            "external_session_id": external_session_id,
            "workspace": "/repo",
            "items": [],
        }

    monkeypatch.setattr(imports_module, "load_local_session_on_host", _fake_load)
    _connect_host(host_env, _HOST_A)

    response = await host_env.client.post(
        "/v1/imports/local-sessions",
        json={"host_id": _HOST_A, "source": "claude", "external_session_id": "empty"},
    )

    assert response.status_code == 400
    assert (
        SqlAlchemyConversationStore(db_uri).find_imported_conversation("claude", "empty") is None
    )


@pytest.mark.parametrize(
    "bad_item",
    [
        pytest.param({"type": "message"}, id="missing-required-fields"),
        pytest.param("not-even-a-mapping", id="not-a-mapping"),
    ],
)
async def test_import_local_session_rejects_malformed_item(
    host_env: _HostEnv,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    bad_item: object,
) -> None:
    """A host-returned item the server can't parse is a 400, not a crash."""
    import omnigent.server.routes.imports as imports_module

    _seed_claude_agent(db_uri)

    async def _fake_load(
        *, host_registry: object, host_conn: object, source: str, external_session_id: str
    ) -> dict[str, object]:
        """Return a transcript whose one item this server cannot parse."""
        return {
            "source": "claude",
            "external_session_id": external_session_id,
            "workspace": "/repo",
            "items": [bad_item],
        }

    monkeypatch.setattr(imports_module, "load_local_session_on_host", _fake_load)
    _connect_host(host_env, _HOST_A)

    response = await host_env.client.post(
        "/v1/imports/local-sessions",
        json={"host_id": _HOST_A, "source": "claude", "external_session_id": "malformed"},
    )

    assert response.status_code == 400
    assert (
        SqlAlchemyConversationStore(db_uri).find_imported_conversation("claude", "malformed")
        is None
    )
