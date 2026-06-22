"""Per-message actor attribution on conversation items.

Mirrors the comment ``created_by`` contract for conversation items:
the human actor who posts a message is recorded on the item and is
visible — distinct per actor — in the session items API. Agent/system
items carry no actor and stay distinguishable.

Coverage spans the route helper that stamps the actor, the end-to-end
``POST /events`` and ``initial_items`` paths (runner stubbed so no real
runner is needed), and the GET read path that surfaces it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.entities import MessageData, NewConversationItem
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT
from omnigent.server.routes._auth_helpers import attribution_user
from omnigent.server.routes.sessions import _build_new_item
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from tests.unit.server.conftest import ControllableMockClient
from tests.unit.server.helpers import create_test_agent

# ── Route helper: actor is stamped onto the new item ─────────────────────────


def test_build_new_item_stamps_created_by() -> None:
    """``_build_new_item`` threads the posting actor onto the item."""
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    )
    item = _build_new_item(body, "resp_1", created_by="bob@example.com")
    assert item.created_by == "bob@example.com"


def test_build_new_item_defaults_created_by_none() -> None:
    """Single-user mode (no actor) leaves ``created_by`` unset."""
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    )
    item = _build_new_item(body, "resp_1")
    assert item.created_by is None


@pytest.mark.parametrize(
    "user_id,expected",
    [
        # The single-user fallback sentinel is not a distinct actor.
        ("local", None),
        # A real authenticated identity passes through unchanged.
        ("alice@example.com", "alice@example.com"),
        # Auth disabled (no provider) already yields None.
        (None, None),
    ],
)
def test_attribution_user_normalizes_local_sentinel(
    user_id: str | None, expected: str | None
) -> None:
    """``attribution_user`` drops the reserved ``"local"`` identity.

    A non-``None`` return for the ``"local"`` case would mean the route
    records ``created_by="local"``, which the UI renders as a spurious
    author label on every bubble in a single-user session.
    """
    assert attribution_user(user_id) == expected


# ── GET read path: API surfaces per-actor attribution ────────────────────────


def _seed_shared_session(db_uri: str, grants: dict[str, int]) -> str:
    """Create a conversation and grant access to each user."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    conversation = conv_store.create_conversation()
    perm_store = SqlAlchemyPermissionStore(db_uri)
    for user_email, level in grants.items():
        perm_store.ensure_user(user_email)
        perm_store.grant(user_email, conversation.id, level)
    return conversation.id


def _seed_items(db_uri: str, session_id: str) -> None:
    """Append owner, collaborator, and agent items to the session."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.append(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_owner",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "owner asks"}],
                ),
                created_by="alice@example.com",
            ),
            NewConversationItem(
                type="message",
                response_id="resp_collab",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "collaborator asks"}],
                ),
                created_by="bob@example.com",
            ),
            NewConversationItem(
                type="message",
                response_id="resp_agent",
                data=MessageData(
                    role="assistant",
                    agent="coding-agent",
                    content=[{"type": "output_text", "text": "done"}],
                ),
            ),
        ],
    )


@pytest.fixture()
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """App with ``permission_store`` enabled so auth is active.

    Uses header-mode auth so tests can authenticate via
    ``X-Forwarded-Email`` without needing cookie infrastructure.
    ``local_single_user=True`` models the single-user local runtime,
    so headerless requests resolve to the reserved ``"local"``
    sentinel (exercised by the attribution tests below) instead of
    being rejected with 401 as on a deployed multi-user server.
    """
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=True),
    )


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """Async HTTP client wired to the auth-enabled app."""
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


@pytest.mark.asyncio
async def test_session_items_expose_per_actor_attribution(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """GET items distinguishes owner, collaborator, and agent messages.

    A collaborator reading the shared session sees both human messages
    attributed to their actual authors (distinct values) and the agent
    message with no ``created_by`` key.
    """
    session_id = _seed_shared_session(
        db_uri,
        {"alice@example.com": LEVEL_EDIT, "bob@example.com": LEVEL_EDIT},
    )
    _seed_items(db_uri, session_id)

    # Bob (a collaborator) reads the session.
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/items",
        headers={"X-Forwarded-Email": "bob@example.com"},
    )
    resp.raise_for_status()
    items = resp.json()["data"]
    assert len(items) == 3

    by_resp = {it["response_id"]: it for it in items}
    assert by_resp["resp_owner"]["created_by"] == "alice@example.com"
    assert by_resp["resp_collab"]["created_by"] == "bob@example.com"
    # Owner and collaborator are distinguishable, not collapsed.
    assert by_resp["resp_owner"]["created_by"] != by_resp["resp_collab"]["created_by"]
    # Agent message carries no human actor.
    assert "created_by" not in by_resp["resp_agent"]


# ── End-to-end: the route stamps the authenticated poster ────────────────────


class _CaptureRunnerClient:
    """Stub runner client that accepts the forwarded event POST."""

    async def post(self, path: str, *, json: dict[str, Any], **_: Any) -> Any:
        """Return a fake 202 so persist-before-forward completes."""

        class _Resp:
            status_code = 202
            headers: dict[str, str] = {}
            text = ""

        return _Resp()

    async def get(self, *_: Any, **__: Any) -> Any:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_post_event_records_authenticated_poster(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``POST /events`` persists the item with the caller's identity.

    The runner is stubbed (persist-before-forward still runs), so this
    pins the one line that connects ``X-Forwarded-Email`` to the
    persisted ``created_by``. If the route stopped threading the
    user_id, the read-back below would be ``None``.
    """
    from omnigent.server.routes import sessions as sessions_mod

    async def _stub(*_: Any, **__: Any) -> _CaptureRunnerClient:
        return _CaptureRunnerClient()

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _stub)

    session_id = _seed_shared_session(db_uri, {"alice@example.com": LEVEL_EDIT})

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        },
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert resp.status_code == 202, resp.text

    items = await asyncio.to_thread(SqlAlchemyConversationStore(db_uri).list_items, session_id)
    [persisted] = items.data
    assert persisted.created_by == "alice@example.com"


