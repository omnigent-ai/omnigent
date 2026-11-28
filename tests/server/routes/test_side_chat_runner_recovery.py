"""Side chats recover their shared runner through their source session."""

import asyncio
import dataclasses
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from omnigent.db.utils import generate_agent_id
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import CAP_CODEX_SIDE_CHAT_RESUME
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ
from omnigent.server.routes import sessions
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes.sessions import routes_events
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY, SIDE_CHAT_SOURCE_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.budgets import budget
from tests.server.helpers import create_test_agent

_HOST_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
_OWNER = "alice@example.com"
_OTHER = "bob@example.com"


def _source_and_side_chat(
    store: SqlAlchemyConversationStore, db_uri: str
) -> tuple[Conversation, Conversation]:
    """Create a host-bound source and a side chat still bound to its old runner."""
    agent_id = generate_agent_id()
    SqlAlchemyAgentStore(db_uri).create(agent_id, name="test", bundle_location="test:///bundle")
    source = store.create_conversation(agent_id=agent_id)
    store.set_host_id(source.id, _HOST_ID, workspace="/workspace")
    store.set_runner_id(source.id, "runner-replacement")
    side = store.fork_conversation(
        source.id,
        extra_labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: source.id},
    )
    store.set_runner_id(side.id, "runner-exited")
    saved_source = store.get_conversation(source.id)
    saved_side = store.get_conversation(side.id)
    assert saved_source is not None and saved_side is not None
    return saved_source, saved_side


async def _recover(
    store: SqlAlchemyConversationStore,
    side: Conversation,
    *,
    user_id: str | None = None,
    permission_store: SqlAlchemyPermissionStore | None = None,
    runner_router: Mock | None = None,
) -> tuple[httpx.AsyncClient | None, Conversation]:
    return await orchestration._recover_side_chat_runner_via_source(
        side,
        app_state=SimpleNamespace(),
        conversation_store=store,
        runner_router=runner_router,
        user_id=user_id,
        permission_store=permission_store,
    )


async def test_side_chat_rebinds_to_source_runner(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    source, side = _source_and_side_chat(store, db_uri)
    runner_client = httpx.AsyncClient(base_url="http://runner")
    ensure = AsyncMock(return_value=(runner_client, source))
    monkeypatch.setattr(orchestration, "ensure_runner_connected", ensure)
    monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=runner_client))

    try:
        client, recovered = await _recover(store, side)
    finally:
        await runner_client.aclose()

    assert client is runner_client
    assert recovered.runner_id == "runner-replacement"
    assert ensure.await_args.kwargs["session_id"] == source.id
    saved = store.get_conversation(side.id)
    assert saved is not None and saved.runner_id == "runner-replacement"
    assert saved.host_id is None


