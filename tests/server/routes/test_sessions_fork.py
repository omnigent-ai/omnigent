"""Tests for ``POST /v1/sessions/{source_id}/fork``.

Exercises the fork endpoint's validation logic (404 for missing
session, 400 for no agent binding) and the happy-path response
shape using minimal real-type stubs — no MagicMock.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from omnigent.entities import Agent, Conversation, ConversationItem, MessageData, PagedList
from omnigent.errors import OmnigentError
from omnigent.server.routes.sessions import create_sessions_router

# ── Minimal store stubs ──────────────────────────────────────────


class _AgentStore:
    """Agent store stub that supports get and create for fork tests.

    Pre-populated with agents keyed by ID. ``create`` records the
    call and stores the new agent so the route's clone-then-fork
    sequence succeeds.

    :param agents: Pre-populated map of agent_id → Agent.
    """

    def __init__(self, agents: dict[str, Agent] | None = None) -> None:
        """
        Initialize the stub.

        :param agents: Map from agent ID to Agent entity.
        """
        self._agents: dict[str, Agent] = dict(agents or {})
        self.create_calls: list[dict[str, Any]] = []

    def get(self, agent_id: str) -> Agent | None:
        """
        Return the agent or None.

        :param agent_id: Agent ID to look up.
        :returns: The Agent if found, else None.
        """
        return self._agents.get(agent_id)

    def create(
        self,
        agent_id: str,
        name: str,
        bundle_location: str,
        description: str | None = None,
    ) -> Agent:
        """
        Record the create call and store the new agent.

        :param agent_id: New agent ID, e.g. ``"ag_abc123"``.
        :param name: Agent name.
        :param bundle_location: Bundle location string.
        :param description: Optional description.
        :returns: The newly created Agent.
        """
        self.create_calls.append(
            {
                "agent_id": agent_id,
                "name": name,
                "bundle_location": bundle_location,
                "description": description,
            }
        )
        agent = Agent(
            id=agent_id,
            created_at=1,
            name=name,
            bundle_location=bundle_location,
            version=1,
            description=description,
        )
        self._agents[agent_id] = agent
        return agent


class _ConversationStore:
    """In-memory conversation store stub for route-level tests.

    Provides the subset of the :class:`ConversationStore` interface
    that the fork route calls. Using a real class (not MagicMock)
    so that unexpected attribute access fails loud.

    :param conversations: Pre-populated map of id → Conversation.
    :param items_by_conv: Pre-populated map of conv_id → item list.
    """

    def __init__(
        self,
        conversations: dict[str, Conversation],
        items_by_conv: dict[str, list[ConversationItem]] | None = None,
    ) -> None:
        """
        Initialize the stub.

        :param conversations: Map from conversation ID to Conversation.
        :param items_by_conv: Map from conversation ID to items.
        """
        self._convs = conversations
        self._items = items_by_conv or {}
        self.fork_calls: list[dict[str, Any]] = []

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """
        Return the conversation or None.

        :param conversation_id: Conversation ID to look up.
        :returns: The Conversation if found, else None.
        """
        return self._convs.get(conversation_id)

    def fork_conversation(
        self,
        source_conversation_id: str,
        *,
        title: str | None = None,
        agent_id: str | None = None,
        cloned_agent_name: str | None = None,
        cloned_agent_bundle_location: str | None = None,
        cloned_agent_description: str | None = None,
        copy_model_settings: bool = True,
        carry_history_into_native: bool = False,
        resume_source_native_session: bool = True,
        presentation_labels: dict[str, str] | None = None,
        up_to_response_id: str | None = None,
    ) -> Conversation:
        """
        Record the fork call and return a fixed new conversation.

        :param source_conversation_id: Source ID, e.g. ``"conv_src"``.
        :param title: Optional title for the fork.
        :param agent_id: Agent ID override. When ``None``, inherits
            the source's ``agent_id``.
        :param cloned_agent_name: Name for the fork's cloned agent row
            (route supplies ``"<name> (fork <id>)"`` when cloning).
        :param cloned_agent_bundle_location: Bundle the fork clones into
            a session-scoped agent row created atomically in the store.
        :param cloned_agent_description: Optional clone description.
        :param copy_model_settings: Whether the source's model settings
            carry over (route passes ``False`` on a cross-family switch).
        :param carry_history_into_native: Whether to mark the fork for
            native transcript rebuild (route passes ``True`` for any
            native target, regardless of family).
        :param resume_source_native_session: Whether the source's native
            session id may be stamped for the runner's clone path (route
            passes ``False`` on a cross-family switch — the source's
            native transcript is the wrong format for the target).
        :param presentation_labels: Web UI mode labels for the switched-to
            target (``{}`` to drop them for an SDK target, ``{ui, wrapper}``
            for a native target), or ``None`` on a same-agent fork.
        :param up_to_response_id: Truncation point, e.g. ``"resp_a"``.
            Mirrors the real store: ``None`` copies everything; a value
            matching no item's ``response_id`` raises ValueError.
        :returns: A new Conversation with a deterministic ID.
        :raises LookupError: If source is not in our map.
        :raises ValueError: If *up_to_response_id* matches no item.
        """
        self.fork_calls.append(
            {
                "source": source_conversation_id,
                "title": title,
                "agent_id": agent_id,
                "cloned_agent_name": cloned_agent_name,
                "cloned_agent_bundle_location": cloned_agent_bundle_location,
                "cloned_agent_description": cloned_agent_description,
                "copy_model_settings": copy_model_settings,
                "carry_history_into_native": carry_history_into_native,
                "resume_source_native_session": resume_source_native_session,
                "presentation_labels": presentation_labels,
                "up_to_response_id": up_to_response_id,
            }
        )
        src = self._convs.get(source_conversation_id)
        if src is None:
            raise LookupError(f"not found: {source_conversation_id}")
        if up_to_response_id is not None and not any(
            item.response_id == up_to_response_id
            for item in self._items.get(source_conversation_id, [])
        ):
            raise ValueError(
                f"response not found in conversation "
                f"{source_conversation_id!r}: {up_to_response_id!r}"
            )
        effective_agent_id = agent_id if agent_id is not None else src.agent_id
        # Also store items under the fork ID so list_items returns
        # the copied items (mirrors real store behavior, including the
        # up-to-and-including-last-item-of-the-response truncation).
        fork_id = "conv_forked"
        source_items = list(self._items.get(source_conversation_id, []))
        if up_to_response_id is not None:
            cutoff_index = max(
                index
                for index, item in enumerate(source_items)
                if item.response_id == up_to_response_id
            )
            source_items = source_items[: cutoff_index + 1]
        self._items[fork_id] = source_items
        return Conversation(
            id=fork_id,
            created_at=100,
            updated_at=100,
            root_conversation_id=fork_id,
            title=title or f"Fork of {src.title}",
            agent_id=effective_agent_id,
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
        """
        Return items for the given conversation.

        :param conversation_id: Conversation to list items for.
        :param limit: Max items.
        :param after: Cursor.
        :param before: Cursor.
        :param order: Sort order.
        :param type: Item type filter.
        :returns: A PagedList of items.
        """
        items = self._items.get(conversation_id, [])
        return PagedList(
            data=items,
            first_id=items[0].id if items else None,
            last_id=items[-1].id if items else None,
            has_more=False,
        )


# ── Helpers ──────────────────────────────────────────────────────


def _make_conversation(
    conv_id: str = "conv_src",
    agent_id: str | None = "ag_test",
    title: str = "Source Chat",
    kind: str = "default",
) -> Conversation:
    """
    Build a minimal Conversation entity for testing.

    :param conv_id: Conversation id.
    :param agent_id: Agent id or None.
    :param title: Title string.
    :param kind: Conversation kind, e.g. ``"default"`` or
        ``"sub_agent"``.
    :returns: A Conversation.
    """
    return Conversation(
        id=conv_id,
        created_at=1,
        updated_at=1,
        root_conversation_id=conv_id,
        agent_id=agent_id,
        title=title,
        kind=kind,
    )


def _make_item(item_id: str, text: str, response_id: str = "resp_001") -> ConversationItem:
    """
    Build a minimal ConversationItem for testing.

    :param item_id: Item id.
    :param text: Message text content.
    :param response_id: Response the item belongs to, e.g. ``"resp_001"``.
    :returns: A ConversationItem.
    """
    return ConversationItem(
        id=item_id,
        type="message",
        status="completed",
        response_id=response_id,
        created_at=1,
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": text}],
        ),
    )


def _build_app(
    store: _ConversationStore,
    agent_store: _AgentStore | None = None,
) -> FastAPI:
    """
    Build a FastAPI app with the sessions router and error handler.

    Mirrors the error-handler registration in ``create_app()`` so
    that ``OmnigentError`` is translated into the correct HTTP
    status rather than surfacing as an unhandled 500.

    :param store: The conversation store stub.
    :param agent_store: The agent store stub. Defaults to a
        pre-populated stub with ``ag_test``.
    :returns: A configured FastAPI app ready for TestClient.
    """
    if agent_store is None:
        agent_store = _AgentStore(
            agents={
                "ag_test": Agent(
                    id="ag_test",
                    created_at=1,
                    name="test-agent",
                    bundle_location="ag_test/fakehash",
                    version=1,
                ),
            }
        )
    router = create_sessions_router(
        conversation_store=store,  # type: ignore[arg-type]
        agent_store=agent_store,  # type: ignore[arg-type]
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(
        request: Request,
        exc: OmnigentError,
    ) -> JSONResponse:
        """Translate OmnigentError to an HTTP error response."""
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(router, prefix="/v1")
    return app


# ── Tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fork_session_happy_path() -> None:
    """POST /sessions/{id}/fork returns 201, clones the agent, and
    binds the fork to the cloned agent.

    Verifies that the route clones the source agent, calls
    fork_conversation with the cloned agent_id, applies runner
    affinity, and returns the correct response shape. A wrong
    response shape or missing agent clone breaks clients that
    reconfigure the forked agent independently.
    """
    conv = _make_conversation()
    items = [_make_item("msg_1", "Hello"), _make_item("msg_2", "World")]
    conv_store = _ConversationStore(
        conversations={"conv_src": conv},
        items_by_conv={"conv_src": items},
    )
    agent_store = _AgentStore(
        agents={
            "ag_test": Agent(
                id="ag_test",
                created_at=1,
                name="test-agent",
                bundle_location="ag_test/fakehash",
                version=1,
                description="A test agent",
            ),
        }
    )
    client = TestClient(_build_app(conv_store, agent_store=agent_store))

    resp = client.post("/v1/sessions/conv_src/fork", json={"title": "My Fork"})

    assert resp.status_code == 201, f"Expected 201 Created, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["id"] == "conv_forked"
    # The agent_id should be the cloned agent, NOT the original.
    assert body["agent_id"] != "ag_test", (
        "Fork should be bound to a cloned agent, not the source's agent"
    )
    assert body["agent_id"].startswith("ag_"), "Cloned agent ID must use the ag_ prefix"
    assert body["status"] == "idle", "Freshly forked session should be idle"
    # 2 items copied from the source — proves the store's items were
    # included in the response, not an empty list.
    assert len(body["items"]) == 2, f"Expected 2 items (matching source), got {len(body['items'])}"
    # Verify item content survived the copy — if the route returns empty
    # shells the client loses conversation history.
    item_texts = [
        part["text"]
        for item in body["items"]
        for part in item.get("data", {}).get("content", [])
        if part.get("type") == "input_text"
    ]
    assert item_texts == ["Hello", "World"], (
        f"Copied items should preserve content and order, got {item_texts}"
    )
    assert body["title"] == "My Fork"

    # The agent clone is created INSIDE fork_conversation (atomically), not
    # via a separate agent_store.create — a pre-created row would leak as a
    # phantom built-in on a fork failure. So the route must NOT pre-create,
    # and must hand the clone's bundle/description to the store instead.
    assert len(agent_store.create_calls) == 0, (
        "Route must not pre-create the clone; it's created atomically in the fork txn"
    )

    # Exactly 1 store fork — more means the route called fork_conversation
    # multiple times; 0 means it never forked.
    assert len(conv_store.fork_calls) == 1
    fork_call = conv_store.fork_calls[0]
    assert fork_call["source"] == "conv_src"
    assert fork_call["title"] == "My Fork"
    # The store receives the clone's bundle/name/description so it can mint
    # the session-scoped agent row in the same transaction.
    assert fork_call["cloned_agent_bundle_location"] == "ag_test/fakehash"
    assert fork_call["cloned_agent_description"] == "A test agent"
    # The clone keeps the source's ROOT name — no "(fork …)" suffix. Being
    # session-scoped it's exempt from the unique built-in-name index, so no
    # disambiguator is needed and the name matches its origin directly.
    assert fork_call["cloned_agent_name"] == "test-agent", (
        f"Cloned agent should keep the source's root name, got {fork_call['cloned_agent_name']!r}"
    )
    assert fork_call["agent_id"] == body["agent_id"], (
        "Fork must bind the same cloned agent id it asked the store to create"
    )


@pytest.mark.asyncio
async def test_fork_session_up_to_response_id_passes_through_and_truncates() -> None:
    """``up_to_response_id`` reaches the store and the response is truncated.

    The route must forward the request field to
    ``fork_conversation`` verbatim — dropping it would silently fork
    the full history — and the returned session must contain only the
    items up to the selected response.
    """
    conv = _make_conversation()
    items = [
        _make_item("msg_1", "Q1", response_id="resp_001"),
        _make_item("msg_2", "A1", response_id="resp_001"),
        _make_item("msg_3", "Q2", response_id="resp_002"),
    ]
    conv_store = _ConversationStore(
        conversations={"conv_src": conv},
        items_by_conv={"conv_src": items},
    )
    client = TestClient(_build_app(conv_store))

    resp = client.post(
        "/v1/sessions/conv_src/fork",
        json={"up_to_response_id": "resp_001"},
    )

    assert resp.status_code == 201, f"Expected 201 Created, got {resp.status_code}: {resp.text}"
    # The route forwarded the truncation point to the store — None here
    # means the request field was dropped and the fork copied everything.
    assert conv_store.fork_calls[0]["up_to_response_id"] == "resp_001"
    body = resp.json()
    # Only resp_001's two items survive the truncation; msg_3 (resp_002)
    # appearing means the store ignored the cutoff.
    assert [item["response_id"] for item in body["items"]] == ["resp_001", "resp_001"], (
        f"Fork should contain only resp_001 items, got {body['items']!r}"
    )


@pytest.mark.asyncio
async def test_fork_session_400_unknown_up_to_response_id() -> None:
    """An ``up_to_response_id`` matching no response returns 400.

    The store raises ValueError for an unknown response id (stale
    client state); the route must surface it as ``invalid_input``
    rather than a 500 or a silent full-history fork.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(
        conversations={"conv_src": conv},
        items_by_conv={"conv_src": [_make_item("msg_1", "Q1")]},
    )
    client = TestClient(_build_app(conv_store))

    resp = client.post(
        "/v1/sessions/conv_src/fork",
        json={"up_to_response_id": "resp_nope"},
    )

    assert resp.status_code == 400, (
        f"Expected 400 for unknown response id, got {resp.status_code}: {resp.text}"
    )
    error = resp.json().get("error", {})
    assert error.get("code") == "invalid_input", f"Expected 'invalid_input', got {error}"


