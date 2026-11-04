"""Integration tests shared by claude-sdk ``/clear`` and ``/rewind``.

Both continue a session in a new one on the same placement through one server
path. These scenarios hold for either command; each command's own suite keeps
its happy path and command-specific refusals.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from starlette.datastructures import Headers

from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import HostHelloFrame
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, RESERVED_USER_PUBLIC
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes._sessions.helpers import _HostLaunchAttempt
from omnigent.server.routes.sessions import routes_events
from omnigent.server.schemas import SessionEventInput
from omnigent.server.sharing_settings import DefaultPublicSessions
from omnigent.server.startup_inputs import ManagedStartupInputs
from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY, SIDE_CHAT_SOURCE_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.integration._session_continuation import (
    _COMPACT_INPUT,
    _HOST_ID,
    _MESSAGE_INPUT,
    _OWNER,
    _READER,
    _RUNNER_ID,
    _SKILL_INPUT,
    _STRANGER,
    LiveRunner,
    bind_to_host,
    continue_session,
    create_placed_session,
    install_managed_sandbox,
    message_texts,
    resolve_harness_as,
)
from tests.server.integration._session_continuation import (
    auth_live_runner as auth_live_runner,
)
from tests.server.integration._session_continuation import (
    live_runner as live_runner,
)

pytestmark = pytest.mark.asyncio

COMMANDS = ["clear", "rewind"]


@pytest.mark.parametrize(
    ("deleted_first", "survivor_archived"),
    [
        pytest.param("original", False, id="original"),
        pytest.param("continuation", False, id="continuation"),
        pytest.param("original", True, id="original-archived-survivor"),
    ],
)
@pytest.mark.parametrize("command", COMMANDS)
async def test_deleting_one_side_keeps_their_shared_sandbox(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    deleted_first: str,
    survivor_archived: bool,
) -> None:
    """Both sessions work in one managed sandbox.

    Deleting either leaves the other bound to the host and the sandbox
    running, even when the survivor is archived; deleting the last one still
    tears the sandbox down.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    host_store, launcher = install_managed_sandbox(app, db_uri, monkeypatch, f"sb-{command}")
    source_id = await create_placed_session(client, store)
    continued = await continue_session(client, store, source_id, command)
    assert continued.is_success, continued.text
    new_id = continued.json()["session_id"]
    first, last = (source_id, new_id) if deleted_first == "original" else (new_id, source_id)
    if survivor_archived:
        monkeypatch.setattr(sessions_module, "_ARCHIVE_STOP_UNDO_GRACE_S", 0.0)
        archived = await client.patch(f"/v1/sessions/{last}", json={"archived": True})
        assert archived.status_code == 200, archived.text
        await asyncio.gather(*list(orchestration._detached_stop_tasks), return_exceptions=True)

    deleted = await client.delete(f"/v1/sessions/{first}")

    assert deleted.status_code == 200, deleted.text
    survivor = store.get_conversation(last)
    assert survivor is not None and survivor.host_id == _HOST_ID
    assert host_store.get_host(_HOST_ID) is not None
    assert launcher.terminated == []

    deleted = await client.delete(f"/v1/sessions/{last}")

    assert deleted.status_code == 200, deleted.text
    assert launcher.terminated == [f"sb-{command}"]
    assert host_store.get_host(_HOST_ID) is None


@dataclass
class Handoff:
    """The app and client a continuation runs on, with a live runner installed."""

    app: FastAPI
    client: httpx.AsyncClient
    owner: str | None


@pytest.fixture
def handoff(request: pytest.FixtureRequest) -> Handoff:
    """Signed in as ``request.param`` on the app with permissions, else the shared app.

    A sync fixture, so it can pick the async app fixtures before the test's loop runs.
    """
    owner: str | None = getattr(request, "param", None)
    prefix = "auth_" if owner is not None else ""
    client: httpx.AsyncClient = request.getfixturevalue(f"{prefix}client")
    request.getfixturevalue(f"{prefix}live_runner")
    if owner is not None:
        client.headers["X-Forwarded-Email"] = owner
    return Handoff(app=request.getfixturevalue(f"{prefix}app"), client=client, owner=owner)


