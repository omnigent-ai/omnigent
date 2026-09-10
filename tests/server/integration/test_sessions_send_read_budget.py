"""Per-request database read budget for a message send.

``POST /v1/sessions/{id}/events`` used to read the same conversation row three
times — once for the access check, once for runner routing, once to refresh the
binding before dispatch — each in its own pool checkout. On Postgres with
``pool_pre_ping`` every checkout is an extra round-trip, so the count (not the
millisecond) is the thing worth pinning.

The assertions are integers so they don't move with machine load. Routing now
takes the row the access check already loaded; the pre-dispatch refresh stays,
because the runner-resolution ladder above it can rebind ``runner_id`` /
``host_id`` and a request-phase policy may have written labels dispatch reads.
"""

from __future__ import annotations

import traceback
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event

from omnigent.entities import Conversation
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER
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
from tests.server.conftest import ControllableMockClient

ALICE = "alice@example.com"
_RUNNER_ID = "runner_send_budget"

# The reads one send is allowed to make on the conversation row: the access
# check's, and the pre-dispatch refresh that must observe a rebound runner.
_ALLOWED_CONVERSATION_READS = 3
# Pool checkouts the request may take against the conversation engine. Each
# conversation read costs two (the row, then its metadata row); the rest are the
# permission resolve's shared burst and the persist writes.
_CHECKOUT_BUDGET = 9


@pytest.fixture()
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """App with auth on, so the access check reads the conversation for reuse."""
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
    """Async client wired to the auth-enabled app."""
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


class _AcceptedResponse:
    """Runner reply that lets persist-before-forward complete."""

    status_code = 202
    headers: dict[str, str] = {}
    text = ""

    def json(self) -> dict[str, Any]:
        """:returns: An empty runner payload."""
        return {}


class _StubRunnerClient:
    """Stands in for the tunnel-backed client the router hands back."""

    async def post(self, *_a: Any, **_k: Any) -> _AcceptedResponse:
        """:returns: A 202 for the forwarded event."""
        return _AcceptedResponse()

    async def get(self, *_a: Any, **_k: Any) -> _AcceptedResponse:
        """:returns: A 202 for any runner probe."""
        return _AcceptedResponse()


class _OnlineRegistry:
    """Tunnel registry that reports the session's runner as connected."""

    def get(self, runner_id: str) -> object:
        """:returns: A live-session sentinel for any runner."""
        del runner_id
        return object()

    def runner_owner(self, runner_id: str) -> None:
        """:returns: ``None`` — no-auth runner registration."""
        del runner_id
        return


async def _noop_relay_ready(*_a: Any, **_k: Any) -> None:
    """Skip relay startup; the stub runner has no stream to tail."""
    return


@pytest.fixture()
def counted_send(
    auth_app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Any]]:
    """Seed an owned, runner-bound session and count its conversation reads.

    Only the tunnel lookup and the tunnel-backed client are stubbed on the real
    :class:`~omnigent.runner.routing.RunnerRouter`, so routing's own decision to
    read (or reuse) the conversation row is the production one.

    :param auth_app: The auth-enabled app whose router the send goes through.
    :param db_uri: Per-test database URI.
    :param monkeypatch: pytest attribute patcher.
    :returns: A dict carrying the session id, the read/checkout tallies, and
        the conversation each routing call received.
    """
    from omnigent.server.routes.sessions import routes_events as events_mod

    conv_store = SqlAlchemyConversationStore(db_uri)
    conversation = conv_store.create_conversation()
    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user(ALICE)
    perm_store.grant(ALICE, conversation.id, LEVEL_OWNER)
    assert conv_store.set_runner_id(conversation.id, _RUNNER_ID) is True

    monkeypatch.setattr(events_mod, "_ensure_runner_relay_ready", _noop_relay_ready)

    router = auth_app.state.runner_router
    monkeypatch.setattr(router, "_registry", _OnlineRegistry())
    monkeypatch.setattr(router, "_client_for_runner", lambda _rid: _StubRunnerClient())

    routed_conversations: list[Conversation | None] = []
    real_resolver = router.client_for_session_resources

    def _recording_resolver(
        conversation_id: str, *, conversation: Conversation | None = None
    ) -> Any:
        routed_conversations.append(conversation)
        return real_resolver(conversation_id, conversation=conversation)

    monkeypatch.setattr(router, "client_for_session_resources", _recording_resolver)

    read_sites: list[str] = []
    real_get_conversation = SqlAlchemyConversationStore.get_conversation

    def _recording_get_conversation(self: Any, conversation_id: str) -> Any:
        read_sites.append("".join(traceback.format_stack(limit=12)))
        return real_get_conversation(self, conversation_id)

    monkeypatch.setattr(
        SqlAlchemyConversationStore, "get_conversation", _recording_get_conversation
    )

    checkouts: list[int] = []

    def _on_checkout(_dbapi: object, _record: object, _proxy: object) -> None:
        checkouts.append(1)

    engine = conv_store._engine
    event.listen(engine, "checkout", _on_checkout)
    try:
        yield {
            "session_id": conversation.id,
            "read_sites": read_sites,
            "checkouts": checkouts,
            "routed_conversations": routed_conversations,
        }
    finally:
        event.remove(engine, "checkout", _on_checkout)