@pytest.mark.asyncio
async def test_fork_session_404_missing_source() -> None:
    """POST /sessions/{id}/fork returns 404 when source doesn't exist.

    If the route silently creates an empty fork instead of 404, the
    client loses the original conversation's context.
    """
    store = _ConversationStore(conversations={})
    client = TestClient(_build_app(store))

    resp = client.post("/v1/sessions/conv_missing/fork", json={})

    # The route should return 404, not 500 or 201.
    assert resp.status_code == 404, (
        f"Expected 404 for missing source, got {resp.status_code}: {resp.text}"
    )
    # Verify the error body contains a structured error so clients can
    # distinguish "not found" from generic failures.
    error = resp.json().get("error", {})
    assert error.get("code") == "not_found", f"Expected error code 'not_found', got {error}"


@pytest.mark.asyncio
async def test_fork_session_400_sub_agent() -> None:
    """POST /sessions/{id}/fork returns 400 when source is a sub-agent session.

    Sub-agent conversations are internal execution artifacts owned by a
    parent session. Forking one would produce a zombie that appears in
    the parent's ``/children`` list and might collide with the
    ``(parent_conversation_id, title)`` uniqueness index. The fork
    endpoint must reject them.
    """
    conv = _make_conversation(kind="sub_agent")
    store = _ConversationStore(conversations={"conv_src": conv})
    client = TestClient(_build_app(store))

    resp = client.post("/v1/sessions/conv_src/fork", json={})

    assert resp.status_code == 400, (
        f"Expected 400 for sub-agent source, got {resp.status_code}: {resp.text}"
    )
    error = resp.json().get("error", {})
    assert "sub-agent" in error.get("message", "").lower(), (
        f"Error message should mention 'sub-agent', got: {error}"
    )


