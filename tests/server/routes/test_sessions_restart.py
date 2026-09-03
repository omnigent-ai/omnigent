"""Tests for ``POST /v1/sessions/{id}/restart``.

Exercises the restart-in-place endpoint: validation (404 missing session,
400 sub-agent / no binding / unknown truncation point / unloadable bundle,
409 while busy) and the happy-path wiring (stop → store restart → relaunch
→ event). Real-type store stubs — no MagicMock.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from omnigent.entities import Agent, Conversation, ConversationItem, MessageData, PagedList
from omnigent.errors import OmnigentError
from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.server.routes.sessions import routes_core as routes_core_mod

# ── Stubs ────────────────────────────────────────────────────────


class _AgentStore:
    """Agent store stub: get + list for the restart route.

    :param agents: Pre-populated map of agent_id → Agent.
    """

    def __init__(self, agents: dict[str, Agent]) -> None:
        self._agents = dict(agents)

    def get(self, agent_id: str) -> Agent | None:
        """:returns: The agent if present, else None."""
        return self._agents.get(agent_id)

    def get_by_name(self, name: str) -> Agent | None:
        """:returns: The first agent with this name, else None."""
        return next((a for a in self._agents.values() if a.name == name), None)

    def list(
        self,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
    ) -> PagedList[Agent]:
        """Return the built-in (session_id is None) agents.

        :param limit: Max agents (ignored — stubs are small).
        :returns: A PagedList of the template agents.
        """
        del after, before, order
        builtins = [a for a in self._agents.values() if a.session_id is None][:limit]
        return PagedList(data=builtins, first_id=None, last_id=None, has_more=False)


class _ConversationStore:
    """Conversation store stub for the restart route.

    :param conversations: Map of id → Conversation.
    :param items_by_conv: Map of conv_id → items.
    """

    def __init__(
        self,
        conversations: dict[str, Conversation],
        items_by_conv: dict[str, list[ConversationItem]] | None = None,
    ) -> None:
        self._convs = conversations
        self._items = items_by_conv or {}
        self.restart_calls: list[dict[str, Any]] = []

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """:returns: The conversation if present, else None."""
        return self._convs.get(conversation_id)

    def restart_conversation(
        self,
        conversation_id: str,
        *,
        new_agent_id: str | None,
        new_agent_name: str | None,
        new_agent_bundle_location: str | None,
        new_agent_description: str | None,
        presentation_labels: dict[str, str] | None,
        up_to_response_id: str | None,
        carry_history_into_native: bool,
    ) -> Conversation:
        """Record the call and return the restarted conversation.

        :param conversation_id: Session being restarted.
        :param new_agent_id: Refreshed clone id, or None to keep the binding.
        :param new_agent_name: Clone name.
        :param new_agent_bundle_location: Fresh bundle key.
        :param new_agent_description: Fresh description.
        :param presentation_labels: ui/wrapper labels from the refresh source.
        :param up_to_response_id: Truncation point, or None.
        :param carry_history_into_native: Rebuild directive flag.
        :returns: The conversation, rebound when ``new_agent_id`` is set.
        :raises LookupError: If the conversation is unknown.
        """
        self.restart_calls.append(
            {
                "conversation_id": conversation_id,
                "new_agent_id": new_agent_id,
                "new_agent_name": new_agent_name,
                "new_agent_bundle_location": new_agent_bundle_location,
                "new_agent_description": new_agent_description,
                "presentation_labels": presentation_labels,
                "up_to_response_id": up_to_response_id,
                "carry_history_into_native": carry_history_into_native,
            }
        )
        src = self._convs.get(conversation_id)
        if src is None:
            raise LookupError(conversation_id)
        return Conversation(
            id=conversation_id,
            created_at=src.created_at,
            updated_at=200,
            root_conversation_id=conversation_id,
            agent_id=new_agent_id or src.agent_id,
            title=src.title,
            kind=src.kind,
            host_id=src.host_id,
        )

    def list_items(
        self,
        conversation_id: str,
        limit: int = 100,
        after: str | None = None,
        before: str | None = None,
        order: str = "asc",
        type: str | None = None,
    ) -> PagedList[ConversationItem]:
        """:returns: A PagedList of the conversation's items."""
        del limit, after, before, order, type
        items = self._items.get(conversation_id, [])
        return PagedList(
            data=items,
            first_id=items[0].id if items else None,
            last_id=items[-1].id if items else None,
            has_more=False,
        )