@pytest.mark.parametrize("source_level", [None, LEVEL_READ])
async def test_side_chat_recovery_requires_edit_access_to_source(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, source_level: int | None
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    source, side = _source_and_side_chat(store, db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in (_OWNER, _OTHER):
        permissions.ensure_user(user)
    permissions.grant(_OWNER, source.id, LEVEL_OWNER)
    permissions.grant(_OTHER, side.id, LEVEL_EDIT)
    if source_level is not None:
        permissions.grant(_OTHER, source.id, source_level)
    ensure = AsyncMock()
    monkeypatch.setattr(orchestration, "ensure_runner_connected", ensure)

    client, unchanged = await _recover(store, side, user_id=_OTHER, permission_store=permissions)

    assert client is None and unchanged is side
    ensure.assert_not_awaited()
    saved = store.get_conversation(side.id)
    assert saved is not None and saved.runner_id == "runner-exited"


async def test_side_chat_binding_unchanged_when_source_runner_unavailable(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    source, side = _source_and_side_chat(store, db_uri)
    ensure = AsyncMock(return_value=(None, source))
    monkeypatch.setattr(orchestration, "ensure_runner_connected", ensure)

    client, unchanged = await _recover(store, side)

    assert client is None and unchanged is side
    ensure.assert_awaited_once()
    saved = store.get_conversation(side.id)
    assert saved is not None and saved.runner_id == "runner-exited"


@pytest.mark.parametrize(
    ("state", "status", "payload", "succeeds"),
    [
        ("replaced", 200, {"resumed": True}, True),
        ("unavailable", 200, {"resumed": True}, False),
        ("live", 422, {"detail": [{"type": "union_tag_invalid"}]}, True),
        ("closed", 200, {"queued": False}, False),
        ("closed", 409, {"error": "no_live_harness"}, False),
        ("closed", 422, {"detail": [{"type": "union_tag_invalid"}]}, False),
        ("unbound", 503, {"error": "no_harness"}, False),
        ("mismatched", 200, {"queued": False}, False),
        ("reappeared", 200, {"queued": False}, False),
        ("advanced", 409, {"error": "no_live_harness"}, False),
        ("offline", 503, {"error": "no_harness"}, False),
        ("closed", 503, {"error": "codex_side_chat_unavailable"}, False),
        ("closed", 200, {"resumed": False}, False),
        ("legacy-reappeared", 422, {"detail": [{"type": "union_tag_invalid"}]}, True),
        ("legacy-closed", 422, {"detail": [{"type": "union_tag_invalid"}]}, False),
        ("legacy-mismatched", 422, {"detail": [{"type": "union_tag_invalid"}]}, False),
    ],
)
async def test_native_side_chat_resume_revalidates_runner_after_parent_initialization(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    status: int,
    payload: dict[str, Any],
    succeeds: bool,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    source = store.create_conversation(runner_id="runner-first")
    side = store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=source.id,
        runner_id=None if state == "unbound" else "runner-first",
    )
    if state in ("mismatched", "legacy-mismatched"):
        store.replace_runner_id(side.id, "runner-stale")
    closed = state in ("replaced", "unavailable", "closed", "advanced", "legacy-closed")
    store.set_labels(
        side.id,
        {
            "omnigent.wrapper": "codex-native-ui-subagent",
            "omnigent.codex_native.agent_nickname": "Side chat",
            "omnigent.codex_native.subagent_thread_id": "thread-side",
            **({"omnigent.closed": "true"} if closed else {}),
        },
    )
    side = store.get_conversation(side.id)
    assert side is not None
    first_runner = AsyncMock()
    next_runner = AsyncMock()
    first_runner.post.return_value = next_runner.post.return_value = httpx.Response(
        status, json=payload
    )
    runners = {"runner-first": first_runner, "runner-next": next_runner}

    async def ensure(*, conv: Conversation, **_kwargs: object):
        current = store.get_conversation(conv.id)
        assert current is not None
        if current.runner_id == "runner-next" and state == "unavailable":
            return None, current
        return runners[current.runner_id], current

    parent_probes = 0

    async def client_for(session_id: str, *_args: object, **_kwargs: object):
        nonlocal parent_probes
        if session_id == source.id:
            parent_probes += 1
            if state == "offline" or (
                state in ("reappeared", "legacy-reappeared") and parent_probes == 1
            ):
                return None
        current = store.get_conversation(session_id)
        assert current is not None
        return runners[current.runner_id]

    async def initialize_parent(**kwargs: object) -> None:
        assert kwargs["session_id"] == source.id
        if state in ("replaced", "unavailable", "advanced"):
            store.replace_runner_id(source.id, "runner-next")

    ensure_connected = AsyncMock(side_effect=ensure)
    initialize = AsyncMock(side_effect=initialize_parent)
    monkeypatch.setattr(orchestration, "ensure_runner_connected", ensure_connected)
    monkeypatch.setattr(sessions, "_get_runner_client", client_for)
    monkeypatch.setattr(routes_events, "_recover_retry_session", initialize)
    kwargs = {
        "request": SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        "conv": side,
        "conversation_store": store,
        "runner_router": SimpleNamespace(
            runner_is_online=lambda _runner_id: True,
            runner_has_capability=lambda _runner_id, capability: (
                capability == CAP_CODEX_SIDE_CHAT_RESUME and not state.startswith("legacy-")
            ),
            runner_owner=lambda _runner_id: None,
        ),
        "user_id": None,
        "permission_store": None,
    }
    expected_runner_id = (
        "runner-next" if state in ("replaced", "unavailable", "advanced") else "runner-first"
    )
    expected_runner = runners[expected_runner_id]
    if succeeds:
        recovered = await routes_events._resume_codex_side_chat(**kwargs)
        assert recovered.runner_id == expected_runner_id
        assert "omnigent.closed" not in recovered.labels
    else:
        with pytest.raises(OmnigentError) as raised:
            await routes_events._resume_codex_side_chat(**kwargs)
        assert raised.value.code == (
            ErrorCode.CONFLICT
            if status == 409 and state != "unavailable"
            else ErrorCode.RUNNER_UNAVAILABLE
        )
        saved = store.get_conversation(side.id)
        assert (saved.labels.get("omnigent.closed") == "true") == (state != "legacy-mismatched")
        if state == "legacy-mismatched":
            assert saved.runner_id == "runner-stale"
    if state in ("live", "legacy-reappeared", "legacy-closed", "legacy-mismatched"):
        if state == "legacy-mismatched":
            ensure_connected.assert_awaited_once()
        else:
            ensure_connected.assert_not_awaited()
        initialize.assert_not_awaited()
        first_runner.post.assert_not_awaited()
    elif state == "unavailable":
        next_runner.post.assert_not_awaited()
    else:
        expected_runner.post.assert_awaited_once_with(
            f"/v1/sessions/{source.id}/events",
            json={"type": "codex_side_chat_resume", "codex_side_session_id": side.id},
        )
    other_runner = next_runner if expected_runner is first_runner else first_runner
    other_runner.post.assert_not_awaited()
    if state == "mismatched":
        first_runner.post.return_value = httpx.Response(200, json={"resumed": True})
        kwargs["conv"] = store.get_conversation(side.id)
        assert kwargs["conv"].runner_id == "runner-first"

        recovered = await routes_events._resume_codex_side_chat(**kwargs)

        assert recovered.runner_id == "runner-first"
        assert "omnigent.closed" not in recovered.labels
        assert first_runner.post.await_count == 2
        assert first_runner.post.await_args == first_runner.post.await_args_list[0]


class _AdvancingSource:
    """Stage ``ensure_runner_connected`` with a stale snapshot, then the advanced row.

    The first call returns a snapshot pinned to *stale_runner*, as if taken
    before a concurrent recovery advanced the source; later calls return the
    advanced row, as they would after revalidation retries against the fresh
    source binding.
    """

    def __init__(self, advanced: str, stale_runner: str) -> None:
        self.advanced = advanced
        self.stale_runner = stale_runner
        self.calls = 0
        self.stale_client = object()
        self.advanced_client = object()

    async def __call__(self, *, conv: Conversation, **_kwargs: object):
        self.calls += 1
        if self.calls == 1:
            return self.stale_client, dataclasses.replace(conv, runner_id=self.stale_runner)
        return self.advanced_client, dataclasses.replace(conv, runner_id=self.advanced)


@pytest.mark.parametrize(
    "initial_binding", ["initially-bound", "initially-unbound"], ids=["bound", "unbound"]
)
async def test_side_chat_recovery_preserves_concurrent_rebind(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, initial_binding: str
) -> None:
    """A concurrent recovery's newer binding survives a stale source snapshot.

    Regression for the lost update: recovery snapshots the side chat on B (or
    unbound) while a concurrent recovery moves the source and the side chat to
    C. The compare-and-swap rebind for a bound side chat, and the absent-only
    bind for a previously unbound one, must both leave the side chat on C
    instead of overwriting it back to B.
    """
    store = SqlAlchemyConversationStore(db_uri)
    source, side = _source_and_side_chat(store, db_uri)
    stale_side = dataclasses.replace(side, runner_id="runner-stale-b")
    advanced = "runner-live-c"
    if initial_binding == "initially-bound":
        # The concurrent recovery finished before this one snapshotted anything.
        stale_runner = "runner-stale-b"
        staged = _AdvancingSource(advanced=advanced, stale_runner=stale_runner)
        store.replace_runner_id(source.id, advanced)
        store.replace_runner_id(side.id, advanced)
        expected_ensure_calls = 2  # revalidation catches the advance and retries
    else:
        # Unbound side chat: the concurrent recovery lands between source
        # validation and the bind, so only the absent-only write keeps C from
        # being overwritten by stale B.
        stale_runner = "runner-replacement"
        staged = _AdvancingSource(advanced=advanced, stale_runner=stale_runner)
        store.clear_runner_id(side.id)
        stale_side = dataclasses.replace(side, runner_id=None)
        real_set = store.set_runner_id

        def _racing_set(conversation_id: str, runner_id: str) -> bool:
            current_source = store.get_conversation(source.id)
            assert current_source is not None
            if conversation_id == side.id and current_source.runner_id != advanced:
                store.replace_runner_id(source.id, advanced)
                store.replace_runner_id(side.id, advanced)
            return real_set(conversation_id, runner_id)

        monkeypatch.setattr(store, "set_runner_id", _racing_set)
        expected_ensure_calls = 1  # validation already passed; the race is at the write

    monkeypatch.setattr(orchestration, "ensure_runner_connected", staged)
    monkeypatch.setattr(
        sessions, "_get_runner_client", AsyncMock(return_value=staged.advanced_client)
    )

    client, recovered = await _recover(store, stale_side)

    assert client is staged.advanced_client
    assert staged.calls == expected_ensure_calls
    assert recovered.runner_id == advanced
    saved = store.get_conversation(side.id)
    assert saved is not None and saved.runner_id == advanced


async def test_side_chat_recovery_follows_source_only_advancement(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that moved while the side chat lagged behind is followed, not trusted.

    The source advanced to C after recovery snapshotted it on B, and the side
    chat still holds B. Recovery must notice the fresh source binding and move
    the side chat to C rather than rebinding it to the superseded B.
    """
    store = SqlAlchemyConversationStore(db_uri)
    source, side = _source_and_side_chat(store, db_uri)
    stale_side = dataclasses.replace(side, runner_id="runner-stale-b")
    advanced = "runner-live-c"
    store.replace_runner_id(source.id, advanced)
    store.replace_runner_id(side.id, "runner-stale-b")
    staged = _AdvancingSource(advanced=advanced, stale_runner="runner-stale-b")

    monkeypatch.setattr(orchestration, "ensure_runner_connected", staged)
    monkeypatch.setattr(
        sessions, "_get_runner_client", AsyncMock(return_value=staged.advanced_client)
    )

    client, recovered = await _recover(store, stale_side)

    assert client is staged.advanced_client
    assert staged.calls == 2
    assert recovered.runner_id == advanced
    saved = store.get_conversation(side.id)
    assert saved is not None and saved.runner_id == advanced


@pytest.mark.parametrize("native", [False, True], ids=["generic", "codex-native"])
async def test_side_chat_recovery_gates_on_source_edit_access_at_the_routes(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    native: bool,
) -> None:
    """Route-level recovery requires edit access to the source, not just the side chat.

    Protects ``user_id``/``permission_store`` propagation through the message
    and Resume entry points: a caller who can edit the side chat but not its
    source gets the plain unavailable error with no recovery attempt, while
    the source's owner reaches recovery. An editor may use an existing live
    native binding without owning its runner.
    """
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    agent = await create_test_agent(client, name="side-chat-source-edit-gate")
    source = store.create_conversation(agent_id=agent["id"])
    store.set_runner_id(source.id, "runner-shared")
    if native:
        side = store.create_conversation(
            kind="sub_agent", agent_id=agent["id"], parent_conversation_id=source.id
        )
        store.set_labels(
            side.id,
            {
                "omnigent.wrapper": "codex-native-ui-subagent",
                "omnigent.codex_native.agent_nickname": "Side chat",
                "omnigent.codex_native.subagent_thread_id": "thread-side",
            },
        )
    else:
        side = store.fork_conversation(
            source.id,
            extra_labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: source.id},
        )
    store.set_runner_id(side.id, "runner-shared")
    for user in (_OWNER, _OTHER):
        permissions.ensure_user(user)
    permissions.grant(_OWNER, source.id, LEVEL_OWNER)
    permissions.grant(_OWNER, side.id, LEVEL_OWNER)
    permissions.grant(_OTHER, side.id, LEVEL_EDIT)

    async def _ensure(*, session_id: str, conv: Conversation, **_kwargs: object):
        return None, conv

    ensure = AsyncMock(side_effect=_ensure)
    # The helper resolves its import through orchestration; the Resume path
    # holds its own binding in routes_events. Patch both to spy on every call.
    monkeypatch.setattr(orchestration, "ensure_runner_connected", ensure)
    monkeypatch.setattr(routes_events, "ensure_runner_connected", ensure)
    monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=None))

    message = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
    }

    denied = await auth_client.post(
        f"/v1/sessions/{side.id}/events",
        json=message,
        headers={"X-Forwarded-Email": _OTHER},
    )
    assert denied.status_code == (404 if native else 503), denied.text
    ensure.assert_not_awaited()

    denied_resume = await auth_client.post(
        f"/v1/sessions/{side.id}/events",
        json={"type": "retry_session", "data": {}},
        headers={"X-Forwarded-Email": _OTHER},
    )
    assert denied_resume.status_code == (404 if native else 503), denied_resume.text
    # Resume resolves the side chat's own binding first; the source is never
    # touched when access is denied.
    assert [c.kwargs["session_id"] for c in ensure.await_args_list] == (
        [] if native else [side.id]
    )

    allowed = await auth_client.post(
        f"/v1/sessions/{side.id}/events",
        json=message,
        headers={"X-Forwarded-Email": _OWNER},
    )
    assert allowed.status_code == 503, allowed.text
    assert [c.kwargs["session_id"] for c in ensure.await_args_list] == (
        [source.id] if native else [side.id, source.id]
    )
    if native:
        permissions.grant(_OTHER, source.id, LEVEL_READ)
        store.set_host_id(source.id, _HOST_ID, workspace="/workspace")
        runner = AsyncMock()
        runner.post.return_value = httpx.Response(202)
        ensure.reset_mock()
        ensure.side_effect = None
        ensure.return_value = (runner, store.get_conversation(source.id))
        monkeypatch.setattr(
            auth_app.state.runner_router, "runner_owner", Mock(return_value=_OWNER)
        )
        monkeypatch.setattr(
            auth_app.state.runner_router, "host_is_on_another_replica", Mock(return_value=False)
        )
        monkeypatch.setattr(
            auth_app.state.runner_router, "runner_is_online", Mock(return_value=True)
        )
        monkeypatch.setattr(
            auth_app.state.runner_router,
            "runner_has_capability",
            Mock(
                side_effect=lambda _runner_id, capability: capability == CAP_CODEX_SIDE_CHAT_RESUME
            ),
        )
        monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=runner))
        monkeypatch.setattr(routes_events, "_ensure_runner_relay_ready", AsyncMock())

        read_only = await auth_client.post(
            f"/v1/sessions/{side.id}/events",
            json=message,
            headers={"X-Forwarded-Email": _OTHER},
        )
        assert read_only.status_code == 403, read_only.text
        ensure.assert_not_awaited()
        runner.post.assert_not_awaited()

        permissions.grant(_OTHER, source.id, LEVEL_EDIT)
        edited = await auth_client.post(
            f"/v1/sessions/{side.id}/events",
            json=message,
            headers={"X-Forwarded-Email": _OTHER},
        )

        assert edited.status_code == 202, edited.text
        ensure.assert_not_awaited()
        runner.post.assert_awaited_once_with(
            f"/v1/sessions/{source.id}/events",
            json={
                "type": "message",
                "content": message["data"]["content"],
                "codex_side_thread_id": "thread-side",
            },
        )
        monkeypatch.setattr(routes_events, "_recover_retry_session", AsyncMock())
        runner.post.return_value = httpx.Response(200, json={"resumed": True})
        for event in (message, {"type": "retry_session", "data": {}}):
            store.set_labels(side.id, {"omnigent.closed": "true"})
            runner.post.reset_mock()

            reopened = await auth_client.post(
                f"/v1/sessions/{side.id}/events",
                json=event,
                headers={"X-Forwarded-Email": _OTHER},
            )

            assert reopened.status_code == 202, reopened.text
            ensure.assert_not_awaited()
            saved = store.get_conversation(side.id)
            assert saved.runner_id == "runner-shared"
            assert "omnigent.closed" not in saved.labels
            forwarded = [call.kwargs["json"] for call in runner.post.await_args_list]
            assert forwarded[0] == {
                "type": "codex_side_chat_resume",
                "codex_side_session_id": side.id,
            }
            assert len(forwarded) == (2 if event["type"] == "message" else 1)
            if event["type"] == "message":
                assert forwarded[1]["codex_side_thread_id"] == "thread-side"


@pytest.mark.parametrize(
    ("code", "status"),
    [(ErrorCode.WRONG_REPLICA, 400), (ErrorCode.FORBIDDEN, 403)],
    ids=["wrong-replica", "foreign-runner"],
)
async def test_side_chat_recovery_errors_reach_both_routes(
    auth_client: httpx.AsyncClient,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    status: int,
) -> None:
    """Message and Resume return recovery's redirect and ownership errors unchanged.

    Neither route may consume the message or replace the error with the generic
    unavailable response.
    """
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    agent = await create_test_agent(client, name=f"side-chat-recovery-error-{code}")
    source = store.create_conversation(agent_id=agent["id"])
    store.set_runner_id(source.id, "runner-shared")
    side = store.fork_conversation(
        source.id,
        extra_labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: source.id},
    )
    store.set_runner_id(side.id, "runner-shared")
    permissions.ensure_user(_OWNER)
    permissions.grant(_OWNER, source.id, LEVEL_OWNER)
    permissions.grant(_OWNER, side.id, LEVEL_OWNER)

    async def _ensure(*, conv: Conversation, **_kwargs: object):
        return None, conv

    monkeypatch.setattr(routes_events, "ensure_runner_connected", AsyncMock(side_effect=_ensure))
    monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=None))
    recover = AsyncMock(side_effect=OmnigentError("recovery refused", code=code))
    monkeypatch.setattr(routes_events, "_recover_side_chat_runner_via_source", recover)

    message = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
    }
    for event in (message, {"type": "retry_session", "data": {}}):
        response = await auth_client.post(
            f"/v1/sessions/{side.id}/events",
            json=event,
            headers={"X-Forwarded-Email": _OWNER},
        )
        assert response.status_code == status, response.text
        assert response.json()["error"]["code"] == code, response.text

    assert recover.await_count == 2
    assert store.list_items(side.id).data == []


async def test_resume_single_flight_is_shared_only_within_one_caller(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent Resume calls on a side chat share an attempt only within one caller.

    Both callers may edit the source, but only one owns its runner. The other
    editor starts recovery first and is held open; the runner owner arriving
    meanwhile gets its own attempt instead of the editor's 503, and the owner's
    second request shares that attempt.
    """
    store = SqlAlchemyConversationStore(db_uri)
    source, side = _source_and_side_chat(store, db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in (_OWNER, _OTHER):
        permissions.ensure_user(user)
        permissions.grant(user, side.id, LEVEL_OWNER)
    permissions.grant(_OWNER, source.id, LEVEL_OWNER)
    permissions.grant(_OTHER, source.id, LEVEL_EDIT)

    started = {_OWNER: asyncio.Event(), _OTHER: asyncio.Event()}
    release = {_OWNER: asyncio.Event(), _OTHER: asyncio.Event()}
    recoveries: list[str | None] = []

    async def _recover(*, user_id: str, **_kwargs: Any) -> dict[str, bool | str]:
        recoveries.append(user_id)
        started[user_id].set()
        await release[user_id].wait()
        if user_id == _OTHER:
            raise OmnigentError(
                "No runner is available to recover this session.",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        return {"queued": False, "recovered": True, "recovery": "runner_relaunched"}

    monkeypatch.setattr(routes_events, "_recover_retry_session", _recover)

    def _resume(user_id: str) -> asyncio.Task[dict[str, bool | str]]:
        return asyncio.create_task(
            routes_events._retry_session_single_flight(
                request=None,
                session_id=side.id,
                conversation=side,
                conversation_store=store,
                runner_router=None,
                user_id=user_id,
                permission_store=permissions,
            )
        )

    async def _within_budget(event: asyncio.Event, what: str) -> None:
        try:
            await asyncio.wait_for(event.wait(), timeout=budget(5.0))
        except TimeoutError:
            pytest.fail(f"{what} never started its own recovery attempt")

    tasks: list[asyncio.Task[dict[str, bool | str]]] = []
    try:
        tasks.append(editor := _resume(_OTHER))
        await _within_budget(started[_OTHER], "the non-owner editor")
        tasks.append(owner := _resume(_OWNER))
        await _within_budget(started[_OWNER], "the runner owner")
        tasks.append(joiner := _resume(_OWNER))
        # The joiner has no awaits before joining, so a few loop turns let it join.
        for _ in range(5):
            await asyncio.sleep(0)
        release[_OWNER].set()
        assert (await owner)["recovery"] == "runner_relaunched"
        assert (await joiner)["recovery"] == "runner_relaunched"
        release[_OTHER].set()
        with pytest.raises(OmnigentError) as raised:
            await editor
        assert raised.value.code == ErrorCode.RUNNER_UNAVAILABLE
    finally:
        for event in release.values():
            event.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert recoveries == [_OTHER, _OWNER]


@pytest.mark.parametrize(
    ("caller", "recovers"), [(_OWNER, True), (_OTHER, False)], ids=["runner-owner", "other-editor"]
)
async def test_side_chat_recovery_requires_ownership_of_source_runner(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, caller: str, recovers: bool
) -> None:
    """Recovery binds a side chat only to a runner its caller owns, like an explicit attach.

    Both callers may edit the source, but its live runner belongs to one of
    them. The other editor gets an explained ``FORBIDDEN`` and their side chat
    stays unbound.
    """
    store = SqlAlchemyConversationStore(db_uri)
    source, side = _source_and_side_chat(store, db_uri)
    store.clear_runner_id(side.id)
    unbound = store.get_conversation(side.id)
    assert unbound is not None and unbound.runner_id is None
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in (_OWNER, _OTHER):
        permissions.ensure_user(user)
    permissions.grant(_OWNER, source.id, LEVEL_OWNER)
    permissions.grant(_OTHER, source.id, LEVEL_EDIT)
    permissions.grant(caller, side.id, LEVEL_OWNER)
    router = Mock()
    router.host_is_on_another_replica.return_value = False
    router.runner_owner.return_value = _OWNER
    runner_client = object()
    monkeypatch.setattr(
        orchestration, "ensure_runner_connected", AsyncMock(return_value=(runner_client, source))
    )
    get_client = AsyncMock(return_value=runner_client)
    monkeypatch.setattr(sessions, "_get_runner_client", get_client)

    if recovers:
        client, _ = await _recover(
            store, unbound, user_id=caller, permission_store=permissions, runner_router=router
        )
        assert client is runner_client
    else:
        with pytest.raises(OmnigentError) as raised:
            await _recover(
                store, unbound, user_id=caller, permission_store=permissions, runner_router=router
            )
        assert raised.value.code == ErrorCode.FORBIDDEN
        assert "another user's runner" in str(raised.value)
        get_client.assert_not_awaited()

    router.runner_owner.assert_called_with("runner-replacement")
    saved = store.get_conversation(side.id)
    assert saved is not None
    assert saved.runner_id == ("runner-replacement" if recovers else None)


async def test_side_chat_recovery_redirects_when_source_host_is_on_another_replica(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    _, side = _source_and_side_chat(store, db_uri)
    router = Mock()
    router.host_is_on_another_replica.return_value = True
    ensure = AsyncMock()
    monkeypatch.setattr(orchestration, "ensure_runner_connected", ensure)

    with pytest.raises(OmnigentError) as raised:
        await _recover(store, side, runner_router=router)

    assert raised.value.code == ErrorCode.WRONG_REPLICA
    router.host_is_on_another_replica.assert_called_once_with(_HOST_ID)
    ensure.assert_not_awaited()
    saved = store.get_conversation(side.id)
    assert saved is not None and saved.runner_id == "runner-exited"


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "source-1"),
        ({"host_id": _HOST_ID}, None),
        ({"labels": {SIDE_CHAT_LABEL_KEY: "1"}}, None),
        ({"labels": {SIDE_CHAT_SOURCE_LABEL_KEY: "source-1"}}, None),
        ({"kind": "sub_agent"}, None),
        ({"labels": {SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: "side-1"}}, None),
    ],
    ids=[
        "hostless-side-chat",
        "host-bound",
        "missing-source-label",
        "not-side-chat",
        "sub-agent",
        "self-source",
    ],
)
def test_side_chat_source_id_accepts_only_hostless_side_chats(
    changes: dict[str, Any], expected: str | None
) -> None:
    side_chat = Conversation(
        id="side-1",
        created_at=0,
        updated_at=0,
        root_conversation_id="side-1",
        labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: "source-1"},
    )

    assert (
        orchestration._side_chat_source_id(dataclasses.replace(side_chat, **changes)) == expected
    )
