"""Tests for server-owned runner session initialization coordination."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.entities import Conversation, MessageData, NewConversationItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inner.native_attachments import CAP_FILESYSTEM_ATTACHMENTS
from omnigent.runner.session_init_protocol import (
    build_runner_session_init_payload,
    parse_runner_session_init_envelope,
)
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.runner.transports.ws_tunnel.transport import WSTunnelTransport
from omnigent.server.runner_session_init import RunnerSessionInitializer
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store import (
    FORK_CARRY_HISTORY_LABEL_KEY,
    FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
    FORK_SOURCE_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore


class _Registry:
    def __init__(self) -> None:
        self.connection: Any = SimpleNamespace(generation=1)

    def get(self, _runner_id: str) -> Any:
        return self.connection


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.status_code = 201

    async def post(self, _path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(kwargs["json"])
        self.entered.set()
        await self.release.wait()
        return httpx.Response(self.status_code, json={"status": "initialized"})


def _conversation() -> Conversation:
    return Conversation(
        id="conv_init",
        created_at=10,
        updated_at=11,
        root_conversation_id="conv_init",
        agent_id="agent_init",
        runner_id="runner_init",
        workspace="/tmp/workspace",
        labels={"example": "value"},
    )


@pytest.mark.asyncio
async def test_initializer_shares_result_for_one_tunnel_generation() -> None:
    registry = _Registry()
    client = _Client()
    initializer = RunnerSessionInitializer(  # type: ignore[arg-type]
        registry,
        server_version="0.6.0.dev0",
    )
    conversation = _conversation()

    first = asyncio.create_task(initializer.initialize(conversation, client, timeout=10))  # type: ignore[arg-type]
    await client.entered.wait()
    second = asyncio.create_task(initializer.initialize(conversation, client, timeout=10))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    client.release.set()
    first_response, second_response = await asyncio.gather(first, second)

    assert first_response is second_response
    assert len(client.calls) == 1
    assert client.calls[0]["session_init"]["snapshot"]["workspace"] == "/tmp/workspace"

    cached = await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert cached is first_response
    assert len(client.calls) == 1

    initializer.invalidate_runner("runner_init")
    await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_initializer_evicts_rejected_result_for_retry() -> None:
    registry = _Registry()
    client = _Client()
    client.release.set()
    client.status_code = 503
    initializer = RunnerSessionInitializer(  # type: ignore[arg-type]
        registry,
        server_version="0.6.0.dev0",
    )
    conversation = _conversation()

    first = await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    second = await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]

    assert first.status_code == second.status_code == 503
    assert len(client.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_ready"),
    [
        ({"session_init_protocol_version": 2, "terminal_ready": True}, True),
        ({}, False),
    ],
    ids=["current-runner", "legacy-runner"],
)
async def test_session_init_readiness_is_explicit_and_backward_compatible(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    expected_ready: bool,
) -> None:
    """Only a current runner response suppresses the terminal ensure."""
    from omnigent.server.routes import sessions as sessions_routes

    async def _noop_recovered(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sessions_routes, "_publish_runner_recovered_status", _noop_recovered)
    monkeypatch.setattr(sessions_routes, "_ensure_runner_relay_ready", _noop_recovered)
    monkeypatch.setattr(
        "omnigent.server.child_session_recovery.restore_active_children", _noop_recovered
    )

    class _Initializer:
        async def initialize(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
            return httpx.Response(
                201,
                json=payload,
                request=httpx.Request("POST", "http://runner/v1/sessions"),
            )

    ready = await sessions_routes._ensure_runner_session_initialized(
        "conv_init",
        _conversation(),
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        initializer=_Initializer(),  # type: ignore[arg-type]
    )

    assert ready is expected_ready


def test_reconnect_init_envelope_carries_fork_history_directives(db_uri: str) -> None:
    """A forked native session's fork directives survive to the runner envelope.

    End-to-end regression guard for the exact seam that dropped a forked
    claude-native session's history: the runner reconnect path
    (``_on_runner_connect``) sources its conversations from
    ``list_conversations_by_runner_id`` and hands each straight to
    ``build_runner_session_init_payload``, which projects
    ``conversation.labels`` into the init envelope the runner reads to decide
    whether to clone/rebuild the vendor transcript. When that store lookup
    returned label-less conversations, the envelope shipped no ``omnigent.fork.*``
    directives, so the runner skipped its clone/rebuild branch and launched the
    TUI fresh -- history lost -- even though the fork copied the history into the
    store.

    This drives the real store (fork included), not a hand-built envelope, so it
    fails if any layer between the by-runner-id lookup and the envelope stops
    carrying labels. The label-to-launch-metadata projection is covered
    separately by ``test_claude_launch_metadata_envelope_never_calls_server``.
    """
    agent_store = SqlAlchemyAgentStore(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)

    # A claude-native SOURCE with a captured native session id and a bound
    # workspace -- the two preconditions fork_conversation needs to stamp the
    # source-transcript directive.
    agent = agent_store.create(generate_agent_id(), "claude-native-ui", "bundle/loc")
    source = conversation_store.create_conversation(agent_id=agent.id, workspace="/tmp/ws")
    conversation_store.set_external_session_id(source.id, "src-claude-sid")

    # Fork it the way the route does for a same-family native target: carry
    # history and resume the source's native transcript.
    fork = conversation_store.fork_conversation(
        source.id,
        carry_history_into_native=True,
        resume_source_native_session=True,
    )
    # Bind the fork to a runner so the reconnect lookup returns it.
    assert conversation_store.set_runner_id(fork.id, "runner_fork")

    # The reconnect path: by-runner-id lookup -> init payload.
    bound = conversation_store.list_conversations_by_runner_id("runner_fork")
    assert [c.id for c in bound] == [fork.id]

    payload = build_runner_session_init_payload(bound[0], server_version="0.6.0.dev0")
    envelope = parse_runner_session_init_envelope(payload)
    assert envelope is not None

    # The directives that select the runner's clone/rebuild branch must be
    # present in the envelope the runner actually reads.
    labels = envelope.snapshot.labels
    assert labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1"
    assert labels.get(FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY) == "src-claude-sid"
    assert labels.get(FORK_SOURCE_LABEL_KEY) == source.id

    # And the runner's own projection reads them as launch directives -- the
    # boolean the clone/rebuild branch gates on.
    from omnigent.runner.app import _claude_launch_metadata_from_envelope

    metadata = _claude_launch_metadata_from_envelope(envelope)
    assert metadata.fork_carry_history is True
    assert metadata.fork_source_external_id == "src-claude-sid"


@pytest.mark.asyncio
async def test_recovery_has_own_readiness_and_stable_identity_across_failed_posts() -> None:
    registry, client = _Registry(), _Client()
    client.release.set()
    initializer = RunnerSessionInitializer(registry, server_version="test")  # type: ignore[arg-type]
    conv = _conversation()
    await initializer.initialize(conv, client, timeout=10)  # type: ignore[arg-type]
    client.status_code = 500
    await initializer.initialize(conv, client, timeout=10, resume_interrupted_turn=True)  # type: ignore[arg-type]
    first_id = client.calls[-1]["session_init"]["recovery_id"]
    client.status_code = 201
    await initializer.initialize(conv, client, timeout=10, resume_interrupted_turn=True)  # type: ignore[arg-type]
    assert len(client.calls) == 3
    assert first_id and client.calls[-1]["session_init"]["recovery_id"] == first_id
    await initializer.initialize(conv, client, timeout=10, resume_interrupted_turn=True)  # type: ignore[arg-type]
    assert len(client.calls) == 3
    # A later binding back to this live runner starts a distinct recovery.
    initializer.invalidate_session(conv.id)
    await initializer.initialize(conv, client, timeout=10, resume_interrupted_turn=True)  # type: ignore[arg-type]
    assert client.calls[-1]["session_init"]["recovery_id"] != first_id


class _AdvertisedRunner:
    generation = 1

    def __init__(self, capabilities: list[str]) -> None:
        self.hello = HelloFrame(
            runner_version="test", frame_protocol_version=1, capabilities=capabilities
        )


def _attachment_initializer(
    db_uri: str, filename: str | None, capabilities: list[str]
) -> tuple[RunnerSessionInitializer, Conversation, _Registry, _Client]:
    """Build an initializer over real persisted attachment metadata and history."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    agent = agent_store.create(generate_agent_id(), "native-attachment-test", "bundle/loc")
    conversation = conv_store.create_conversation(agent_id=agent.id, runner_id="a" * 32)
    if filename is not None:
        stored = file_store.create(filename, bytes=4, session_id=conversation.id)
        conv_store.append(
            conversation.id,
            [
                NewConversationItem(
                    type="message",
                    response_id="b" * 32,
                    data=MessageData(
                        role="user", content=[{"type": "input_file", "file_id": stored.id}]
                    ),
                )
            ],
        )
    registry, client = _Registry(), _Client()
    registry.connection = _AdvertisedRunner(capabilities)
    initializer = RunnerSessionInitializer(
        registry,  # type: ignore[arg-type]
        server_version="test",
        conversation_store=conv_store,
        file_store=file_store,
    )
    return initializer, conversation, registry, client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename,capabilities,allowed",
    [
        (None, [], True),
        ("sample.txt", [], True),
        ("sample.png", [], True),
        ("sample.zip", [], False),
        ("sample.sqlite", [], False),
        ("sample.docx", [], False),
        ("sample.zip", [CAP_FILESYSTEM_ATTACHMENTS], True),
    ],
)
async def test_initializer_checks_retained_files_before_posting_to_runner(
    db_uri: str, filename: str | None, capabilities: list[str], allowed: bool
) -> None:
    """Reconnect cannot start a cold rebuild that silently loses new file formats."""
    initializer, conversation, _, client = _attachment_initializer(db_uri, filename, capabilities)
    client.release.set()
    if allowed:
        response = await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
        assert response.status_code == 201
        assert len(client.calls) == 1
    else:
        with pytest.raises(OmnigentError, match="Update Omnigent") as error:
            await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
        assert error.value.code == ErrorCode.CONFLICT
        assert not client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("require_success", [False, True])
