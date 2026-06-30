"""Tests for ``POST /v1/sessions/{id}/switch-agent``.

Exercises the in-place agent-switch endpoint: validation (404 missing
session, 400 sub-agent / no binding / unloadable bundle, 404 non-bindable
target, 409 while busy) and the happy-path wiring (it clones the target,
computes the same-family model/history/label deltas, and forwards them to
``switch_conversation_agent``). Real-type store stubs — no MagicMock.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from omnigent.entities import Agent, Conversation, ConversationItem, MessageData, PagedList
from omnigent.errors import OmnigentError
from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes.sessions import create_sessions_router

# ── Stubs ────────────────────────────────────────────────────────


class _AgentStore:
    """Agent store stub: get + list for the switch route.

    :param agents: Pre-populated map of agent_id → Agent.
    """

    def __init__(self, agents: dict[str, Agent]) -> None:
        self._agents = dict(agents)

    def get(self, agent_id: str) -> Agent | None:
        """:returns: The agent if present, else None."""
        return self._agents.get(agent_id)

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
    """Conversation store stub for the switch route.

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
        self.switch_calls: list[dict[str, Any]] = []

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """:returns: The conversation if present, else None."""
        return self._convs.get(conversation_id)

    def switch_conversation_agent(
        self,
        conversation_id: str,
        *,
        new_agent_id: str,
        new_agent_name: str,
        new_agent_bundle_location: str,
        new_agent_description: str | None,
        copy_model_settings: bool,
        carry_history_into_native: bool,
        presentation_labels: dict[str, str],
        previous_builtin_id: str | None,
    ) -> Conversation:
        """Record the call and return the updated conversation.

        :param conversation_id: Session being switched.
        :param new_agent_id: Cloned agent id the route generated.
        :param new_agent_name: Cloned agent name.
        :param new_agent_bundle_location: Target bundle to clone.
        :param new_agent_description: Target description.
        :param copy_model_settings: Same-family flag from the route.
        :param carry_history_into_native: Native-rebuild flag.
        :param presentation_labels: Target-harness ui/wrapper labels.
        :param previous_builtin_id: Built-in switched away from.
        :returns: The conversation rebound to ``new_agent_id``.
        :raises LookupError: If the conversation is unknown.
        """
        self.switch_calls.append(
            {
                "conversation_id": conversation_id,
                "new_agent_id": new_agent_id,
                "new_agent_name": new_agent_name,
                "new_agent_bundle_location": new_agent_bundle_location,
                "new_agent_description": new_agent_description,
                "copy_model_settings": copy_model_settings,
                "carry_history_into_native": carry_history_into_native,
                "presentation_labels": presentation_labels,
                "previous_builtin_id": previous_builtin_id,
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
            agent_id=new_agent_id,
            title=src.title,
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
        unloadable target bundle (the route maps this to 400).
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
        :param expand_env: Ignored — accepted to match the real
            ``AgentCache.load`` signature. Without it the
            route's keyword call would raise ``TypeError``.
        :returns: A sentinel object (the route ignores the value).
        :raises RuntimeError: When configured to fail.
        """
        del agent_id, bundle_location, expand_env
        if self._raise:
            raise RuntimeError("bundle missing")
        return object()


class _LoadedAgentStub:
    """Loaded-agent stub exposing ``spec.executor.harness_kind``."""

    def __init__(self, harness_kind: str) -> None:
        class _Executor:
            def __init__(self, hk: str) -> None:
                self.harness_kind = hk

        class _Spec:
            def __init__(self, hk: str) -> None:
                self.executor = _Executor(hk)

        self.spec = _Spec(harness_kind)


class _HarnessAgentCacheStub:
    """Agent cache stub mapping agent id to harness kind."""

    def __init__(self, harness_by_id: dict[str, str]) -> None:
        self._harness_by_id = harness_by_id

    def load(
        self,
        agent_id: str,
        bundle_location: str,
        *,
        expand_env: bool = False,
    ) -> _LoadedAgentStub:
        del bundle_location, expand_env
        return _LoadedAgentStub(self._harness_by_id[agent_id])


# ── Helpers ──────────────────────────────────────────────────────


def _conv(
    conv_id: str = "conv_src",
    agent_id: str | None = "ag_session_scoped",
    kind: str = "default",
) -> Conversation:
    """Build a Conversation entity.

    :param conv_id: Conversation id.
    :param agent_id: Bound agent id, or None.
    :param kind: ``"default"`` or ``"sub_agent"``.
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


def _patch_family_helpers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    same_family: bool,
    native: bool,
    labels: dict[str, str],
    raise_on_load: bool = False,
) -> None:
    """Stub the bundle-loading helpers so the route runs without a real
    bundle, with controlled family/native/label outcomes.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param same_family: Value ``_same_provider_family`` returns.
    :param native: Value ``_agent_is_native`` /
        ``_agent_carries_native_fork_history`` return (these switch tests
        target claude/codex native, which both classify and carry history).
    :param labels: Value ``_presentation_labels_for_agent`` returns.
    :param raise_on_load: Whether the bundle precheck should fail.
    """
    monkeypatch.setattr(
        sessions_mod, "get_agent_cache", lambda: _AgentCacheStub(raise_on_load=raise_on_load)
    )
    monkeypatch.setattr(sessions_mod, "_same_provider_family", lambda a, b: same_family)
    monkeypatch.setattr(sessions_mod, "_agent_is_native", lambda a: native)
    monkeypatch.setattr(sessions_mod, "_agent_carries_native_fork_history", lambda a: native)
    monkeypatch.setattr(sessions_mod, "_presentation_labels_for_agent", lambda a: labels)


# The session's currently-bound agent is a session-scoped clone of the
# claude-sdk built-in (shares its bundle_location). ``_BUILTIN_ORIGIN`` is
# that built-in — what "switch back" resolves to, and the no-op target the
# route now rejects (same bundle as the current agent). The switch *targets*
# are built-ins with a DIFFERENT bundle.
_CURRENT = _agent("ag_session_scoped", "claude (switch src)", "bundle/claude-sdk", "conv_src")
_BUILTIN_ORIGIN = _agent("ag_builtin_origin", "claude", "bundle/claude-sdk", None)
_BUILTIN_CLAUDE = _agent("ag_builtin_claude", "claude-native-ui", "bundle/claude-native", None)
_BUILTIN_CODEX = _agent("ag_builtin_codex", "codex-native-ui", "bundle/codex", None)
_BUILTIN_CURSOR = _agent("ag_builtin_cursor", "cursor-native-ui", "bundle/cursor", None)
_BUILTIN_PI = _agent("ag_builtin_pi", "pi-native-ui", "bundle/pi", None)
_BUILTIN_QWEN = _agent("ag_builtin_qwen", "qwen-native-ui", "bundle/qwen", None)


# ── Tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_switch_same_family_native_carries_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-family native target keeps model settings, marks the native
    rebuild, applies target labels, and resolves the previous built-in.
    """
    conv_store = _ConversationStore(
        conversations={"conv_src": _conv()},
        items_by_conv={
            "conv_src": [
                ConversationItem(
                    id="msg_1",
                    type="message",
                    status="completed",
                    response_id="r1",
                    created_at=1,
                    data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
                )
            ]
        },
    )
    agent_store = _AgentStore(
        {
            "ag_session_scoped": _CURRENT,
            "ag_builtin_claude": _BUILTIN_CLAUDE,
            "ag_builtin_origin": _BUILTIN_ORIGIN,
        }
    )
    labels = {"omnigent.ui": "terminal", "omnigent.wrapper": "claude-code-native-ui"}
    _patch_family_helpers(monkeypatch, same_family=True, native=True, labels=labels)
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(
        "/v1/sessions/conv_src/switch-agent", json={"agent_id": "ag_builtin_claude"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "idle"
    # Bound to a freshly cloned session-scoped agent, not the built-in itself.
    assert body["agent_id"].startswith("ag_") and body["agent_id"] != "ag_builtin_claude"
    # 1 item returned — the in-place transcript is preserved (not copied/empty).
    assert len(body["items"]) == 1

    assert len(conv_store.switch_calls) == 1, "route must call switch exactly once"
    call = conv_store.switch_calls[0]
    assert call["conversation_id"] == "conv_src"
    assert call["new_agent_bundle_location"] == "bundle/claude-native"
    # Same family → keep model settings AND carry native history (target native).
    assert call["copy_model_settings"] is True
    assert call["carry_history_into_native"] is True
    assert call["presentation_labels"] == labels
    # The origin built-in shares the current agent's bundle → that's the
    # built-in to offer for "Switch back" (NOT the switched-to target).
    assert call["previous_builtin_id"] == "ag_builtin_origin"


@pytest.mark.asyncio
async def test_switch_cross_family_resets_model_but_carries_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cross-family switch resets model settings but still carries history
    into a native target.

    The model id is provider-bound so it must reset; history is NOT — the
    switch clears ``external_session_id`` and the runner rebuilds the
    native transcript from this session's own Omnigent items, a conversion
    that doesn't depend on the source harness.
    """
    conv_store = _ConversationStore(conversations={"conv_src": _conv()})
    agent_store = _AgentStore({"ag_session_scoped": _CURRENT, "ag_builtin_codex": _BUILTIN_CODEX})
    # native=True with same_family=False → model resets, history still carries.
    _patch_family_helpers(monkeypatch, same_family=False, native=True, labels={})
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post("/v1/sessions/conv_src/switch-agent", json={"agent_id": "ag_builtin_codex"})

    assert resp.status_code == 200, resp.text
    call = conv_store.switch_calls[0]
    # Cross-family → reset model settings (a model id is provider-bound).
    assert call["copy_model_settings"] is False
    # Native target carries history regardless of family: the runner
    # rebuilds the transcript from Omnigent items. False here would mean
    # the cross-family gate regressed and the session resumes blank.
    assert call["carry_history_into_native"] is True


@pytest.mark.parametrize(
    "target_agent,target_harness,expected_labels,expected_carry",
    [
        # cursor-native has no resumable session file to rebuild → no carry.
        (
            _BUILTIN_CURSOR,
            "cursor-native",
            {"omnigent.ui": "terminal", "omnigent.wrapper": "cursor-native-ui"},
            False,
        ),
        # pi-native rebuilds its JSONL session file from the copied items →
        # carry history (parity with claude/codex native).
        (
            _BUILTIN_PI,
            "pi-native",
            {"omnigent.ui": "terminal", "omnigent.wrapper": "pi-native-ui"},
            True,
        ),
        # qwen-native rebuilds qwen's on-disk chat recording from the copied
        # items → carry history (parity with claude/codex/pi native).
        (
            _BUILTIN_QWEN,
            "qwen-native",
            {"omnigent.ui": "terminal", "omnigent.wrapper": "qwen-native-ui"},
            True,
        ),
    ],
)
@pytest.mark.asyncio
async def test_switch_cursor_pi_native_targets_carry_history_gating(
    monkeypatch: pytest.MonkeyPatch,
    target_agent: Agent,
    target_harness: str,
    expected_labels: dict[str, str],
    expected_carry: bool,
) -> None:
    """Switching into cursor/pi native keeps terminal UI; carry gates per harness.

    Cross-family from a claude SDK source so ``copy_model_settings`` is always
    False. ``carry_history_into_native`` is True only for the harness with a
    rebuildable session file (pi-native), False for cursor-native.
    """
    conv_store = _ConversationStore(conversations={"conv_src": _conv()})
    agent_store = _AgentStore(
        {
            "ag_session_scoped": _CURRENT,
            target_agent.id: target_agent,
            "ag_builtin_origin": _BUILTIN_ORIGIN,
        }
    )
    monkeypatch.setattr(
        sessions_mod,
        "get_agent_cache",
        lambda: _HarnessAgentCacheStub(
            {"ag_session_scoped": "claude_sdk", target_agent.id: target_harness}
        ),
    )
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post("/v1/sessions/conv_src/switch-agent", json={"agent_id": target_agent.id})

    assert resp.status_code == 200, resp.text
    call = conv_store.switch_calls[0]
    assert call["copy_model_settings"] is False
    assert call["carry_history_into_native"] is expected_carry, (
        f"{target_harness} carry_history_into_native should be {expected_carry}."
    )
    assert call["presentation_labels"] == expected_labels


@pytest.mark.asyncio
async def test_switch_400_noop_same_bundle() -> None:
    """Switching to the built-in the session already runs (same bundle) is a
    no-op and rejected with 400 — no store mutation.
    """
    conv_store = _ConversationStore(conversations={"conv_src": _conv()})
    # _BUILTIN_ORIGIN shares the current clone's bundle_location.
    agent_store = _AgentStore(
        {"ag_session_scoped": _CURRENT, "ag_builtin_origin": _BUILTIN_ORIGIN}
    )
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(
        "/v1/sessions/conv_src/switch-agent", json={"agent_id": "ag_builtin_origin"}
    )

    assert resp.status_code == 400, resp.text
    # No-op rejected before any mutation.
    assert conv_store.switch_calls == []


@pytest.mark.asyncio
async def test_switch_publishes_agent_changed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful switch publishes ``session.agent_changed`` on the session
    stream, carrying the cloned agent's id and the clean target-agent
    display name, so connected clients
    re-derive their binding-dependent state (the chat store's
    ``isNativeTerminalSession`` gate) without a navigation. No event → a
    bound web client keeps treating the session as the old harness and
    drops the first post-switch optimistic bubble on idle churn.
    """
    conv_store = _ConversationStore(conversations={"conv_src": _conv()})
    agent_store = _AgentStore(
        {"ag_session_scoped": _CURRENT, "ag_builtin_claude": _BUILTIN_CLAUDE}
    )
    _patch_family_helpers(monkeypatch, same_family=True, native=True, labels={})

    # Capture session-stream publishes by rebinding the module's
    # ``session_stream`` reference to a recorder (not patching ``publish``
    # through the shared module singleton) — omnigent-testing rule 14.
    published: list[dict[str, object]] = []

    class _RecordingStream:
        @staticmethod
        def publish(conversation_id: str, event: dict[str, object]) -> None:
            published.append({"_conversation_id": conversation_id, **event})

    monkeypatch.setattr(sessions_mod, "session_stream", _RecordingStream)
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(
        "/v1/sessions/conv_src/switch-agent", json={"agent_id": "ag_builtin_claude"}
    )

    assert resp.status_code == 200, resp.text
    changed = [e for e in published if e.get("type") == "session.agent_changed"]
    # Exactly one broadcast per switch. Zero means clients never learn about
    # the in-place rebind; more than one would double the snapshot refetch.
    assert len(changed) == 1, f"expected exactly one agent_changed event, got {published}"
    event = changed[0]
    # Published on the switched session's channel — a wrong id would deliver
    # the refresh signal to some other session's subscribers.
    assert event["_conversation_id"] == "conv_src"
    assert event["conversation_id"] == "conv_src"
    # agent_id must be the agent the store actually bound (the fresh
    # clone) — that's the durable reference clients re-bind to.
    call = conv_store.switch_calls[0]
    assert event["agent_id"] == call["new_agent_id"]
    # agent_name must be the clean target name, NOT the clone row's
    # "<name> (switch ag_…)" disambiguation name — clients display it
    # verbatim. The clone name leaking here is the web's
    # flash-of-ugly-name bug.
    assert event["agent_name"] == "claude-native-ui"
    assert event["agent_name"] != call["new_agent_name"]


@pytest.mark.asyncio
async def test_switch_rejected_publishes_no_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected switch (no-op same-bundle target, 400) publishes nothing —
    clients must not refetch a snapshot for a binding that didn't change.
    """
    conv_store = _ConversationStore(conversations={"conv_src": _conv()})
    # _BUILTIN_ORIGIN shares the current clone's bundle_location → 400.
    agent_store = _AgentStore(
        {"ag_session_scoped": _CURRENT, "ag_builtin_origin": _BUILTIN_ORIGIN}
    )
    published: list[dict[str, object]] = []

    class _RecordingStream:
        @staticmethod
        def publish(conversation_id: str, event: dict[str, object]) -> None:
            published.append({"_conversation_id": conversation_id, **event})

    monkeypatch.setattr(sessions_mod, "session_stream", _RecordingStream)
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(
        "/v1/sessions/conv_src/switch-agent", json={"agent_id": "ag_builtin_origin"}
    )

    assert resp.status_code == 400, resp.text
    # No mutation happened, so no broadcast may go out.
    assert published == []


@pytest.mark.asyncio
async def test_switch_schedules_runner_resource_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful switch schedules a runner-side resource reset so the new
    agent's os_env/sandbox governs the web filesystem/shell endpoints (the
    cached primary OSEnv is dropped) and no lingering native terminal shadows
    the next harness's transcript rebuild.
    """
    conv_store = _ConversationStore(conversations={"conv_src": _conv()})
    agent_store = _AgentStore(
        {"ag_session_scoped": _CURRENT, "ag_builtin_claude": _BUILTIN_CLAUDE}
    )
    _patch_family_helpers(monkeypatch, same_family=True, native=True, labels={})
    reset_calls: list[str] = []

    async def _record_reset(session_id: str) -> None:
        reset_calls.append(session_id)

    monkeypatch.setattr(sessions_mod, "_reset_runner_resources_after_switch", _record_reset)
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(
        "/v1/sessions/conv_src/switch-agent", json={"agent_id": "ag_builtin_claude"}
    )

    assert resp.status_code == 200, resp.text
    # The reset must be scheduled (and run, since TestClient drains background
    # tasks after the response) for exactly this session. An empty list means
    # the route stopped wiring the reset — the new agent's sandbox would never
    # take effect on the cached primary env and a stale terminal could shadow
    # the rebuild.
    assert reset_calls == ["conv_src"]


class _RunnerClientStub:
    """Async runner-HTTP-client stub recording reset-state POSTs.

    :param fail: When True, ``post`` raises ``httpx.ConnectError`` after
        recording the call, simulating a transport-level runner hiccup.
    :param status_code: HTTP status of the stubbed response, e.g. ``500``
        for a runner-side reset failure — httpx does NOT raise on it, so
        production must check it explicitly via ``raise_for_status``.
    """

    def __init__(self, fail: bool = False, status_code: int = 200) -> None:
        self._fail = fail
        self._status_code = status_code
        self.posts: list[str] = []

    async def post(self, url: str, timeout: float) -> httpx.Response:
        """Record the POST and return a real response of the stubbed status.

        :param url: Runner-relative URL, e.g.
            ``"/v1/sessions/conv_src/reset-state"``.
        :param timeout: Per-request timeout in seconds (unused).
        :returns: An ``httpx.Response`` so production's
            ``raise_for_status`` behaves exactly as on a live client.
        """
        del timeout
        self.posts.append(url)
        if self._fail:
            raise httpx.ConnectError("runner unreachable")
        return httpx.Response(
            self._status_code,
            request=httpx.Request("POST", f"http://runner.test{url}"),
        )


@pytest.mark.asyncio
async def test_switch_reset_publishes_changed_files_invalidated_after_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the post-switch runner reset succeeds, the route's background
    task publishes ``session.changed_files.invalidated`` so web clients
    refetch filesystem state — including environment availability, which
    flips the Files tab when the switch crosses an os_env boundary. The
    event must come AFTER the reset POST (and after ``agent_changed``):
    publishing before the reset would have clients refetch the OLD agent's
    still-cached env.
    """
    conv_store = _ConversationStore(conversations={"conv_src": _conv()})
    agent_store = _AgentStore(
        {"ag_session_scoped": _CURRENT, "ag_builtin_claude": _BUILTIN_CLAUDE}
    )
    _patch_family_helpers(monkeypatch, same_family=True, native=True, labels={})
    runner = _RunnerClientStub()

    async def _get_runner(session_id: str) -> _RunnerClientStub:
        del session_id
        return runner

    monkeypatch.setattr(sessions_mod, "_get_runner_client_for_resource_access", _get_runner)
    published: list[dict[str, object]] = []

    class _RecordingStream:
        @staticmethod
        def publish(conversation_id: str, event: dict[str, object]) -> None:
            published.append({"_conversation_id": conversation_id, **event})

    monkeypatch.setattr(sessions_mod, "session_stream", _RecordingStream)
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(
        "/v1/sessions/conv_src/switch-agent", json={"agent_id": "ag_builtin_claude"}
    )

    assert resp.status_code == 200, resp.text
    # The real reset ran (TestClient drains background tasks) and hit the
    # runner's dedicated endpoint — a different URL means the old env was
    # never closed and the event below would advertise a stale refetch.
    assert runner.posts == ["/v1/sessions/conv_src/reset-state"]
    types = [e["type"] for e in published]
    # Exactly one invalidation per switch; zero means the Files tab never
    # learns availability changed until the 60 s staleTime + a refetch
    # trigger.
    assert types.count("session.changed_files.invalidated") == 1, published
    # Ordering: agent_changed (at store-commit time) strictly before the
    # invalidation (after the reset). Reversed order would mean the
    # invalidation was published while the old env was still cached.
    assert types.index("session.agent_changed") < types.index("session.changed_files.invalidated")
    event = published[types.index("session.changed_files.invalidated")]
    assert event["_conversation_id"] == "conv_src"
    assert event["session_id"] == "conv_src"
    # The web client keys filesystem queries by the default environment.
    assert event["environment_id"] == "default"