@pytest.mark.parametrize(
    ("deleted", "handoff"),
    [
        ("before_fork", None),
        ("after_fork", None),
        # Signed in with the runner offline: nothing but the copied sharing
        # gives the owner the continuation, and the deleted original has none.
        ("after_fork", _OWNER),
        ("after_fork_then_failure", None),
        ("not_then_failure", None),
    ],
    indirect=["handoff"],
)
@pytest.mark.parametrize("command", COMMANDS)
async def test_deleting_the_original_during_the_handoff_never_strands_the_continuation(
    handoff: Handoff,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    deleted: str,
) -> None:
    """A delete of the original racing the command never leaves a sandbox-less continuation.

    Once the continuation exists it already holds the host, so the delete keeps
    the sandbox; a delete that lands first leaves no continuation behind. If
    the continuation then fails to be placed, or no one would own it, removing
    it releases the sandbox it was the last user of, but not one the original
    still uses.
    """
    app, client, owner = handoff.app, handoff.client, handoff.owner
    signed_in = owner is not None
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    host_store, launcher = install_managed_sandbox(app, db_uri, monkeypatch, f"sb-{command}")
    source_id = await create_placed_session(
        client, store, runner_id=None if signed_in else _RUNNER_ID, user=owner
    )
    before = len((await client.get("/v1/sessions")).json()["data"])
    if deleted.endswith("then_failure"):
        resolve_runner = sessions_module._get_runner_client

        async def reject_the_continuation(session_id: str, *args: Any, **kwargs: Any) -> Any:
            if session_id != source_id:
                raise OmnigentError(
                    "the runner rejected the new session", code=ErrorCode.RUNNER_UNAVAILABLE
                )
            return await resolve_runner(session_id, *args, **kwargs)

        monkeypatch.setattr(sessions_module, "_get_runner_client", reject_the_continuation)
    loop = asyncio.get_running_loop()
    paused, resume = asyncio.Event(), threading.Event()
    fork = SqlAlchemyConversationStore.fork_conversation

    def fork_around_the_delete(self: Any, *args: Any, **kwargs: Any) -> Any:
        forked = fork(self, *args, **kwargs) if deleted != "before_fork" else None
        loop.call_soon_threadsafe(paused.set)
        assert resume.wait(timeout=10)
        return forked if forked is not None else fork(self, *args, **kwargs)

    monkeypatch.setattr(SqlAlchemyConversationStore, "fork_conversation", fork_around_the_delete)
    continuing = asyncio.create_task(continue_session(client, store, source_id, command))
    await asyncio.wait_for(paused.wait(), timeout=10)
    if deleted != "not_then_failure":
        removed = await client.delete(f"/v1/sessions/{source_id}")
        assert removed.status_code == 200, removed.text
    resume.set()
    continued = await asyncio.wait_for(continuing, timeout=10)

    if deleted == "not_then_failure":
        # The original still uses the sandbox, so only the continuation goes.
        assert continued.status_code >= 400, continued.text
        assert len((await client.get("/v1/sessions")).json()["data"]) == before
        original = store.get_conversation(source_id)
        assert original is not None and original.host_id == _HOST_ID
        assert host_store.get_host(_HOST_ID) is not None
        assert launcher.terminated == []
        return
    if deleted != "after_fork" or signed_in:
        # No session is left on the sandbox, so it is torn down.
        assert continued.status_code >= 400, continued.text
        assert len((await client.get("/v1/sessions")).json()["data"]) == before - 1
        assert launcher.terminated == [f"sb-{command}"]
        assert host_store.get_host(_HOST_ID) is None
        return
    assert continued.is_success, continued.text
    continuation = store.get_conversation(continued.json()["session_id"])
    assert continuation is not None and continuation.host_id == _HOST_ID
    assert host_store.get_host(_HOST_ID) is not None
    assert launcher.terminated == []