async def test_attachment_init_error_propagates_and_can_retry_after_upgrade(
    db_uri: str, require_success: bool
) -> None:
    """Message/retry helpers retain the actionable error and rejected inits are evicted."""
    from omnigent.server.routes import sessions as sessions_routes

    initializer, conversation, registry, client = _attachment_initializer(db_uri, "sample.zip", [])
    client.release.set()
    with pytest.raises(OmnigentError, match="Update Omnigent") as error:
        await sessions_routes._ensure_runner_session_initialized(
            conversation.id,
            conversation,
            client,  # type: ignore[arg-type]
            initializer._conversation_store,  # type: ignore[arg-type]
            initializer=initializer,
            require_success=require_success,
        )
    assert error.value.code == ErrorCode.CONFLICT
    assert not client.calls
    assert not initializer._tasks

    assert isinstance(registry.connection, _AdvertisedRunner)
    registry.connection.hello.capabilities.append(CAP_FILESYSTEM_ATTACHMENTS)
    response = await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert response.status_code == 201
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_attachment_validation_preserves_single_flight(db_uri: str) -> None:
    """Concurrent reconnect and message initialization share the async validation/post."""
    initializer, conversation, _, client = _attachment_initializer(
        db_uri, "sample.zip", [CAP_FILESYSTEM_ATTACHMENTS]
    )
    first = asyncio.create_task(initializer.initialize(conversation, client, timeout=10))  # type: ignore[arg-type]
    await client.entered.wait()
    second = asyncio.create_task(initializer.initialize(conversation, client, timeout=10))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    client.release.set()
    first_response, second_response = await asyncio.gather(first, second)
    assert first_response is second_response
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_init_logs_rejection_retry_and_cached_success_once() -> None:
    from tests.debug_log_helpers import capture_debug_rows

    registry = _Registry()
    client = _Client()
    client.release.set()
    initializer = RunnerSessionInitializer(registry, server_version="test")  # type: ignore[arg-type]
    conversation = _conversation()
    with capture_debug_rows("server") as rows:
        client.status_code = 503
        await initializer.initialize(conversation, client, timeout=1)  # type: ignore[arg-type]
        client.status_code = 201
        await initializer.initialize(conversation, client, timeout=1)  # type: ignore[arg-type]
        await initializer.initialize(conversation, client, timeout=1)  # type: ignore[arg-type]
    events = [row for row in rows if row["event_name"]]
    assert [row["event_name"] for row in events] == [
        "runner_session_init_started",
        "runner_session_init_failed",
        "runner_session_init_started",
        "runner_session_initialized",
    ]
    assert all(row["session_id"] == conversation.id for row in events)
    assert all(row["attributes"]["runner_id"] == conversation.runner_id for row in events)
    assert events[1]["attributes"]["status_code"] == "503"
    # The init-started row says what the server asked the runner to do.
    assert events[0]["attributes"]["resume_interrupted_turn"] == "False"
    assert events[0]["attributes"]["suppress_recovery_turn"] == "False"
    assert "recovery_id" not in events[0]["attributes"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("tunnel closed before request completed"),
        httpx.ConnectError("runner 'runner_init' is offline"),
    ],
)
async def test_init_attributes_dropped_tunnel_to_runner(error: Exception) -> None:
    """Both shapes the tunnel transport raises for a vanished runner."""
    from tests.debug_log_helpers import capture_debug_rows

    class _DroppedTunnelClient(_Client):
        async def post(self, _path: str, **kwargs: Any) -> httpx.Response:
            raise error

    initializer = RunnerSessionInitializer(_Registry(), server_version="test")  # type: ignore[arg-type]
    with capture_debug_rows("server") as rows, pytest.raises(type(error)):
        await initializer.initialize(_conversation(), _DroppedTunnelClient(), timeout=1)  # type: ignore[arg-type]
    [failed] = [row for row in rows if row["event_name"] == "runner_session_init_failed"]
    assert failed["attributes"]["error_category"] == "runner"
    assert failed["attributes"]["error_impact"] == "transient"