@pytest.mark.asyncio
async def test_input_consumed_event_carries_created_by(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live ``session.input.consumed`` event carries the poster.

    A collaborator watching the stream must be able to attribute a
    just-arrived message without waiting for a hydration refresh, so the
    event payload mirrors the persisted item's ``created_by``. If the
    emit stopped threading ``item.created_by``, the captured payload
    would omit it (``None``) and the other client's bubble would stay
    unlabeled until refresh.
    """
    from omnigent.server.routes import sessions as sessions_mod

    async def _stub(*_: Any, **__: Any) -> _CaptureRunnerClient:
        return _CaptureRunnerClient()

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _stub)

    published: list[dict[str, Any]] = []

    def _capture(_session_id: str, event: dict[str, Any]) -> None:
        published.append(event)

    monkeypatch.setattr(sessions_mod.session_stream, "publish", _capture)

    session_id = _seed_shared_session(db_uri, {"alice@example.com": LEVEL_EDIT})

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        },
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert resp.status_code == 202, resp.text

    consumed = [e for e in published if e["type"] == "session.input.consumed"]
    assert len(consumed) == 1, f"expected one consumed event, got {[e['type'] for e in published]}"
    # The author rides on the payload level (beside item_id/type), matching
    # the client parser and the GET items contract.
    assert consumed[0]["data"]["created_by"] == "alice@example.com"


@pytest.mark.asyncio
async def test_single_user_local_actor_not_attributed(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Single-user mode (``"local"`` identity) leaves messages unattributed.

    ``auth_app`` runs with header-mode auth, so a request with
    no ``X-Forwarded-Email`` header resolves to the reserved ``"local"``
    sentinel. That sentinel is not a distinct human actor, so the route
    must record ``created_by=None`` (not the literal ``"local"``);
    otherwise the UI labels every bubble ``"local"``.
    """
    agent = await create_test_agent(auth_client)

    resp = await auth_client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "kick off"}],
                    },
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    items = await asyncio.to_thread(SqlAlchemyConversationStore(db_uri).list_items, session_id)
    [persisted] = items.data
    assert persisted.created_by is None


@pytest.mark.asyncio
async def test_initial_items_record_creator(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Session-creation ``initial_items`` carry the creator's identity.

    No runner is bound, so the items take the history-only seed branch.
    Each seeded item must record the authenticated creator.
    """
    agent = await create_test_agent(auth_client, user="alice@example.com")

    resp = await auth_client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "kick off"}],
                    },
                }
            ],
        },
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    items = await asyncio.to_thread(SqlAlchemyConversationStore(db_uri).list_items, session_id)
    [persisted] = items.data
    assert persisted.created_by == "alice@example.com"


@pytest.mark.asyncio
async def test_external_conversation_item_direct_terminal_attributes_request_actor(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Direct terminal input stamps the forwarder's authenticated identity.

    When the transcript forwarder POSTs ``external_conversation_item``
    for a message typed directly in the native terminal (no prior web-UI
    POST, so no pending-input entry exists), the server must attribute the
    item to whoever is authenticated on the forwarder request. Before the
    fix, ``_persist_external_conversation_item`` received no ``created_by``
    argument, so items persisted with ``None`` and the author label never
    appeared in the web UI for terminal-typed messages.
    """
    from omnigent.runtime import pending_inputs

    session_id = _seed_shared_session(db_uri, {"alice@example.com": LEVEL_EDIT})

    # Ensure no stale pending entry exists for this session — the fix only
    # fires on the no-pending-entry branch, so a leftover entry would mask
    # the regression we're guarding against.
    pending_inputs.reset_for_tests()

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi from terminal"}],
                },
                "response_id": "resp_terminal_1",
            },
        },
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert resp.status_code == 202, resp.text

    items = await asyncio.to_thread(SqlAlchemyConversationStore(db_uri).list_items, session_id)
    [persisted] = items.data
    # The forwarder authenticates as alice — the item must carry her email.
    # If this is None, _persist_external_conversation_item stopped reading
    # created_by from the request and direct terminal typing has no author label.
    assert persisted.created_by == "alice@example.com"