class _AgentCacheStub:
    """Stub for ``get_agent_cache()`` — controls the bundle precheck.

    :param raise_on_load: When True, ``load`` raises to simulate an
        unloadable refresh-source bundle (the route maps this to 400).
    """

    def __init__(self, raise_on_load: bool = False) -> None:
        self._raise = raise_on_load

    def load(
        self,
        agent_id: str,
        bundle_location: str,
        *,
        expand_env: bool = False,
    ) -> object:
        """Pretend to load a bundle.

        :param agent_id: Agent id (unused).
        :param bundle_location: Bundle key (unused).
        :param expand_env: Accepted to match the real ``AgentCache.load``.
        :returns: A sentinel object (the route ignores the value).
        :raises RuntimeError: When configured to fail.
        """
        del agent_id, bundle_location, expand_env
        if self._raise:
            raise RuntimeError("bundle missing")
        return object()


# ── Helpers ──────────────────────────────────────────────────────

_SESSION_ID = "e9f8f58523cec9a57d3bdf93be543e8c"
_CLONE_ID = "a98bb825ebd41391c19637c58fe3c0b7"
_BUILTIN_ID = "c1030c25bd9d756e4aef6c4e96a7e126"


def _conv(
    conv_id: str = _SESSION_ID,
    agent_id: str | None = _CLONE_ID,
    kind: str = "default",
    host_id: str | None = None,
) -> Conversation:
    """Build a Conversation entity.

    :param conv_id: Conversation id.
    :param agent_id: Bound agent id, or None.
    :param kind: ``"default"`` or ``"sub_agent"``.
    :param host_id: Connected-host id, or None.
    :returns: A Conversation.
    """
    return Conversation(
        id=conv_id,
        created_at=1,
        updated_at=1,
        root_conversation_id=conv_id,
        agent_id=agent_id,
        title="Source",
        kind=kind,
        host_id=host_id,
    )


def _agent(agent_id: str, name: str, bundle: str, session_id: str | None) -> Agent:
    """Build an Agent entity.

    :param agent_id: Agent id.
    :param name: Agent name.
    :param bundle: Bundle location.
    :param session_id: Owning session id (None for a built-in).
    :returns: An Agent.
    """
    return Agent(
        id=agent_id,
        created_at=1,
        name=name,
        bundle_location=bundle,
        version=1,
        session_id=session_id,
    )


def _item(item_id: str, response_id: str) -> ConversationItem:
    """Build a user-message item.

    :param item_id: Item id.
    :param response_id: Turn response id.
    :returns: A ConversationItem.
    """
    return ConversationItem(
        id=item_id,
        type="message",
        status="completed",
        response_id=response_id,
        created_at=1,
        data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
    )