@pytest.mark.asyncio
async def test_cancelling_waiter_preserves_shared_initialization() -> None:
    registry, client = _Registry(), _Client()
    initializer = RunnerSessionInitializer(registry, server_version="test")  # type: ignore[arg-type]
    conv = _conversation()
    waiter = asyncio.create_task(initializer.initialize(conv, client, timeout=10))  # type: ignore[arg-type]
    await client.entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    client.release.set()
    assert (await initializer.initialize(conv, client, timeout=10)).status_code == 201  # type: ignore[arg-type]
    assert len(client.calls) == 1


class _Agents:
    """Agent store stub whose ``get`` finds only the given ids."""

    def __init__(self, *ids: str) -> None:
        self.ids = set(ids)

    def get(self, agent_id: str) -> object | None:
        return object() if agent_id in self.ids else None


@pytest.mark.asyncio
async def test_initializer_skips_a_session_whose_agent_was_removed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The runner could only reject it: no POST, one warning line, and a response
    callers recognize. An agent that still exists initializes as before."""
    from omnigent.server.runner_session_init import is_session_agent_removed

    client = _Client()
    client.release.set()
    conversation = _conversation()
    removed = RunnerSessionInitializer(  # type: ignore[arg-type]
        _Registry(), server_version="test", agent_store=_Agents()
    )
    with caplog.at_level(logging.WARNING, logger="omnigent.server.runner_session_init"):
        response = await removed.initialize(conversation, client, timeout=1)  # type: ignore[arg-type]

    assert is_session_agent_removed(response)
    assert client.calls == []
    records = [r for r in caplog.records if r.name == "omnigent.server.runner_session_init"]
    assert [(r.levelname, r.exc_info) for r in records] == [("WARNING", None)]

    present = RunnerSessionInitializer(  # type: ignore[arg-type]
        _Registry(), server_version="test", agent_store=_Agents(conversation.agent_id)
    )
    response = await present.initialize(conversation, client, timeout=1)  # type: ignore[arg-type]
    assert response.status_code == 201
    assert not is_session_agent_removed(response)
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_callers_arriving_during_the_agent_check_share_one_init() -> None:
    """The agent lookup yields, so a caller arriving meanwhile joins the first
    caller's initialization instead of sending the runner another."""
    client = _Client()
    client.release.set()
    conversation = _conversation()
    initializer = RunnerSessionInitializer(  # type: ignore[arg-type]
        _Registry(), server_version="test", agent_store=_Agents(conversation.agent_id)
    )
    responses = await asyncio.gather(
        *(initializer.initialize(conversation, client, timeout=1) for _ in range(2))  # type: ignore[arg-type]
    )
    assert [response.status_code for response in responses] == [201, 201]
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", ["runner", "session"])
@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_retired_initialization_preserves_each_callers_cancellation(
    invalidate: str, cancel_caller: bool
) -> None:
    registry, client = _Registry(), _Client()
    initializer = RunnerSessionInitializer(registry, server_version="test")  # type: ignore[arg-type]
    conv = _conversation()
    first = asyncio.create_task(initializer.initialize(conv, client, timeout=10))  # type: ignore[arg-type]
    await client.entered.wait()
    second = asyncio.create_task(initializer.initialize(conv, client, timeout=10))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    if cancel_caller:
        first.cancel()
    if invalidate == "runner":
        initializer.invalidate_runner("runner_init", generation=1)
    else:
        initializer.invalidate_session(conv.id)
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError if cancel_caller else ConnectionError)
    assert isinstance(results[1], ConnectionError)
    assert not second.cancelled()
    assert len(client.calls) == 1

    client.release.set()
    assert (await initializer.initialize(conv, client, timeout=10)).status_code == 201  # type: ignore[arg-type]
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_retiring_old_generation_cancels_only_its_initialization() -> None:
    registry = _Registry()
    old_client, new_client = _Client(), _Client()
    initializer = RunnerSessionInitializer(registry, server_version="test")  # type: ignore[arg-type]
    conv = _conversation()
    old = asyncio.create_task(
        initializer.initialize(conv, old_client, timeout=10, resume_interrupted_turn=True)  # type: ignore[arg-type]
    )
    await old_client.entered.wait()
    registry.connection = SimpleNamespace(generation=2)
    new = asyncio.create_task(
        initializer.initialize(conv, new_client, timeout=10, resume_interrupted_turn=True)  # type: ignore[arg-type]
    )
    await new_client.entered.wait()
    await asyncio.gather(
        *initializer.invalidate_runner(conv.runner_id, generation=1), return_exceptions=True
    )
    with pytest.raises(ConnectionError):
        await old
    assert not new.done()
    new_client.release.set()
    response = await new
    assert (
        await initializer.initialize(  # type: ignore[arg-type]
            conv, new_client, timeout=10, resume_interrupted_turn=True
        )
        is response
    )
    assert len(new_client.calls) == 1
    assert (
        old_client.calls[0]["session_init"]["recovery_id"]
        != new_client.calls[0]["session_init"]["recovery_id"]
    )