@pytest.mark.parametrize("command", COMMANDS)
async def test_a_continuation_that_fails_after_runner_setup_leaves_nothing_behind(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """A step failing after the runner set the new session up leaves nothing behind.

    Detaching the original from the runner fails here: the new session is
    deleted and dropped from the runner, was never listed in the owner's tabs,
    and the original keeps its history with no notice or redirect.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store)
    monkeypatch.setattr(
        SqlAlchemyConversationStore,
        "clear_runner_id",
        Mock(side_effect=OmnigentError("detach failed", code=ErrorCode.INTERNAL_ERROR)),
    )
    before = len((await client.get("/v1/sessions")).json()["data"])

    resp = await continue_session(client, store, source_id, command)

    assert resp.status_code == 500, resp.text
    initialized = [
        body["session_id"] for path, body in live_runner.posts if path == "/v1/sessions"
    ]
    [new_id] = [session_id for session_id in initialized if session_id != source_id]
    assert store.get_conversation(new_id) is None
    assert (f"/v1/sessions/{new_id}", {}) in live_runner.posts
    assert new_id not in live_runner.announced
    assert len((await client.get("/v1/sessions")).json()["data"]) == before
    assert message_texts(store, source_id) == ["Q1", "A1", "Q2", "A2"]
    assert live_runner.superseded == []


@pytest.mark.parametrize("host_live", [False, True], ids=["offline", "on-another-replica"])
@pytest.mark.parametrize("command", COMMANDS)
async def test_tells_an_offline_runner_from_one_on_another_replica(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    host_live: bool,
) -> None:
    """An offline runner leaves the new session on the host, which relaunches it on send.

    A runner absent here whose host is live elsewhere is on another replica, so
    the command re-addresses there before anything is created or sent.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store, runner_id="runner_gone")
    host_store = HostStore(db_uri)
    host_store.upsert_on_connect(host_id=_HOST_ID, name="laptop", user_id="local")
    if not host_live:
        host_store.set_offline(_HOST_ID)
    monkeypatch.setattr(app.state, "host_store", host_store)
    before = len((await client.get("/v1/sessions")).json()["data"])
    posts_before = len(live_runner.posts)

    resp = await continue_session(client, store, source_id, command)

    if host_live:
        assert resp.status_code == 400, resp.text
        assert "wrong_replica" in resp.text
        assert len((await client.get("/v1/sessions")).json()["data"]) == before
        assert live_runner.posts[posts_before:] == []
        assert live_runner.superseded == []
        return
    assert resp.is_success, resp.text
    new_id = resp.json()["session_id"]
    fork = store.get_conversation(new_id)
    assert fork is not None
    assert (fork.runner_id, fork.host_id, fork.workspace) == (None, _HOST_ID, "/work/repo")

    # Its next message asks the original host to launch a runner in the same workspace.
    app.state.host_registry.register(
        _HOST_ID,
        AsyncMock(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="laptop", runners=[]),
        owner=None,
    )
    monkeypatch.setattr(sessions_module, "_get_runner_client", AsyncMock(return_value=None))
    launches: list[tuple[str, str | None, str | None]] = []

    async def launch_runner_on_host(conv: Any, *_args: Any, **_kwargs: Any) -> Any:
        launches.append((conv.id, conv.host_id, conv.workspace))
        return _HostLaunchAttempt(
            runner_id="runner_relaunched", error_code="harness_not_configured", error="stop here"
        )

    monkeypatch.setattr(sessions_module, "_launch_runner_on_host", launch_runner_on_host)
    sent = await client.post(
        f"/v1/sessions/{new_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "Q3"}]},
        },
    )
    assert sent.status_code == 202, sent.text
    assert launches == [(new_id, _HOST_ID, "/work/repo")]