@pytest.mark.parametrize(
    "getter_kind",
    [
        # Reset POST raises mid-flight (transport-level runner hiccup).
        "post_fails",
        # Reset POST returns non-2xx — httpx does NOT raise on this; the
        # route must check the status itself or it would publish against
        # a runner that never closed the old env.
        "post_500",
        # No runner client at all (session not runner-bound / unit setup).
        "no_client",
    ],
)
@pytest.mark.asyncio
async def test_switch_reset_failure_publishes_no_changed_files_event(
    monkeypatch: pytest.MonkeyPatch,
    getter_kind: str,
) -> None:
    """When the post-switch reset doesn't complete, NO
    ``session.changed_files.invalidated`` goes out: the runner's env cache
    is still the OLD agent's, so a triggered refetch would re-serve stale
    availability. (``agent_changed`` still fires — the store commit
    succeeded.)

    :param getter_kind: Which failure shape to simulate —
        ``"post_fails"`` (reset POST raises), ``"post_500"`` (reset POST
        returns HTTP 500), or ``"no_client"`` (no runner client resolved).
    """
    conv_store = _ConversationStore(conversations={"conv_src": _conv()})
    agent_store = _AgentStore(
        {"ag_session_scoped": _CURRENT, "ag_builtin_claude": _BUILTIN_CLAUDE}
    )
    _patch_family_helpers(monkeypatch, same_family=True, native=True, labels={})

    async def _get_runner(session_id: str) -> _RunnerClientStub | None:
        del session_id
        if getter_kind == "post_fails":
            return _RunnerClientStub(fail=True)
        if getter_kind == "post_500":
            return _RunnerClientStub(status_code=500)
        return None

    monkeypatch.setattr(sessions_mod, "_get_runner_client_for_resource_access", _get_runner)
    published: list[dict[str, object]] = []

    class _RecordingStream:
        @staticmethod
        def publish(conversation_id: str, event: dict[str, object]) -> None:
            published.append({"_conversation_id": conversation_id, **event})

    monkeypatch.setattr(sessions_mod, "session_stream", _RecordingStream)
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(
        "/v1/sessions/conv_src/switch-agent", json={"agent_id": "ag_builtin_claude"}
    )

    assert resp.status_code == 200, resp.text
    types = [e["type"] for e in published]
    # The switch itself committed, so the binding broadcast still goes out.
    assert types.count("session.agent_changed") == 1
    # But no filesystem invalidation: the old env wasn't closed, so a
    # refetch now would re-serve it; recovery is the client-side stale-mark
    # (focus/remount refetch) or the runner relaunching with fresh caches.
    assert types.count("session.changed_files.invalidated") == 0, published