@pytest.mark.asyncio
async def test_fork_session_400_no_agent_binding() -> None:
    """POST /sessions/{id}/fork returns 400 when source has no agent_id.

    A conversation without an agent binding is not a session — the
    fork route must reject it so the client doesn't end up with an
    orphaned fork.
    """
    conv = _make_conversation(agent_id=None)
    store = _ConversationStore(conversations={"conv_src": conv})
    client = TestClient(_build_app(store))

    resp = client.post("/v1/sessions/conv_src/fork", json={})

    # 400 for invalid request (no agent binding).
    assert resp.status_code == 400, (
        f"Expected 400 for no agent binding, got {resp.status_code}: {resp.text}"
    )
    # Verify the error body explains the rejection reason so clients
    # can surface a meaningful message.
    error = resp.json().get("error", {})
    assert "agent" in error.get("message", "").lower(), (
        f"Error message should mention 'agent' binding, got: {error}"
    )


# ── Agent-switch on fork ─────────────────────────────────────────


class _StubLoadedSpec:
    """Minimal stand-in for ``LoadedAgent.spec`` exposing harness_kind.

    The route's ``_agent_provider_family`` / ``_agent_is_native`` only read
    ``spec.executor.harness_kind``; this real (not MagicMock) stub returns a
    controlled value so the family/native logic runs on a known harness.

    :param harness_kind: The harness id to expose, e.g. ``"claude-native"``.
    """

    def __init__(self, harness_kind: str) -> None:
        """:param harness_kind: Harness id, e.g. ``"claude_sdk"``."""

        class _Executor:
            def __init__(self, hk: str) -> None:
                self.harness_kind = hk

        self.executor = _Executor(harness_kind)


