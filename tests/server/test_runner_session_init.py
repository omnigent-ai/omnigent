"""Tests for server-owned runner session initialization coordination."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import threading
from collections.abc import Iterator
from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.entities import Conversation, MessageData, NewConversationItem
from omnigent.entities.session_observation import RunnerOfflineDecision, SessionObservation
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
        self.response_json: dict[str, Any] = {"status": "initialized"}

    async def post(self, _path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(kwargs["json"])
        self.entered.set()
        await self.release.wait()
        return httpx.Response(self.status_code, json=self.response_json)


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

    assert not initializer.is_initialized(conversation, client)  # type: ignore[arg-type]
    first = asyncio.create_task(initializer.initialize(conversation, client, timeout=10))  # type: ignore[arg-type]
    await client.entered.wait()
    assert not initializer.is_initialized(conversation, client)  # type: ignore[arg-type]
    second = asyncio.create_task(initializer.initialize(conversation, client, timeout=10))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    client.release.set()
    first_response, second_response = await asyncio.gather(first, second)

    assert first_response is second_response
    assert initializer.is_initialized(conversation, client)  # type: ignore[arg-type]
    assert len(client.calls) == 1
    assert client.calls[0]["session_init"]["snapshot"]["workspace"] == "/tmp/workspace"

    cached = await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert cached is first_response
    assert len(client.calls) == 1
    conversation.inference_snapshot = {"runtime_config": {"providers": {}}}
    assert not initializer.is_initialized(conversation, client)  # type: ignore[arg-type]
    conversation.inference_snapshot = None

    initializer.invalidate_runner("runner_init")
    assert not initializer.is_initialized(conversation, client)  # type: ignore[arg-type]
    await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert len(client.calls) == 2
    registry.connection = SimpleNamespace(generation=2)
    assert not initializer.is_initialized(conversation, client)  # type: ignore[arg-type]
    await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert initializer.is_initialized(conversation, client)  # type: ignore[arg-type]
    conversation.agent_id = "different_agent"
    assert not initializer.is_initialized(conversation, client)  # type: ignore[arg-type]
    conversation.agent_id = "agent_init"
    conversation.sub_agent_name = "different_child"
    assert not initializer.is_initialized(conversation, client)  # type: ignore[arg-type]


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
    assert not initializer.is_initialized(conversation, client)  # type: ignore[arg-type]
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
    assert not initializer.is_initialized(conv, client)  # type: ignore[arg-type]
    await initializer.initialize(conv, client, timeout=10, resume_interrupted_turn=True)  # type: ignore[arg-type]
    assert client.calls[-1]["session_init"]["recovery_id"] != first_id
    assert initializer.is_initialized(conv, client)  # type: ignore[arg-type]


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
    client.response_json = {"error": "runner overloaded"}
    with capture_debug_rows("server") as rows:
        client.status_code = 503
        await initializer.initialize(conversation, client, timeout=1)  # type: ignore[arg-type]
        client.status_code = 201
        client.response_json = {"status": "initialized"}
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
    # Non-2xx row carries status code, body snippet, and a distinct message.
    rejected: dict[str, Any] = events[1]
    assert rejected["attributes"]["status_code"] == "503"
    assert rejected["attributes"]["response_body"] == "runner overloaded"
    assert rejected["level"] == "WARNING"
    assert "finished" not in rejected["message"]
    # Success row logs at INFO.
    assert events[3]["level"] == "INFO"
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
    assert failed["attributes"]["exc_type"] == type(error).__name__


@pytest.mark.asyncio
async def test_init_logs_transport_error_as_warning_without_traceback() -> None:
    """Transport loss during POST /v1/sessions is recoverable on the next reconnect."""
    from tests.debug_log_helpers import capture_debug_rows

    registry = _Registry()
    initializer = RunnerSessionInitializer(registry, server_version="test")  # type: ignore[arg-type]
    conversation = _conversation()

    class _TunnelDropClient:
        async def post(self, _path: str, **kwargs: Any) -> httpx.Response:
            raise httpx.ConnectError("tunnel closed before request completed")

    with capture_debug_rows("server") as rows:
        with pytest.raises(httpx.ConnectError):
            await initializer.initialize(conversation, _TunnelDropClient(), timeout=1)  # type: ignore[arg-type]

    transport_rows = [row for row in rows if row["event_name"] == "runner_session_init_failed"]
    assert len(transport_rows) == 1
    row: dict[str, Any] = transport_rows[0]
    assert row["level"] == "WARNING"
    assert row["stack_trace"] is None
    assert row["attributes"]["exc_type"] == "ConnectError"


@pytest.mark.asyncio
async def test_init_logs_unexpected_exception_as_error_with_traceback() -> None:
    """Unexpected exceptions during POST /v1/sessions stay at ERROR with traceback."""
    from tests.debug_log_helpers import capture_debug_rows

    registry = _Registry()
    initializer = RunnerSessionInitializer(registry, server_version="test")  # type: ignore[arg-type]
    conversation = _conversation()

    class _BrokenClient:
        async def post(self, _path: str, **kwargs: Any) -> httpx.Response:
            raise RuntimeError("unexpected internal error")

    with capture_debug_rows("server") as rows:
        with pytest.raises(RuntimeError):
            await initializer.initialize(conversation, _BrokenClient(), timeout=1)  # type: ignore[arg-type]

    error_rows = [row for row in rows if row["event_name"] == "runner_session_init_failed"]
    assert len(error_rows) == 1
    assert error_rows[0]["level"] == "ERROR"
    assert error_rows[0]["stack_trace"] is not None


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
    """The runner could only reject it: no POST, one info line, and a response
    callers recognize. An agent that still exists initializes as before."""
    from omnigent.server.runner_session_init import is_session_agent_removed

    client = _Client()
    client.release.set()
    conversation = _conversation()
    removed = RunnerSessionInitializer(  # type: ignore[arg-type]
        _Registry(), server_version="test", agent_store=_Agents()
    )
    with caplog.at_level(logging.INFO, logger="omnigent.server.runner_session_init"):
        response = await removed.initialize(conversation, client, timeout=1)  # type: ignore[arg-type]

    assert is_session_agent_removed(response)
    assert client.calls == []
    records = [r for r in caplog.records if r.name == "omnigent.server.runner_session_init"]
    assert [(r.levelname, r.exc_info) for r in records] == [("INFO", None)]

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "level"),
    [(404, "WARNING"), (410, "WARNING"), (503, "WARNING"), (500, "ERROR"), (409, "ERROR")],
)
async def test_init_rejection_level_by_status(status: int, level: str) -> None:
    """Transient rejections warn; unexpected statuses stay ERROR, never 'finished'."""
    from tests.debug_log_helpers import capture_debug_rows

    client = _Client()
    client.release.set()
    client.status_code = status
    client.response_json = {"error": "nope"}
    initializer = RunnerSessionInitializer(_Registry(), server_version="test")  # type: ignore[arg-type]
    with capture_debug_rows("server") as rows:
        await initializer.initialize(_conversation(), client, timeout=1)  # type: ignore[arg-type]
    failed = [r for r in rows if r["event_name"] == "runner_session_init_failed"]
    assert len(failed) == 1
    rejected: dict[str, Any] = failed[0]
    assert rejected["attributes"]["failure_kind"] == "rejected"
    assert rejected["level"] == level
    assert rejected["attributes"]["response_body"] == "nope"
    assert "finished" not in rejected["message"]


@pytest.fixture
def native_init_store(db_uri: str) -> Iterator[SqlAlchemyConversationStore]:
    from omnigent.server import session_live_state
    from omnigent.server.routes._sessions import common

    store = SqlAlchemyConversationStore(db_uri)
    common._session_status_cache.clear()
    common._session_terminal_pending_cache.clear()
    session_live_state.configure(store)
    try:
        yield store
    finally:
        session_live_state.submit("test_barrier", lambda: None).result(timeout=10)
        session_live_state.configure(None)
        common._session_status_cache.clear()
        common._session_terminal_pending_cache.clear()


class _ObservedInitClient(_Client):
    def __init__(self, store: SqlAlchemyConversationStore, session_id: str) -> None:
        super().__init__()
        self._store = store
        self._session_id = session_id
        self.at_post: list[Conversation | None] = []

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        self.at_post.append(self._store.get_conversation(self._session_id))
        response = await super().post(path, **kwargs)
        response.request = httpx.Request("POST", "http://runner" + path)
        return response


def _native_initializer(
    store: SqlAlchemyConversationStore,
) -> tuple[Conversation, _Registry, _ObservedInitClient, RunnerSessionInitializer]:
    conversation = store.create_conversation(
        agent_id=generate_agent_id(),
        runner_id="runner_init",
        harness_override="claude-native",
        labels={"omnigent.wrapper": "claude-code-native-ui"},
    )
    registry, client = _Registry(), _ObservedInitClient(store, conversation.id)
    client.response_json = {
        "status": "initialized",
        "session_init_protocol_version": 2,
        "terminal_ready": True,
    }
    initializer = RunnerSessionInitializer(
        registry,
        server_version="test",
        conversation_store=store,  # type: ignore[arg-type]
    )
    return conversation, registry, client, initializer


@pytest.mark.asyncio
async def test_native_initializer_reserves_before_post_and_shares_owned_completion(
    native_init_store: SqlAlchemyConversationStore,
) -> None:
    store = native_init_store
    conv, _, client, initializer = _native_initializer(store)
    previous = store.record_session_observation(
        conv.id,
        "failed",
        response_id="previous-response",
        error_key="previous-error",
        error_labels={"omnigent.last_task_error_code": "native_turn_error"},
    )
    assert previous is not None
    conv = store.get_conversation(conv.id)
    assert conv is not None
    second_conv = dataclasses.replace(conv)
    first = asyncio.create_task(initializer.initialize(conv, client, timeout=10))
    second = None
    try:
        await asyncio.wait_for(client.entered.wait(), 10)
        pending = client.at_post[0]
        assert pending is not None and pending.terminal_pending
        reservation = pending.session_observation
        assert reservation is not None and reservation != previous
        assert client.calls[0]["session_observation"] == reservation.model_dump()
        assert pending.live_status == "failed"
        assert reservation.response_id == previous.response_id
        assert reservation.error_key == previous.error_key
        second = asyncio.create_task(initializer.initialize(second_conv, client, timeout=10))
        await asyncio.sleep(0)
        assert len(client.calls) == 1
        assert store.get_session_observation(conv.id) == reservation
        client.release.set()
        responses = await asyncio.gather(first, second)
        assert responses[0] is responses[1] and responses[0].status_code == 201
        current = store.get_conversation(conv.id)
        assert current is not None and not current.terminal_pending
        assert current.live_status == "failed"
        assert current.session_observation.response_id == previous.response_id
        assert current.session_observation.error_key == previous.error_key
        assert current.session_observation.revision > reservation.revision
        completed = SessionObservation.model_validate(
            responses[0].extensions["session_observation"]
        )
        assert completed == current.session_observation
        assert conv is not second_conv
        assert conv.session_observation == second_conv.session_observation == completed
        assert initializer.is_initialized(conv, client)
    finally:
        client.release.set()
        await asyncio.gather(
            *[task for task in (first, second) if task is not None], return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "pending-pane",
        "completed-pane",
        "replacement-pane",
        "pending-file",
        "pending-idle",
        "replacement-file",
        "replacement-idle",
    ],
)
async def test_late_runner_status_after_init_response_keeps_its_source_ownership(
    native_init_store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from omnigent.runtime import session_stream
    from omnigent.server.routes._sessions import common, orchestration

    replacement_startup = mode.startswith("replacement-")
    file_running = mode in {"pending-file", "replacement-file"}
    idle_resync = mode in {"pending-idle", "replacement-idle"}
    store = native_init_store
    conv, _, client, initializer = _native_initializer(store)
    previous = store.record_session_observation(conv.id, "idle", response_id="prior-response")
    assert previous is not None
    conv = store.get_conversation(conv.id)
    assert conv is not None
    completion_entered, completion_release = asyncio.Event(), asyncio.Event()
    complete = orchestration._complete_native_terminal_startup
    events: list[dict[str, Any]] = []
    publish = session_stream.publish

    def capture(session_id: str, event: dict[str, Any]) -> None:
        events.append(event)
        publish(session_id, event)

    async def paused_completion(
        session_id: str, owner: SessionObservation
    ) -> SessionObservation | None:
        completed = await complete(session_id, owner) if mode == "completed-pane" else None
        completion_entered.set()
        await completion_release.wait()
        return completed if completed is not None else await complete(session_id, owner)

    monkeypatch.setattr(session_stream, "publish", capture)
    monkeypatch.setattr(orchestration, "_complete_native_terminal_startup", paused_completion)
    task = asyncio.create_task(initializer.initialize(conv, client, timeout=10))
    try:
        await asyncio.wait_for(client.entered.wait(), 10)
        client.release.set()
        await asyncio.wait_for(completion_entered.wait(), 10)
        assert len(client.calls) == 1
        current = store.get_conversation(conv.id)
        assert current is not None
        if replacement_startup:
            replacement = await orchestration._begin_native_terminal_startup(
                conv.id, current.session_observation
            )
            assert replacement is not None
            current = store.get_conversation(conv.id)
            assert current is not None and current.session_observation == replacement
        projection = common._session_status_cache.projection(conv.id)
        assert projection is not None and projection.observation == current.session_observation
        assert (
            common._session_terminal_pending_cache.get(conv.id, False) == current.terminal_pending
        )
        events.clear()
        frames = [
            {"type": "session.terminal.activity", "terminal_id": "terminal_claude_main"},
            {
                "type": "session.status",
                "status": "idle" if idle_resync else "running",
                "status_source": (
                    "claude_status_file" if file_running or idle_resync else "pane_activity"
                ),
            },
        ]
        response_text = "".join("data: " + json.dumps(event) + "\n\n" for event in frames)
        response_text += "data: [DONE]\n\n"
        async with httpx.AsyncClient(
            base_url="http://runner",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, text=response_text)
            ),
        ) as relay_client:
            await orchestration._relay_runner_stream_once(
                conv.id,
                relay_client,
                store,
                runner_id=conv.runner_id,
                owner=current.session_observation,
            )
        after = store.get_conversation(conv.id)
        assert after is not None
        assert any(event["type"] == "session.terminal.activity" for event in events)
        if file_running:
            assert after.live_status == "running" and not after.terminal_pending
            assert after.session_observation.revision > current.session_observation.revision
            assert any(event["type"] == "session.status" for event in events)
        else:
            assert after.session_observation == current.session_observation
            assert after.terminal_pending == current.terminal_pending
            assert common._session_status_cache.projection(conv.id) == projection
            if not idle_resync:
                assert all(event["type"] != "session.status" for event in events)
        completion_release.set()
        if replacement_startup or file_running:
            with pytest.raises(OmnigentError):
                await asyncio.wait_for(task, 10)
            assert not initializer.is_initialized(conv, client)
            assert store.get_session_observation(conv.id) == after.session_observation
        else:
            response = await asyncio.wait_for(task, 10)
            assert response.status_code == 201 and initializer.is_initialized(conv, client)
            settled = store.get_conversation(conv.id)
            assert settled is not None and not settled.terminal_pending
            assert settled.session_observation.status == current.session_observation.status
        assert len(client.calls) == 1
    finally:
        client.release.set()
        completion_release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "queued_status", ["running", "waiting", "failed", "startup", "binding-aba"]
)
@pytest.mark.parametrize("resolution", ["done", "pending", "failed", "unresolved"])
def test_idle_resync_never_adopts_its_queued_successor(
    native_init_store: SqlAlchemyConversationStore, queued_status: str, resolution: str
) -> None:
    from omnigent.server import session_live_state

    store = native_init_store
    conv, _, _, _ = _native_initializer(store)
    owner = store.record_session_observation(conv.id, "idle", response_id="native-old-turn")
    assert owner is not None
    queued: Future[SessionObservation | None] = Future()
    predecessor = session_live_state.PendingObservation(
        owner, queued_status, "native-new-turn", None, None, "queued-work", queued
    )
    blocked, release = threading.Event(), threading.Event()

    def hold_writer() -> None:
        blocked.set()
        assert release.wait(10)

    barrier = session_live_state.submit("held_writer", hold_writer)
    assert blocked.wait(10)

    def replace() -> SessionObservation:
        if queued_status == "startup":
            replacement = store.begin_terminal_startup(conv.id, owner, "newer-startup")
        elif queued_status == "binding-aba":
            store.replace_runner_id(conv.id, "runner-other")
            store.replace_runner_id(conv.id, owner.runner_id)
            replacement = store.record_session_observation(
                conv.id, "idle", response_id=owner.response_id
            )
        else:
            replacement = store.record_session_observation(
                conv.id,
                queued_status,
                response_id="native-new-turn",
                expected_observation=owner,
                starts_new_turn=True,
            )
        assert replacement is not None
        return replacement

    current = owner
    if resolution == "done":
        current = replace()
        queued.set_result(current)
    elif resolution == "failed":
        queued.set_exception(RuntimeError("synthetic predecessor failure"))
    committed: list[SessionObservation] = []
    refreshed: list[Conversation] = []
    try:
        future = session_live_state.persist_status_observation(
            conv.id,
            "idle",
            owner=owner,
            predecessor=predecessor,
            response_id=None,
            error=None,
            error_labels={},
            on_commit=lambda observation, _pending: committed.append(observation),
            on_current=refreshed.append,
        )
        assert future is not None
        if resolution == "pending":
            assert not queued.done()
            current = replace()
            queued.set_result(current)
        release.set()
        barrier.result(timeout=10)
        observed = future.result(timeout=10)
        assert store.get_session_observation(conv.id) == current
        if resolution in {"failed", "unresolved"}:
            assert observed == owner and committed == [owner] and refreshed == []
            assert queued.done() is (resolution == "failed")
        else:
            assert observed is None and committed == []
            assert len(refreshed) == 1 and refreshed[0].session_observation == current
            if queued_status == "startup":
                fresh = store.get_conversation(conv.id)
                assert fresh is not None and fresh.terminal_pending
    finally:
        if not queued.done():
            queued.set_result(None)
        release.set()
        barrier.result(timeout=10)


@pytest.mark.parametrize("owner_status", ["running", "waiting"])
@pytest.mark.parametrize("explicit_id", [False, True])
def test_native_idle_preserves_its_captured_busy_owner_without_adopting_new_work(
    native_init_store: SqlAlchemyConversationStore, owner_status: str, explicit_id: bool
) -> None:
    from omnigent.server import session_live_state

    store = native_init_store
    conv, _, _, _ = _native_initializer(store)
    old = store.record_session_observation(conv.id, owner_status, response_id="native-turn-a")
    assert old is not None
    current = store.record_session_observation(
        conv.id,
        "running",
        response_id="native-turn-b",
        expected_observation=old,
        starts_new_turn=True,
    )
    assert current is not None
    future = session_live_state.persist_status_observation(
        conv.id,
        "idle",
        owner=old,
        predecessor=None,
        response_id=old.response_id if explicit_id else None,
        error=None,
        error_labels={},
        on_commit=lambda *_args: pytest.fail("stale terminal edge committed"),
        on_current=lambda refreshed: None,
    )
    assert future is not None and future.result(timeout=10) is None
    assert store.get_session_observation(conv.id) == current
    committed: list[SessionObservation] = []
    future = session_live_state.persist_status_observation(
        conv.id,
        "idle",
        owner=current,
        predecessor=None,
        response_id=current.response_id,
        error=None,
        error_labels={},
        on_commit=lambda observation, _pending: committed.append(observation),
        on_current=lambda _refreshed: pytest.fail("current terminal edge was refused"),
    )
    assert future is not None
    completed = future.result(timeout=10)
    assert completed is not None and committed == [completed]
    assert completed.status == "idle" and completed.response_id == current.response_id


@pytest.mark.asyncio
async def test_native_initializer_cannot_complete_a_newer_startup_reservation(
    native_init_store: SqlAlchemyConversationStore,
) -> None:
    store = native_init_store
    conv, _, client, initializer = _native_initializer(store)
    task = asyncio.create_task(initializer.initialize(conv, client, timeout=10))
    try:
        await asyncio.wait_for(client.entered.wait(), 10)
        captured = store.get_session_observation(conv.id)
        assert captured is not None
        replacement = store.begin_terminal_startup(conv.id, captured, "replacement-startup")
        assert replacement is not None
        client.release.set()
        response = None
        with contextlib.suppress(ConnectionError, OmnigentError):
            response = await asyncio.wait_for(task, 10)
        assert response is None or response.status_code >= 400
        current = store.get_conversation(conv.id)
        assert current is not None and current.terminal_pending
        assert current.session_observation == replacement
        assert not initializer.is_initialized(conv, client)
    finally:
        client.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_initializer_metadata_outage_prevents_runner_post(
    native_init_store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = native_init_store
    conv, _, client, initializer = _native_initializer(store)
    client.release.set()
    attempts = 0

    def unavailable(*_args: Any, **_kwargs: Any):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("synthetic startup metadata outage")

    monkeypatch.setattr(store, "begin_terminal_startup", unavailable)
    response = None
    with contextlib.suppress(RuntimeError, OmnigentError, ConnectionError):
        response = await initializer.initialize(conv, client, timeout=10)
    assert attempts >= 1
    assert client.calls == []
    assert response is None or response.status_code >= 400
    assert not initializer.is_initialized(conv, client)


@pytest.mark.asyncio
async def test_native_initializer_completion_outage_does_not_cache_success(
    native_init_store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = native_init_store
    conv, _, client, initializer = _native_initializer(store)
    client.release.set()
    attempts = 0

    def unavailable(*_args: Any, **_kwargs: Any):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("synthetic completion metadata outage")

    monkeypatch.setattr(store, "finish_terminal_startup", unavailable)
    response = None
    with contextlib.suppress(RuntimeError, OmnigentError, ConnectionError):
        response = await initializer.initialize(conv, client, timeout=10)
    assert attempts >= 1
    assert len(client.calls) == 1
    assert response is None or response.status_code >= 400
    assert not initializer.is_initialized(conv, client)
    current = store.get_conversation(conv.id)
    assert current is not None and current.terminal_pending


@pytest.mark.asyncio
async def test_initializer_waiters_apply_captured_completion_before_a_newer_startup(
    native_init_store: SqlAlchemyConversationStore,
) -> None:
    store = native_init_store
    conv, _, client, initializer = _native_initializer(store)
    previous = store.record_session_observation(
        conv.id, "failed", response_id="old-response", error_key="old-error"
    )
    assert previous is not None
    conv = store.get_conversation(conv.id)
    assert conv is not None
    second_conv = dataclasses.replace(conv)
    first = asyncio.create_task(initializer.initialize(conv, client, timeout=10))
    second = None
    completed: list[SessionObservation] = []
    replacements: list[SessionObservation | None] = []

    def advance_after_completion(done: asyncio.Task[httpx.Response]) -> None:
        captured = SessionObservation.model_validate(
            done.result().extensions["session_observation"]
        )
        completed.append(captured)
        replacements.append(store.begin_terminal_startup(conv.id, captured, "newer-startup"))

    try:
        await asyncio.wait_for(client.entered.wait(), 10)
        second = asyncio.create_task(initializer.initialize(second_conv, client, timeout=10))
        await asyncio.sleep(0)
        assert len(initializer._tasks) == 1
        next(iter(initializer._tasks.values())).add_done_callback(advance_after_completion)
        client.release.set()
        await asyncio.gather(first, second)
        assert len(completed) == 1 and replacements[0] is not None
        assert store.get_session_observation(conv.id) == replacements[0]
        assert conv.session_observation == second_conv.session_observation == completed[0]
        assert completed[0].status == previous.status
        assert completed[0].response_id == previous.response_id
        assert completed[0].error_key == previous.error_key
    finally:
        client.release.set()
        await asyncio.gather(
            *[task for task in (first, second) if task is not None], return_exceptions=True
        )


@pytest.mark.asyncio
async def test_native_ensure_initialization_keeps_normal_failure_without_passive_recovery(
    native_init_store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.routes import sessions as sessions_routes

    store = native_init_store
    conv, _, client, initializer = _native_initializer(store)
    previous = store.record_session_observation(
        conv.id, "failed", response_id="old-response", error_key="old-error"
    )
    conv = store.get_conversation(conv.id)
    assert previous is not None and conv is not None
    client.release.set()

    async def no_recovery(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("native metadata-only completion started passive recovery")

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sessions_routes, "_publish_runner_recovered_status", no_recovery)
    monkeypatch.setattr(sessions_routes, "_ensure_runner_relay_ready", noop)
    monkeypatch.setattr("omnigent.server.child_session_recovery.restore_active_children", noop)
    ready = await sessions_routes._ensure_runner_session_initialized(
        conv.id, conv, client, store, initializer=initializer, require_success=True
    )
    assert ready
    current = store.get_conversation(conv.id)
    assert current is not None and not current.terminal_pending
    assert conv.session_observation == current.session_observation
    assert current.live_status == previous.status
    assert current.session_observation.response_id == previous.response_id
    assert current.session_observation.error_key == previous.error_key


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "failed"])
async def test_cached_initializer_readiness_does_not_rewind_a_later_turn(
    native_init_store: SqlAlchemyConversationStore, status: str
) -> None:
    store = native_init_store
    conv, _, client, initializer = _native_initializer(store)
    client.release.set()
    first = await initializer.initialize(conv, client, timeout=10)
    completed = first.extensions["session_observation"]
    newer = store.record_session_observation(
        conv.id,
        status,
        response_id="newer-turn",
        error_key="newer-error" if status == "failed" else None,
    )
    assert newer is not None and newer.revision > completed.revision
    current = store.get_conversation(conv.id)
    assert current is not None
    cached = await initializer.initialize(current, client, timeout=10)
    assert cached is first and len(client.calls) == 1
    assert current.session_observation == newer
    assert store.get_session_observation(conv.id) == newer


@pytest.mark.asyncio
async def test_native_initializer_offline_startup_retry_keeps_previous_real_failure_payload(
    native_init_store: SqlAlchemyConversationStore,
) -> None:
    store = native_init_store
    conv, _, client, initializer = _native_initializer(store)
    failure = {
        "omnigent.last_task_error_code": "native_turn_error",
        "omnigent.last_task_error_message": "real previous failure",
    }
    previous = store.record_session_observation(
        conv.id,
        "failed",
        response_id="previous-turn",
        error_key="previous-error",
        error_labels=failure,
    )
    assert previous is not None
    startup = store.begin_terminal_startup(conv.id, previous, "interrupted-startup")
    assert startup is not None
    assert (
        store.record_runner_offline_decision(
            conv.id,
            RunnerOfflineDecision(
                observation=startup,
                episode_id="interrupted-startup-episode",
                error_labels={
                    "omnigent.last_task_error_code": "runner_disconnected",
                    "omnigent.last_task_error_message": "startup runner disappeared",
                },
            ),
        )
        is not None
    )
    conv = store.get_conversation(conv.id)
    assert conv is not None
    client.release.set()
    await initializer.initialize(conv, client, timeout=10)
    current = store.get_conversation(conv.id)
    assert current is not None
    assert current.session_observation.status == previous.status
    assert current.session_observation.response_id == previous.response_id
    assert current.session_observation.error_key == previous.error_key
    assert current.runner_offline_decision is None and not current.terminal_pending
    assert current.live_status_error_labels == failure
    assert conv.live_status_error_labels == failure


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 500, 503])
async def test_configured_initializer_preserves_the_runner_rejection(status: int) -> None:
    conversation = _conversation()
    conversation.inference_snapshot = {"runtime_config": {"providers": {}}}
    client = _Client()
    client.status_code = status
    client.response_json = {
        "error": "real_runner_failure",
        "detail": "runner rejected this startup",
    }
    client.release.set()
    initializer = RunnerSessionInitializer(_Registry(), server_version="test")  # type: ignore[arg-type]
    response = await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert response.status_code == status
    assert response.json() == client.response_json


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_response", [None, "turn-a"])
async def test_prior_turn_stop_during_initialize_keeps_the_original_startup_claim(
    native_init_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    previous_response: str | None,
) -> None:
    from omnigent.server.routes._sessions import common, helpers, orchestration

    store = native_init_store
    conv, _, client, initializer = _native_initializer(store)
    previous = store.record_session_observation(conv.id, "running", response_id=previous_response)
    assert previous is not None
    conv = store.get_conversation(conv.id)
    assert conv is not None
    entered, release = asyncio.Event(), asyncio.Event()
    complete = orchestration._complete_native_terminal_startup

    async def held_completion(
        session_id: str, owner: SessionObservation
    ) -> SessionObservation | None:
        entered.set()
        await release.wait()
        return await complete(session_id, owner)

    monkeypatch.setattr(orchestration, "_complete_native_terminal_startup", held_completion)
    task = asyncio.create_task(initializer.initialize(conv, client, timeout=10))
    try:
        await asyncio.wait_for(client.entered.wait(), 10)
        startup = client.at_post[0].session_observation
        client.release.set()
        await asyncio.wait_for(entered.wait(), 10)
        stopped = helpers._publish_status(conv.id, "idle", response_id="turn-a", owner=startup)
        assert stopped is not None
        idle = await asyncio.wrap_future(stopped)
        assert idle is not None and idle.response_id == "turn-a" and idle.status == "idle"
        current = store.get_conversation(conv.id)
        assert current is not None and current.terminal_pending
        release.set()
        response = await asyncio.wait_for(task, 10)
        completed = response.extensions["session_observation"]
        assert response.status_code == 201 and initializer.is_initialized(conv, client)
        assert len(client.calls) == 1
        assert completed.status == "idle" and completed.response_id == "turn-a"
        assert completed.revision == idle.revision + 1
        current = store.get_conversation(conv.id)
        assert current is not None and current.session_observation == completed
        assert not current.terminal_pending and current.labels == conv.labels
        projection = common._session_status_cache.projection(conv.id)
        assert projection is not None and projection.observation == completed
        assert not common._session_terminal_pending_cache.get(conv.id, False)
    finally:
        client.release.set()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