@pytest.mark.parametrize(
    ("left", "host"),
    [
        pytest.param("before_validation", True, id="before_validation-host-bound"),
        pytest.param("before_validation", False, id="before_validation-runner-only"),
        pytest.param("after_validation", True, id="after_validation-host-bound"),
        pytest.param("after_validation", False, id="after_validation-runner-only"),
        # Only a host tells another replica's runner apart from a gone one.
        pytest.param("to_another_replica", True, id="to_another_replica-host-bound"),
    ],
)
@pytest.mark.parametrize("command", COMMANDS)
async def test_survives_the_runner_leaving_before_it_is_bound(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    left: str,
    host: bool,
) -> None:
    """A runner that disconnects after the check leaves no half-made session.

    It leaves before the new session's runner is validated (during ``/clear``'s
    release, or while ``/rewind``, which frees no client, makes the new
    session) or just after, when binding still reports success. A host-bound
    session then continues on the host alone; otherwise the new session is
    removed, so a retry does not leave a second copy. A runner that reconnected
    to another replica is re-addressed there instead.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store, host=host)

    def leave() -> None:
        app.state.tunnel_registry.deregister(_RUNNER_ID)

    if left == "after_validation":
        resolve_runner = sessions_module._get_runner_client

        async def lose_runner_at_lookup(session_id: str, *args: Any, **kwargs: Any) -> Any:
            if session_id != source_id:
                leave()
                return None
            return await resolve_runner(session_id, *args, **kwargs)

        monkeypatch.setattr(sessions_module, "_get_runner_client", lose_runner_at_lookup)
    elif command == "clear":
        live_runner.on_clear = leave
    else:
        fork_conversation = SqlAlchemyConversationStore.fork_conversation

        def fork_then_leave(self: SqlAlchemyConversationStore, *args: Any, **kwargs: Any) -> Any:
            forked = fork_conversation(self, *args, **kwargs)
            leave()
            return forked

        monkeypatch.setattr(SqlAlchemyConversationStore, "fork_conversation", fork_then_leave)
    if left == "to_another_replica":
        host_store = HostStore(db_uri)
        host_store.upsert_on_connect(host_id=_HOST_ID, name="laptop", user_id="local")
        monkeypatch.setattr(app.state, "host_store", host_store)
    before = len((await client.get("/v1/sessions")).json()["data"])

    announced_before = list(live_runner.announced)
    resp = await continue_session(client, store, source_id, command)

    if left == "to_another_replica":
        assert resp.status_code == 400, resp.text
        assert "wrong_replica" in resp.text
    elif host:
        assert resp.is_success, resp.text
        new_id = resp.json()["session_id"]
        fork = store.get_conversation(new_id)
        assert fork is not None
        # Placed on the host alone, holding no runner that went away.
        assert (fork.runner_id, fork.host_id, fork.workspace) == (None, _HOST_ID, "/work/repo")
        assert live_runner.superseded == [(source_id, new_id, command)]
        return
    else:
        assert resp.status_code == 503, resp.text
        assert "runner is offline" in resp.text
    assert len((await client.get("/v1/sessions")).json()["data"]) == before
    assert live_runner.superseded == []
    # A continuation that was never placed was never listed in the owner's tabs.
    assert live_runner.announced == announced_before


@pytest.mark.parametrize("command", COMMANDS)
async def test_stopping_or_archiving_the_original_leaves_the_continuation_running(
    client: httpx.AsyncClient,
    db_uri: str,
    live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """The original no longer holds the runner it shares with its continuation.

    Stopping or archiving the original ends only that chat: the runner is not
    stopped under the continuation's running turn, and the original keeps its
    host so a later message can resume it.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(client, store)
    continued = await continue_session(client, store, source_id, command)
    assert continued.is_success, continued.text
    new_id = continued.json()["session_id"]
    monkeypatch.setitem(sessions_module._session_status_cache, new_id, "running")
    runner_stops = AsyncMock(return_value=True)
    monkeypatch.setattr(routes_events, "_stop_host_runner_intentionally", runner_stops)
    monkeypatch.setattr(orchestration, "_stop_host_runner_intentionally", runner_stops)
    monkeypatch.setattr(sessions_module, "_ARCHIVE_STOP_UNDO_GRACE_S", 0.0)

    stopped = await client.post(
        f"/v1/sessions/{source_id}/events", json={"type": "stop_session", "data": {}}
    )
    archived = await client.patch(f"/v1/sessions/{source_id}", json={"archived": True})
    await asyncio.gather(*list(orchestration._detached_stop_tasks), return_exceptions=True)

    assert stopped.status_code < 300, stopped.text
    assert archived.status_code == 200, archived.text
    runner_stops.assert_not_awaited()
    continuation = store.get_conversation(new_id)
    assert continuation is not None and continuation.runner_id == _RUNNER_ID
    assert sessions_module._session_status_cache.get(new_id) == "running"
    original = store.get_conversation(source_id)
    assert original is not None
    assert (original.runner_id, original.host_id) == (None, _HOST_ID)