class _StubLoadedAgent:
    """Stand-in for ``AgentCache.load(...)`` result; carries ``.spec``."""

    def __init__(self, harness_kind: str) -> None:
        """:param harness_kind: Harness id to expose on the spec."""
        self.spec = _StubLoadedSpec(harness_kind)


class _StubAgentCache:
    """Agent cache stub mapping agent_id → harness_kind.

    :param harness_by_id: Map of agent_id → harness_kind to return from
        ``load``, e.g. ``{"ag_test": "claude_sdk"}``.
    """

    def __init__(self, harness_by_id: dict[str, str]) -> None:
        """:param harness_by_id: agent_id → harness_kind map."""
        self._harness = harness_by_id

    def load(
        self,
        agent_id: str,
        bundle_location: str,
        *,
        expand_env: bool = False,
    ) -> _StubLoadedAgent:
        """
        Return a loaded-agent stub for *agent_id*.

        :param agent_id: Agent id to resolve, e.g. ``"ag_codex"``.
        :param bundle_location: Ignored — the stub keys on agent_id.
        :param expand_env: Ignored — accepted to match the real
            ``AgentCache.load`` signature (this kwarg exists;
            callers pass ``expand_env=agent.session_id is None``). The
            stub returns a fixed harness regardless, but it must accept
            the kwarg or the call raises ``TypeError``, which
            ``_agent_is_native`` swallows and misreports the harness.
        :returns: A :class:`_StubLoadedAgent` with the mapped harness.
        :raises KeyError: If *agent_id* has no mapped harness (a test
            setup error — fail loud rather than silently treating the
            agent as unknown-family).
        """
        del bundle_location, expand_env
        return _StubLoadedAgent(self._harness[agent_id])


