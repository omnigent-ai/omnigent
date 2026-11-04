"""Integration tests shared by claude-sdk ``/clear`` and ``/rewind``.

Both continue a session in a new one on the same placement through one server
path. These scenarios hold for either command; each command's own suite keeps
its happy path and command-specific refusals.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import HostHelloFrame
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes._sessions.helpers import _HostLaunchAttempt
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from tests.server.integration._session_continuation import (
    _HOST_ID,
    _OWNER,
    _RUNNER_ID,
    LiveRunner,
    continue_session,
    create_placed_session,
    install_managed_sandbox,
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