@pytest.mark.parametrize(
    ("placement", "status", "message"),
    [
        ("running", 400, "Stop the current turn"),
        # An input still on its way to the runner counts as a running turn,
        # even when the session shows as failed.
        ("starting", 400, "Stop the current turn"),
        ("skill_starting", 400, "Stop the current turn"),
        # A claude-sdk compact runs a turn too.
        ("compact_starting", 400, "Stop the current turn"),
        ("failed_starting", 400, "Stop the current turn"),
        ("startup_pending", 400, "Wait for this session's first message"),
        ("none", 503, "runner is offline"),
        ("sub_agent", 400, "only available in a top-level session"),
        ("side_chat", 400, "isn't available in a side chat"),
        ("moving", 409, "already moving to a new session"),
    ],
)
@pytest.mark.parametrize("command", COMMANDS)
async def test_refusals_create_no_session(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    db_uri: str,
    auth_live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    placement: str,
    status: int,
    message: str,
) -> None:
    """A busy, unplaced, nested or already moving session refuses the command.

    A running turn, an input (or an SDK compact) still reaching the runner or
    saved for startup, no runner or host, a sub-agent or side chat, or a move
    already in progress creates, initializes and supersedes nothing, and the
    session stays open.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(
        auth_client,
        store,
        runner_id=None
        if placement == "none"
        else "runner_gone"
        if placement.endswith("starting")
        else _RUNNER_ID,
        host=placement != "none",
        user=_OWNER,
    )
    message_task: asyncio.Task[httpx.Response] | None = None
    release_message = asyncio.Event()
    if placement.endswith("starting"):
        # An input to the offline runner waits on runner recovery; a failed
        # session still shows "failed" while its recovery input is pending.
        if placement == "failed_starting":
            monkeypatch.setitem(sessions_module._session_status_cache, source_id, "failed")
        recovering = asyncio.Event()
        resolve_runner = sessions_module._get_runner_client

        async def recover_runner(*args: Any, **kwargs: Any) -> Any:
            if not recovering.is_set():
                recovering.set()
                await release_message.wait()
            return await resolve_runner(*args, **kwargs)

        monkeypatch.setattr(sessions_module, "_get_runner_client", recover_runner)
        message_task = asyncio.create_task(
            auth_client.post(
                f"/v1/sessions/{source_id}/events",
                json={
                    "skill_starting": _SKILL_INPUT,
                    "compact_starting": _COMPACT_INPUT,
                }.get(placement, _MESSAGE_INPUT),
                headers={"X-Forwarded-Email": _OWNER},
            )
        )
        await asyncio.wait_for(recovering.wait(), timeout=10)
    elif placement == "startup_pending":
        # A saved first message the startup service has yet to deliver.
        startup = ManagedStartupInputs(store, auth_app.state.host_registry)
        monkeypatch.setattr(auth_app.state, "startup_inputs", startup)
        await startup.enqueue(
            source_id, SessionEventInput(**_MESSAGE_INPUT), user_id=_OWNER, headers=Headers()
        )
    elif placement == "running":
        monkeypatch.setitem(sessions_module._session_status_cache, source_id, "running")
    elif placement in ("sub_agent", "side_chat"):
        source = store.get_conversation(source_id)
        assert source is not None
        if placement == "sub_agent":
            nested = store.create_conversation(
                kind="sub_agent", parent_conversation_id=source_id, agent_id=source.agent_id
            )
            bind_to_host(store, nested.id, "/work/repo")
        else:
            # A side chat rides its source's runner and has no host of its own.
            nested = store.create_conversation(
                agent_id=source.agent_id,
                labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: source_id},
            )
        store.replace_runner_id(nested.id, _RUNNER_ID)
        SqlAlchemyPermissionStore(db_uri).grant(_OWNER, nested.id, LEVEL_OWNER)
        # /rewind checks its prompt before the session.
        store.append(
            nested.id,
            [
                NewConversationItem(
                    type="message",
                    response_id="resp_nested",
                    data=MessageData(
                        role="user", content=[{"type": "input_text", "text": "Nested"}]
                    ),
                )
            ],
        )
        source_id = nested.id
    headers = {"X-Forwarded-Email": _OWNER}
    before = len((await auth_client.get("/v1/sessions", headers=headers)).json()["data"])
    posts_before = len(auth_live_runner.posts)

    try:
        moving = (
            orchestration._mark_session_moving(source_id)
            if placement == "moving"
            else contextlib.nullcontext()
        )
        with moving:
            resp = await continue_session(auth_client, store, source_id, command, headers)
    finally:
        release_message.set()
        if message_task is not None:
            await message_task

    assert resp.status_code == status, resp.text
    assert message in resp.text
    after = len((await auth_client.get("/v1/sessions", headers=headers)).json()["data"])
    assert after == before
    assert auth_live_runner.superseded == []
    # Nothing reached the runner: no release, no new session initialized.
    sent = auth_live_runner.posts[posts_before:]
    assert [body for _path, body in sent if body.get("type") == "clear"] == []
    assert "/v1/sessions" not in [path for path, _body in sent]
    if placement != "moving":
        # A refused command leaves the session open to messages.
        assert not orchestration._session_is_moving(source_id)


@pytest.mark.parametrize("public", ["revoked", "kept"])
@pytest.mark.parametrize("command", COMMANDS)
async def test_continuation_keeps_the_original_sharing(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    db_uri: str,
    auth_live_runner: LiveRunner,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    public: str,
) -> None:
    """The session the command continues in is shared exactly as the original.

    Live viewers of the original are all redirected to it, so a reader can
    open it. It gets no creation-default sharing, even with every new session
    starting public: an original whose public link was revoked stays private,
    at every point of the handoff, while one still public stays public.
    """
    resolve_harness_as(monkeypatch, "claude-sdk")
    monkeypatch.setattr(
        auth_app.state, "default_public_sessions", lambda: DefaultPublicSessions.ALL
    )
    store = SqlAlchemyConversationStore(db_uri)
    source_id = await create_placed_session(auth_client, store, user=_OWNER)
    owner, reader = {"X-Forwarded-Email": _OWNER}, {"X-Forwarded-Email": _READER}
    stranger = {"X-Forwarded-Email": _STRANGER}
    shared = await auth_client.put(
        f"/v1/sessions/{source_id}/permissions",
        json={"user_id": _READER, "level": LEVEL_READ},
        headers=owner,
    )
    assert shared.status_code < 300, shared.text
    if public == "revoked":
        revoked = await auth_client.delete(
            f"/v1/sessions/{source_id}/permissions/{RESERVED_USER_PUBLIC}", headers=owner
        )
        assert revoked.status_code < 300, revoked.text
    assert (await auth_client.get(f"/v1/sessions/{source_id}", headers=stranger)).status_code == (
        200 if public == "kept" else 404
    )
    stranger_reads_while_placing: list[int] = []
    resolve_runner = sessions_module._get_runner_client

    async def read_as_stranger_while_placing(session_id: str, *args: Any, **kwargs: Any) -> Any:
        if session_id != source_id and not stranger_reads_while_placing:
            opened = await auth_client.get(f"/v1/sessions/{session_id}", headers=stranger)
            stranger_reads_while_placing.append(opened.status_code)
        return await resolve_runner(session_id, *args, **kwargs)

    monkeypatch.setattr(sessions_module, "_get_runner_client", read_as_stranger_while_placing)

    continued = await continue_session(auth_client, store, source_id, command, owner)

    assert continued.is_success, continued.text
    new_id = continued.json()["session_id"]
    assert auth_live_runner.superseded == [(source_id, new_id, command)]
    opened = await auth_client.get(f"/v1/sessions/{new_id}", headers=reader)
    assert opened.status_code == 200, opened.text
    # Read-only, as on the original.
    renamed = await auth_client.patch(
        f"/v1/sessions/{new_id}", json={"title": "mine"}, headers=reader
    )
    assert renamed.status_code == 403, renamed.text
    expected = 200 if public == "kept" else 404
    assert stranger_reads_while_placing == [expected]
    assert (await auth_client.get(f"/v1/sessions/{new_id}", headers=stranger)).status_code == (
        expected
    )