def _switch_agent_store() -> _AgentStore:
    """Build an agent store with a source agent and switchable targets.

    :returns: A store holding ``ag_test`` (source), ``ag_claude_native``
        and ``ag_codex_native`` (bindable built-ins), and
        ``ag_session_scoped`` (a session-scoped agent that must be
        rejected as a switch target).
    """
    return _AgentStore(
        agents={
            "ag_test": Agent(
                id="ag_test",
                created_at=1,
                name="source-agent",
                bundle_location="ag_test/hash",
                version=1,
            ),
            "ag_claude_native": Agent(
                id="ag_claude_native",
                created_at=1,
                name="claude-code",
                bundle_location="ag_claude_native/hash",
                version=1,
            ),
            "ag_codex_native": Agent(
                id="ag_codex_native",
                created_at=1,
                name="codex",
                bundle_location="ag_codex_native/hash",
                version=1,
            ),
            "ag_session_scoped": Agent(
                id="ag_session_scoped",
                created_at=1,
                name="scoped",
                bundle_location="ag_session_scoped/hash",
                version=1,
                session_id="conv_other",
            ),
        }
    )


@pytest.mark.asyncio
async def test_fork_switch_binds_target_agent_bundle() -> None:
    """Switching agent clones the TARGET's bundle, not the source's.

    With ``agent_id`` set to a different built-in, the fork must clone
    that agent's bundle into the new session-scoped row. If the route
    cloned the source's bundle instead, the fork would run the wrong
    harness — defeating the switch.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(
        conversations={"conv_src": conv},
        items_by_conv={"conv_src": [_make_item("msg_1", "Hello")]},
    )
    agent_store = _switch_agent_store()
    client = TestClient(_build_app(conv_store, agent_store=agent_store))

    resp = client.post(
        "/v1/sessions/conv_src/fork",
        json={"agent_id": "ag_codex_native"},
    )

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    # The clone is minted inside fork_conversation, so the route hands it the
    # TARGET agent's bundle (not ag_test/hash) — not a separate create call.
    assert len(agent_store.create_calls) == 0
    fork_call = conv_store.fork_calls[0]
    assert fork_call["cloned_agent_bundle_location"] == "ag_codex_native/hash", (
        "Switch must clone the target agent's bundle; cloning the source's "
        "bundle would launch the wrong harness."
    )
    # Clone keeps the TARGET agent's root name ("codex"), proving the
    # response reflects the bound (switched) agent — not the source.
    assert fork_call["cloned_agent_name"] == "codex"
    # The fork binds the cloned agent id it asked the store to create.
    assert fork_call["agent_id"] is not None
    assert fork_call["agent_id"] != "ag_test"


@pytest.mark.asyncio
async def test_fork_switch_404_session_scoped_target() -> None:
    """Switching to a session-scoped agent is rejected with 404.

    A session-scoped agent (``session_id`` set) belongs to one
    conversation — possibly another user's. Binding it to a fork would
    leak/alias it across sessions, so only built-in agents are bindable.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(conversations={"conv_src": conv})
    client = TestClient(_build_app(conv_store, agent_store=_switch_agent_store()))

    resp = client.post(
        "/v1/sessions/conv_src/fork",
        json={"agent_id": "ag_session_scoped"},
    )

    assert resp.status_code == 404, (
        f"Expected 404 for session-scoped target, got {resp.status_code}: {resp.text}"
    )
    # No fork happened — the route rejected before cloning/forking.
    assert conv_store.fork_calls == []