async def _send_message(client: httpx.AsyncClient, session_id: str) -> httpx.Response:
    """Post one user message the way the web composer does."""
    return await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        },
        headers={"X-Forwarded-Email": ALICE},
    )


@pytest.mark.asyncio
async def test_send_stays_within_its_conversation_read_budget(
    auth_client: httpx.AsyncClient,
    counted_send: dict[str, Any],
) -> None:
    """A send may not grow new reads of the same conversation row.

    The send path keeps routing's own read on purpose (see the safety test
    below), so the budget here is the access check, routing, and the
    pre-dispatch refresh. This pins the count against future growth rather
    than claiming a reduction.
    """
    resp = await _send_message(auth_client, counted_send["session_id"])
    assert resp.status_code == 202, resp.text

    read_sites: list[str] = counted_send["read_sites"]
    assert len(read_sites) <= _ALLOWED_CONVERSATION_READS, (
        f"one send may read the conversation at most {_ALLOWED_CONVERSATION_READS}x "
        f"(access check + routing + pre-dispatch refresh), got {len(read_sites)}:\n"
        + "\n---\n".join(read_sites)
    )


@pytest.mark.asyncio
async def test_send_never_routes_on_a_row_loaded_before_the_policy_gate(
    auth_client: httpx.AsyncClient,
    counted_send: dict[str, Any],
) -> None:
    """Routing on the message path must read the row itself, not inherit one.

    The request-phase policy gate runs between the access check and runner
    resolution, and on an ASK verdict it parks server-side for human approval
    — up to ``DEFAULT_ASK_TIMEOUT`` (one day). A relaunch or heal in that
    window rebinds ``runner_id`` / ``host_id``, so a turn routed on the
    pre-park row would be dispatched to a runner that no longer owns the
    session. Threading the row into routing here is therefore a correctness
    bug, not an optimisation, and this pins it shut.
    """
    resp = await _send_message(auth_client, counted_send["session_id"])
    assert resp.status_code == 202, resp.text

    routed: list[Conversation | None] = counted_send["routed_conversations"]
    assert routed, "the send never resolved a runner through the router"
    assert routed[0] is None, (
        "the message path handed routing a conversation loaded before the "
        f"request-phase policy gate: {routed[0]}"
    )


@pytest.mark.asyncio
async def test_send_stays_within_its_pool_checkout_budget(
    auth_client: httpx.AsyncClient,
    counted_send: dict[str, Any],
) -> None:
    """One send holds its checkout count, the cost that dominates on Postgres.

    Every checkout costs a ``pool_pre_ping`` round-trip against Lakebase, so a
    new checkout on this path is a latency regression even when the added query
    is free. Dropping routing's read took two of them off every send.
    """
    resp = await _send_message(auth_client, counted_send["session_id"])
    assert resp.status_code == 202, resp.text

    checkouts: list[int] = counted_send["checkouts"]
    assert len(checkouts) <= _CHECKOUT_BUDGET, (
        f"a message send may take at most {_CHECKOUT_BUDGET} pool checkouts, "
        f"got {len(checkouts)}; each one is a pre-ping round-trip in production"
    )