@pytest.mark.asyncio
async def test_replaced_connection_cannot_post_after_attachment_lookup(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from omnigent.server.routes._sessions import helpers

    initializer, conv, registry, client = _attachment_initializer(db_uri, None, [])
    entered, release = threading.Event(), threading.Event()

    def blocked_lookup(*_args: Any) -> None:
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(helpers, "_filesystem_attachment_in_history", blocked_lookup)
    client.release.set()
    init = asyncio.create_task(initializer.initialize(conv, client, timeout=10))  # type: ignore[arg-type]
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        registry.connection = SimpleNamespace(generation=2)
        release.set()
        with pytest.raises(ConnectionError, match="tunnel changed"):
            await init
    finally:
        release.set()
        await asyncio.gather(init, return_exceptions=True)
    assert not client.calls


@pytest.mark.asyncio
async def test_delayed_initialization_cannot_follow_a_replacement_tunnel() -> None:
    """Even a request paused inside httpx remains pinned to its original connection."""
    reg = TunnelRegistry()
    hello = HelloFrame(runner_version="0.1.0", frame_protocol_version=1, harnesses=[], envs=[])
    old = reg.register("runner_init", AsyncMock(), hello)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delay_send(_request: httpx.Request) -> None:
        entered.set()
        await release.wait()

    initializer = RunnerSessionInitializer(reg, server_version="test")
    async with httpx.AsyncClient(
        transport=WSTunnelTransport(reg, "runner_init"),
        base_url="http://runner",
        event_hooks={"request": [delay_send]},
    ) as client:
        init = asyncio.create_task(
            initializer.initialize(
                _conversation(), client, timeout=10, resume_interrupted_turn=True
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            new = reg.register("runner_init", AsyncMock(), hello)
            assert new.generation > old.generation
            release.set()
            with pytest.raises(ConnectionError, match="before request was sent"):
                await asyncio.wait_for(init, timeout=1)
        finally:
            release.set()
            init.cancel()
            await asyncio.gather(init, return_exceptions=True)
    assert not new.in_flight
    assert new.outbound_queue.empty()


@pytest.mark.asyncio
async def test_handshake_for_a_removed_agent_forwards_or_says_so() -> None:
    """A message forward goes ahead (the runner answers it); a caller that needs
    the runner ready gets the removed-agent error, not "runner unavailable"."""
    from omnigent.server.routes._sessions.orchestration import (
        _ensure_runner_session_initialized,
    )

    conversation = _conversation()
    initializer = RunnerSessionInitializer(  # type: ignore[arg-type]
        _Registry(), server_version="test", agent_store=_Agents()
    )
    client = _Client()
    client.release.set()

    forwarded = await _ensure_runner_session_initialized(
        conversation.id,
        conversation,
        client,
        Mock(),
        initializer,  # type: ignore[arg-type]
    )
    assert forwarded is False
    with pytest.raises(OmnigentError) as raised:
        await _ensure_runner_session_initialized(
            conversation.id,
            conversation,
            client,
            Mock(),
            initializer,
            require_success=True,  # type: ignore[arg-type]
        )
    assert raised.value.code == ErrorCode.SESSION_AGENT_MISSING
    assert client.calls == []