@pytest.mark.asyncio
async def test_fork_switch_404_unknown_target() -> None:
    """Switching to a non-existent agent id is rejected with 404."""
    conv = _make_conversation()
    conv_store = _ConversationStore(conversations={"conv_src": conv})
    client = TestClient(_build_app(conv_store, agent_store=_switch_agent_store()))

    resp = client.post(
        "/v1/sessions/conv_src/fork",
        json={"agent_id": "ag_does_not_exist"},
    )

    assert resp.status_code == 404, (
        f"Expected 404 for unknown target, got {resp.status_code}: {resp.text}"
    )
    assert conv_store.fork_calls == []


@pytest.mark.parametrize(
    "source_harness,target_harness,expect_copy_model,expect_carry,"
    "expect_resume_source,expect_presentation",
    [
        # SDK → native, same provider family: model settings carry AND the
        # fork is marked for native transcript rebuild (the headline case).
        # The clone becomes terminal-first (claude-code-native-ui).
        (
            "claude_sdk",
            "claude-native",
            True,
            True,
            True,
            {"omnigent.ui": "terminal", "omnigent.wrapper": "claude-code-native-ui"},
        ),
        # cross-family into a native target: model id is meaningless across
        # providers → reset. History still carries — the runner rebuilds the
        # native transcript from the copied Omnigent items — but the source's
        # native session id must NOT be stamped (wrong transcript format for
        # the target; a doomed clone attempt would launch fresh instead).
        # Still terminal-first, but the codex wrapper.
        (
            "claude_sdk",
            "codex-native",
            False,
            True,
            False,
            {"omnigent.ui": "terminal", "omnigent.wrapper": "codex-native-ui"},
        ),
        # cursor target carries history via a text preamble (its conversation
        # is server-backed, so the runner can't seed a local store for --resume),
        # so carry_history_into_native IS stamped — the runner branches on the
        # harness to choose preamble vs transcript rebuild.
        (
            "claude_sdk",
            "cursor-native",
            False,
            True,
            False,
            {"omnigent.ui": "terminal", "omnigent.wrapper": "cursor-native-ui"},
        ),
        # pi-native CAN carry fork history: the runner rebuilds Pi's JSONL
        # session file from the copied Omnigent items. Cross-family from a
        # claude SDK source, so model settings reset and the source's native
        # session id is NOT stamped (Pi rebuilds from items, not a source
        # file) — same shape as the codex-native cross-family case.
        (
            "claude_sdk",
            "pi-native",
            False,
            True,
            False,
            {"omnigent.ui": "terminal", "omnigent.wrapper": "pi-native-ui"},
        ),
        # qwen-native CAN carry fork history: the runner rebuilds qwen's on-disk
        # chat recording (+ runtime/meta sidecars) from the copied Omnigent items
        # (see write_qwen_session_recording). Cross-family here (claude SDK source
        # is anthropic, qwen is openai-family), so model settings reset and the
        # source's native session id is NOT stamped — same shape as the pi-native
        # cross-family case.
        (
            "claude_sdk",
            "qwen-native",
            False,
            True,
            False,
            {"omnigent.ui": "terminal", "omnigent.wrapper": "qwen-native-ui"},
        ),
        # native → SDK, same family: model carries, but an SDK target
        # replays the transcript itself so no native-rebuild marker is set.
        # The clone drops terminal-first mode (chat) — the bug this fixes.
        ("claude-native", "claude_sdk", True, False, True, {}),
        # cross-family into native (openai source → anthropic native): reset
        # model settings, carry history via rebuild-from-items, skip the
        # source-session directive — same as the SDK cross-family case.
        # Terminal-first.
        (
            "openai-agents",
            "claude-native",
            False,
            True,
            False,
            {"omnigent.ui": "terminal", "omnigent.wrapper": "claude-code-native-ui"},
        ),
    ],
)
@pytest.mark.asyncio
async def test_fork_switch_model_and_carry_gating(
    monkeypatch: pytest.MonkeyPatch,
    source_harness: str,
    target_harness: str,
    expect_copy_model: bool,
    expect_carry: bool,
    expect_resume_source: bool,
    expect_presentation: dict[str, str],
) -> None:
    """The switch gates model copy + native carry + UI mode on the target.

    A model id is provider-bound, so ``copy_model_settings`` must be True
    only within a family. ``carry_history_into_native`` must be True for
    native targets that carry fork history (claude/codex/pi rebuild a
    transcript, cursor replays a text preamble), and SDK targets replay
    history themselves so they never set it. ``resume_source_native_session``
    must be False on a cross-family switch so the store skips the fork-source
    directive (the source's native transcript is the wrong format; a clone
    attempt would fail and launch fresh). ``presentation_labels`` must reflect the TARGET
    harness so the clone's UI mode is right — an SDK target drops
    terminal-first mode (``{}``), a native target sets it; copying the
    source's would leave an SDK clone of a native session with a stale
    interactive terminal.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(
        conversations={"conv_src": conv},
        items_by_conv={"conv_src": [_make_item("msg_1", "Hi")]},
    )
    agent_store = _switch_agent_store()
    # Target every switch at ag_claude_native; the stub cache, not the
    # bundle, dictates the harness each agent reports.
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_agent_cache",
        lambda: _StubAgentCache({"ag_test": source_harness, "ag_claude_native": target_harness}),
    )
    client = TestClient(_build_app(conv_store, agent_store=agent_store))

    resp = client.post(
        "/v1/sessions/conv_src/fork",
        json={"agent_id": "ag_claude_native"},
    )

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    fork_call = conv_store.fork_calls[0]
    assert fork_call["copy_model_settings"] is expect_copy_model, (
        f"{source_harness}->{target_harness}: copy_model_settings should be "
        f"{expect_copy_model} (model id is provider-bound)."
    )
    assert fork_call["carry_history_into_native"] is expect_carry, (
        f"{source_harness}->{target_harness}: carry_history_into_native should "
        f"be {expect_carry} (only native harnesses with replayable fork history)."
    )
    assert fork_call["resume_source_native_session"] is expect_resume_source, (
        f"{source_harness}->{target_harness}: resume_source_native_session should "
        f"be {expect_resume_source} (the source's native session id is only "
        f"resumable within the same provider family)."
    )
    assert fork_call["presentation_labels"] == expect_presentation, (
        f"{source_harness}->{target_harness}: presentation_labels should be "
        f"{expect_presentation} so the clone's UI mode matches the target "
        f"harness, got {fork_call['presentation_labels']!r}."
    )


@pytest.mark.asyncio
async def test_fork_no_switch_native_source_carries_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-agent fork of a native source still marks native carry.

    Without an ``agent_id`` the fork keeps the source's (native) agent, so
    the runner must still rebuild the native transcript — otherwise a plain
    clone of a Claude-Code session would resume with no history. Model
    settings always copy on a same-agent fork.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(
        conversations={"conv_src": conv},
        items_by_conv={"conv_src": [_make_item("msg_1", "Hi")]},
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_agent_cache",
        lambda: _StubAgentCache({"ag_test": "claude-native"}),
    )
    client = TestClient(_build_app(conv_store))

    resp = client.post("/v1/sessions/conv_src/fork", json={})

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    fork_call = conv_store.fork_calls[0]
    assert fork_call["copy_model_settings"] is True
    assert fork_call["carry_history_into_native"] is True, (
        "A same-agent fork of a native source must mark native carry so the "
        "runner rebuilds the transcript instead of resuming blank."
    )
    # Not switching → keep the source's copied UI labels untouched.
    assert fork_call["presentation_labels"] is None, (
        "A same-agent fork must not recompute presentation labels (None); "
        "the copied source labels are already correct."
    )


@pytest.mark.parametrize(
    "harness,expect_carry",
    [
        # cursor carries history via a text preamble the runner replays on the
        # first message (its conversation is server-backed, so no local store to
        # seed for --resume) — so a same-agent fork DOES mark native carry.
        ("cursor-native", True),
        # pi rebuilds its JSONL session file from the copied Omnigent items
        # (it is in _FORK_HISTORY_NATIVE_HARNESSES), so a same-agent fork marks
        # native carry — parity with claude/codex.
        ("pi-native", True),
    ],
)
@pytest.mark.asyncio
async def test_fork_cursor_pi_native_carry_gating(
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    expect_carry: bool,
) -> None:
    """A same-agent fork marks native carry for both cursor and pi.

    cursor carries fork history via a text preamble (its conversation is
    server-backed, so no local store to seed for --resume); pi rebuilds its
    JSONL session file from the copied Omnigent items. Both therefore mark
    ``carry_history_into_native``.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(
        conversations={"conv_src": conv},
        items_by_conv={"conv_src": [_make_item("msg_1", "Hi")]},
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_agent_cache",
        lambda: _StubAgentCache({"ag_test": harness}),
    )
    client = TestClient(_build_app(conv_store))

    resp = client.post("/v1/sessions/conv_src/fork", json={})

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    fork_call = conv_store.fork_calls[0]
    assert fork_call["carry_history_into_native"] is expect_carry, (
        f"A {harness} fork should set carry_history_into_native={expect_carry}."
    )