@pytest.mark.asyncio
async def test_switch_404_missing_session() -> None:
    """404 when the session does not exist (before any mutation)."""
    conv_store = _ConversationStore(conversations={})
    agent_store = _AgentStore({})
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post("/v1/sessions/conv_missing/switch-agent", json={"agent_id": "ag_x"})

    assert resp.status_code == 404, resp.text
    assert conv_store.switch_calls == []


@pytest.mark.asyncio
async def test_switch_400_sub_agent() -> None:
    """400 when the session is a sub-agent (only top-level can switch)."""
    conv_store = _ConversationStore(conversations={"conv_src": _conv(kind="sub_agent")})
    agent_store = _AgentStore({"ag_session_scoped": _CURRENT})
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(
        "/v1/sessions/conv_src/switch-agent", json={"agent_id": "ag_builtin_claude"}
    )

    assert resp.status_code == 400, resp.text
    assert conv_store.switch_calls == []


@pytest.mark.asyncio
async def test_switch_404_target_not_bindable() -> None:
    """404 when the target is a session-scoped agent (not a built-in)."""
    other_session_agent = _agent("ag_other", "other", "bundle/x", "conv_other")
    conv_store = _ConversationStore(conversations={"conv_src": _conv()})
    agent_store = _AgentStore({"ag_session_scoped": _CURRENT, "ag_other": other_session_agent})
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post("/v1/sessions/conv_src/switch-agent", json={"agent_id": "ag_other"})

    # Session-scoped target → not bindable, mapped to 404, nothing mutated.
    assert resp.status_code == 404, resp.text
    assert conv_store.switch_calls == []