def _build_app(conv_store: _ConversationStore, agent_store: _AgentStore) -> FastAPI:
    """Build a FastAPI app mounting the sessions router + error handler.

    :param conv_store: Conversation store stub.
    :param agent_store: Agent store stub.
    :returns: A configured FastAPI app.
    """
    router = create_sessions_router(
        conversation_store=conv_store,  # type: ignore[arg-type]
        agent_store=agent_store,  # type: ignore[arg-type]
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(router, prefix="/v1")
    return app


def _patch_restart_helpers(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Stub the bundle/labels/stop helpers so the route runs hermetically.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: A dict with the ordered stop/relaunch call log.
    """
    calls: dict[str, list[str]] = {"stopped": [], "reset": []}
    monkeypatch.setattr(sessions_mod, "get_agent_cache", lambda: _AgentCacheStub())
    monkeypatch.setattr(sessions_mod, "_agent_carries_native_fork_history", lambda a: False)
    monkeypatch.setattr(sessions_mod, "_agent_carries_cursor_fork_history", lambda a: False)
    monkeypatch.setattr(sessions_mod, "_presentation_labels_for_agent", lambda a: {})

    async def _stop(session_id: str, runner_router: Any) -> bool:
        del runner_router
        calls["stopped"].append(session_id)
        return True

    monkeypatch.setattr(sessions_mod, "_stop_session_via_runner", _stop)

    async def _reset(request: Any, conversation: Any) -> None:
        del request
        calls["reset"].append(conversation.id)

    monkeypatch.setattr(sessions_mod, "_reset_runner_resources_after_switch", _reset)

    # Runner is gone after the stop → the relaunch ladder takes the
    # CLI-offline branch (no-op) for a hostless session.
    async def _no_client(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _no_client)
    return calls


# The session's currently-bound agent is a session-scoped clone of the
# claude built-in — exactly the fork/switch shape a restart must refresh.
_CLONE_STALE = _agent(_CLONE_ID, "claude (switch ag_old)", "bundle/old", _SESSION_ID)
_CLONE_CURRENT = _agent(_CLONE_ID, "claude (switch ag_old)", "bundle/new", _SESSION_ID)
_BUILTIN_STALE = _agent(_BUILTIN_ID, "claude", "bundle/new", None)


# ── Tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restart_reclones_from_refreshed_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart of a session-scoped clone whose bundle drifted from the
    current built-in rebinds to a fresh clone of the latest bundle.
    """
    conv_store = _ConversationStore(conversations={_SESSION_ID: _conv()})
    agent_store = _AgentStore({_CLONE_ID: _CLONE_STALE, _BUILTIN_ID: _BUILTIN_STALE})
    calls = _patch_restart_helpers(monkeypatch)
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(f"/v1/sessions/{_SESSION_ID}/restart", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "idle"
    assert body["id"] == _SESSION_ID, "restart must keep the session id"
    assert len(body["agent_id"]) == 32 and body["agent_id"] != _CLONE_ID, (
        "restart must bind a freshly cloned session-scoped agent"
    )

    assert len(conv_store.restart_calls) == 1, "route must call restart exactly once"
    call = conv_store.restart_calls[0]
    assert call["conversation_id"] == _SESSION_ID
    assert call["new_agent_bundle_location"] == "bundle/new", "must clone the latest bundle"
    assert call["up_to_response_id"] is None
    assert call["carry_history_into_native"] is False
    # The old execution was stopped before the store mutation.
    assert calls["stopped"] == [_SESSION_ID]


@pytest.mark.asyncio
async def test_restart_keeps_binding_when_bundle_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clone already on the built-in's bundle restarts in place without a
    rebind — same agent id before and after.
    """
    conv_store = _ConversationStore(conversations={_SESSION_ID: _conv()})
    agent_store = _AgentStore({_CLONE_ID: _CLONE_CURRENT, _BUILTIN_ID: _BUILTIN_STALE})
    _patch_restart_helpers(monkeypatch)
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(f"/v1/sessions/{_SESSION_ID}/restart", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_id"] == _CLONE_ID
    call = conv_store.restart_calls[0]
    assert call["new_agent_id"] is None, "no drift → keep the current binding"


@pytest.mark.asyncio
async def test_restart_keeps_builtin_binding_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session bound directly to a built-in keeps that binding: built-in
    rows already track the latest bundle, and pinning them as clones would
    lose future refreshes.
    """
    conv_store = _ConversationStore(conversations={_SESSION_ID: _conv(agent_id=_BUILTIN_ID)})
    agent_store = _AgentStore({_BUILTIN_ID: _agent(_BUILTIN_ID, "claude", "bundle/old", None)})
    _patch_restart_helpers(monkeypatch)
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(f"/v1/sessions/{_SESSION_ID}/restart", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_id"] == _BUILTIN_ID
    assert conv_store.restart_calls[0]["new_agent_id"] is None


@pytest.mark.asyncio
async def test_restart_keeps_custom_clone_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clone with no matching built-in (custom upload) restarts on its
    own bundle.
    """
    custom = _agent(_CLONE_ID, "my-uploaded-agent", "bundle/custom", _SESSION_ID)
    conv_store = _ConversationStore(conversations={_SESSION_ID: _conv()})
    agent_store = _AgentStore({_CLONE_ID: custom, _BUILTIN_ID: _BUILTIN_STALE})
    _patch_restart_helpers(monkeypatch)
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(f"/v1/sessions/{_SESSION_ID}/restart", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_id"] == _CLONE_ID
    call = conv_store.restart_calls[0]
    assert call["new_agent_id"] is None
    assert call["new_agent_bundle_location"] is None


@pytest.mark.asyncio
async def test_restart_truncates_to_response_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``up_to_response_id`` forwards the truncation to the store."""
    conv_store = _ConversationStore(
        conversations={_SESSION_ID: _conv()},
        items_by_conv={_SESSION_ID: [_item("9980c8a9248139f14f4165e5d53088aa", "resp_1")]},
    )
    agent_store = _AgentStore({_CLONE_ID: _CLONE_CURRENT, _BUILTIN_ID: _BUILTIN_STALE})
    _patch_restart_helpers(monkeypatch)
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(
        f"/v1/sessions/{_SESSION_ID}/restart",
        json={"up_to_response_id": "resp_1"},
    )

    assert resp.status_code == 200, resp.text
    call = conv_store.restart_calls[0]
    assert call["up_to_response_id"] == "resp_1"


@pytest.mark.asyncio
async def test_restart_400_unknown_response_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``up_to_response_id`` that appears in no item is rejected before
    anything is stopped or mutated.
    """
    conv_store = _ConversationStore(
        conversations={_SESSION_ID: _conv()},
        items_by_conv={_SESSION_ID: [_item("9980c8a9248139f14f4165e5d53088aa", "resp_1")]},
    )
    agent_store = _AgentStore({_CLONE_ID: _CLONE_CURRENT, _BUILTIN_ID: _BUILTIN_STALE})
    calls = _patch_restart_helpers(monkeypatch)
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(
        f"/v1/sessions/{_SESSION_ID}/restart",
        json={"up_to_response_id": "resp_missing"},
    )

    assert resp.status_code == 400, resp.text
    assert conv_store.restart_calls == []
    assert calls["stopped"] == [], "validation must happen before the stop"


@pytest.mark.asyncio
async def test_restart_404_missing_session() -> None:
    """Restarting an unknown session returns 404."""
    conv_store = _ConversationStore(conversations={})
    agent_store = _AgentStore({})
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(f"/v1/sessions/{_SESSION_ID}/restart", json={})

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_restart_400_sub_agent() -> None:
    """Sub-agent sessions cannot be restarted."""
    conv_store = _ConversationStore(conversations={_SESSION_ID: _conv(kind="sub_agent")})
    agent_store = _AgentStore({_CLONE_ID: _CLONE_STALE})
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(f"/v1/sessions/{_SESSION_ID}/restart", json={})

    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_restart_400_no_agent_binding() -> None:
    """A session with no bound agent has nothing to relaunch."""
    conv_store = _ConversationStore(conversations={_SESSION_ID: _conv(agent_id=None)})
    agent_store = _AgentStore({})
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(f"/v1/sessions/{_SESSION_ID}/restart", json={})

    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_restart_409_when_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restart refuses to run while the session is mid-turn."""
    conv_store = _ConversationStore(conversations={_SESSION_ID: _conv()})
    agent_store = _AgentStore({_CLONE_ID: _CLONE_STALE, _BUILTIN_ID: _BUILTIN_STALE})
    # Mark the session as running in the relay status cache.
    monkeypatch.setitem(sessions_mod._session_status_cache, _SESSION_ID, "running")
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(f"/v1/sessions/{_SESSION_ID}/restart", json={})

    assert resp.status_code == 409, resp.text
    assert conv_store.restart_calls == []


@pytest.mark.asyncio
async def test_restart_400_unloadable_refresh_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unloadable refresh-source bundle is rejected before the stop so a
    failed restart never leaves a session without its execution.
    """
    conv_store = _ConversationStore(conversations={_SESSION_ID: _conv()})
    agent_store = _AgentStore({_CLONE_ID: _CLONE_STALE, _BUILTIN_ID: _BUILTIN_STALE})
    monkeypatch.setattr(
        sessions_mod, "get_agent_cache", lambda: _AgentCacheStub(raise_on_load=True)
    )
    monkeypatch.setattr(sessions_mod, "_agent_carries_native_fork_history", lambda a: False)
    monkeypatch.setattr(sessions_mod, "_agent_carries_cursor_fork_history", lambda a: False)
    monkeypatch.setattr(sessions_mod, "_presentation_labels_for_agent", lambda a: {})
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(f"/v1/sessions/{_SESSION_ID}/restart", json={})

    assert resp.status_code == 400, resp.text
    assert conv_store.restart_calls == []


@pytest.mark.asyncio
async def test_restart_publishes_agent_changed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebound restart publishes ``session_agent_changed`` so open
    frontends re-render the agent badge.
    """

    class _RecordingStream:
        @staticmethod
        def publish(session_id: str, payload: dict[str, Any]) -> None:
            _RecordingStream.events.append((session_id, payload))

        events: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(sessions_mod, "session_stream", _RecordingStream)
    conv_store = _ConversationStore(conversations={_SESSION_ID: _conv()})
    agent_store = _AgentStore({_CLONE_ID: _CLONE_STALE, _BUILTIN_ID: _BUILTIN_STALE})
    _patch_restart_helpers(monkeypatch)
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(f"/v1/sessions/{_SESSION_ID}/restart", json={})

    assert resp.status_code == 200, resp.text
    assert len(_RecordingStream.events) == 1
    stream_session_id, payload = _RecordingStream.events[0]
    assert stream_session_id == _SESSION_ID
    assert payload["agent_id"] == resp.json()["agent_id"]


@pytest.mark.asyncio
async def test_restart_503_when_connected_host_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a connected host, a runner that can't be woken after the stop
    fails with 503 — the session itself is intact and the call is retryable.
    """
    conv_store = _ConversationStore(conversations={_SESSION_ID: _conv(host_id="host_1")})
    agent_store = _AgentStore({_CLONE_ID: _CLONE_CURRENT, _BUILTIN_ID: _BUILTIN_STALE})
    _patch_restart_helpers(monkeypatch)

    async def _offline(**kwargs: Any) -> tuple[None, Any]:
        return None, kwargs["conv"]

    # The relaunch ladder imports these directly from orchestration, so
    # patch the route module's references.
    monkeypatch.setattr(routes_core_mod, "ensure_runner_connected", _offline)
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(f"/v1/sessions/{_SESSION_ID}/restart", json={})

    assert resp.status_code == 503, resp.text
    # The store mutation already happened — the restart is durable and the
    # 503 only reports the relaunch failure (retry picks up from there).
    assert len(conv_store.restart_calls) == 1