@pytest.mark.parametrize(
    "harness,expect_carry",
    [
        # Reversed native spellings ("native-claude" / "native-codex") are
        # valid harness ids that canonicalize_harness passes through unchanged,
        # so the carry gate must recognize them just like their canonical
        # spellings — otherwise an identically-behaving agent silently loses
        # fork history. cursor carries (preamble); ``native-pi`` IS aliased to
        # ``pi-native`` (which is in the set), so it carries too (rebuild).
        ("native-claude", True),
        ("native-codex", True),
        ("native-cursor", True),
        ("native-pi", True),
    ],
)
@pytest.mark.asyncio
async def test_fork_reversed_native_spelling_carry_gating(
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    expect_carry: bool,
) -> None:
    """The carry gate honors reversed native spellings like the canonical ones.

    ``canonicalize_harness`` aliases ``native-pi`` to ``pi-native``; the other
    reversed spellings pass through unchanged, so the predicate lists both
    forms explicitly. claude/codex/cursor/pi all carry fork history (claude /
    codex / pi via transcript rebuild, cursor via preamble).
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(
        conversations={"conv_src": conv},
        items_by_conv={"conv_src": [_make_item("msg_1", "Hi")]},
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_agent_cache",
        lambda: _StubAgentCache({"ag_test": harness}),
    )
    client = TestClient(_build_app(conv_store))

    resp = client.post("/v1/sessions/conv_src/fork", json={})

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    fork_call = conv_store.fork_calls[0]
    assert fork_call["carry_history_into_native"] is expect_carry, (
        f"A {harness} fork should set carry_history_into_native={expect_carry}: "
        "reversed native spellings must be treated like their canonical form."
    )


@pytest.mark.asyncio
async def test_fork_clone_reuses_source_agent_name_verbatim() -> None:
    """The fork clone reuses the source agent's name as-is — no suffix added."""
    conv = _make_conversation(agent_id="ag_src")
    conv_store = _ConversationStore(
        conversations={"conv_src": conv},
        items_by_conv={"conv_src": [_make_item("msg_1", "Hi")]},
    )
    agent_store = _AgentStore(
        agents={
            "ag_src": Agent(
                id="ag_src",
                created_at=1,
                name="claude-native-ui",
                bundle_location="ag_claude/hash",
                version=1,
            ),
        }
    )
    client = TestClient(_build_app(conv_store, agent_store=agent_store))

    resp = client.post("/v1/sessions/conv_src/fork", json={})

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    assert conv_store.fork_calls[0]["cloned_agent_name"] == "claude-native-ui", (
        "Fork clone should reuse the source name verbatim, no '(fork …)' suffix"
    )
