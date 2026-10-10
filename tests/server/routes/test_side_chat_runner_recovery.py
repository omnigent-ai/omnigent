"""Side chats recover their shared runner through their source session."""

import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ
from omnigent.server.routes import sessions
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes.sessions import routes_events
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY, SIDE_CHAT_SOURCE_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
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
        # The side chat is unbound when recovery starts; the concurrent
        # recovery lands between this recovery's source validation and its
        # bind, so only the absent-only write stands between C and a stale B.
        # The staged snapshot matches the stored source so validation passes
        # before the race.
        stale_runner = "runner-replacement"
        staged = _AdvancingSource(advanced=advanced, stale_runner=stale_runner)
        store.clear_runner_id(side.id)
        stale_side = dataclasses.replace(side, runner_id=None)
        real_set = store.set_runner_id

        def _racing_set(conversation_id: str, runner_id: str) -> bool:
            if (
                conversation_id == side.id
                and store.get_conversation(source.id).runner_id != advanced
            ):
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


async def test_side_chat_recovery_gates_on_source_edit_access_at_the_routes(
    auth_client: httpx.AsyncClient,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route-level recovery requires edit access to the source, not just the side chat.

    Protects ``user_id``/``permission_store`` propagation through the message
    and Resume entry points: a caller who can edit the side chat but not its
    source gets the plain unavailable error with no recovery attempt, while
    the source's owner reaches recovery through the same routes.
    """
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    agent = await create_test_agent(client, name="side-chat-source-edit-gate")
    source = store.create_conversation(agent_id=agent["id"])
    store.set_runner_id(source.id, "runner-shared")
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
    assert denied.status_code == 503, denied.text
    ensure.assert_not_awaited()

    denied_resume = await auth_client.post(
        f"/v1/sessions/{side.id}/events",
        json={"type": "retry_session", "data": {}},
        headers={"X-Forwarded-Email": _OTHER},
    )
    assert denied_resume.status_code == 503, denied_resume.text
    # Resume resolves the side chat's own binding first; the source is never
    # touched when access is denied.
    assert [c.kwargs["session_id"] for c in ensure.await_args_list] == [side.id]

    allowed = await auth_client.post(
        f"/v1/sessions/{side.id}/events",
        json=message,
        headers={"X-Forwarded-Email": _OWNER},
    )
    assert allowed.status_code == 503, allowed.text
    assert [c.kwargs["session_id"] for c in ensure.await_args_list] == [side.id, source.id]


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
    "shape", ["host_bound", "missing_source_label", "not_side_chat", "sub_agent"]
)
async def test_only_hostless_side_chats_recover_through_source(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    source, side = _source_and_side_chat(store, db_uri)
    labels = dict(side.labels)
    changes: dict[str, object] = {}
    if shape == "host_bound":
        changes["host_id"] = _HOST_ID
    elif shape == "missing_source_label":
        labels.pop(SIDE_CHAT_SOURCE_LABEL_KEY)
    elif shape == "not_side_chat":
        labels.pop(SIDE_CHAT_LABEL_KEY)
    else:
        changes["kind"] = "sub_agent"
    candidate = dataclasses.replace(side, **changes, labels=labels)
    ensure = AsyncMock(return_value=(object(), source))
    monkeypatch.setattr(orchestration, "ensure_runner_connected", ensure)

    client, unchanged = await _recover(store, candidate)

    assert client is None and unchanged is candidate
    ensure.assert_not_awaited()