@pytest.mark.asyncio
async def test_switch_409_when_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """409 when a turn is running — switching mid-turn is rejected."""
    conv_store = _ConversationStore(conversations={"conv_src": _conv()})
    agent_store = _AgentStore(
        {"ag_session_scoped": _CURRENT, "ag_builtin_claude": _BUILTIN_CLAUDE}
    )
    # Mark the session as running in the relay status cache.
    monkeypatch.setitem(sessions_mod._session_status_cache, "conv_src", "running")
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(
        "/v1/sessions/conv_src/switch-agent", json={"agent_id": "ag_builtin_claude"}
    )

    assert resp.status_code == 409, resp.text
    # The 409 fires before the irreversible mutation.
    assert conv_store.switch_calls == []


@pytest.mark.asyncio
async def test_switch_400_unloadable_target_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    """400 when the target bundle can't load — fails before deleting the old
    agent, so the session is left untouched.
    """
    conv_store = _ConversationStore(conversations={"conv_src": _conv()})
    agent_store = _AgentStore(
        {"ag_session_scoped": _CURRENT, "ag_builtin_claude": _BUILTIN_CLAUDE}
    )
    _patch_family_helpers(
        monkeypatch, same_family=True, native=False, labels={}, raise_on_load=True
    )
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.post(
        "/v1/sessions/conv_src/switch-agent", json={"agent_id": "ag_builtin_claude"}
    )

    assert resp.status_code == 400, resp.text
    # Pre-commit failure → no switch attempted (old agent intact).
    assert conv_store.switch_calls == []
