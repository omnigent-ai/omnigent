"""Sub-agents relaunch their root's runner when no ancestor runner is live."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.entities import Conversation
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ
from omnigent.server.routes import sessions
from omnigent.server.routes.sessions import routes_events
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE

_HOST_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
_OWNER = "alice@example.com"
_OTHER = "bob@example.com"


def _root_and_sub_agent(
    store: SqlAlchemyConversationStore, db_uri: str
) -> tuple[Conversation, Conversation]:
    """Create a host-bound root and a sub-agent, both bound to the root's exited runner."""
    agent_id = generate_agent_id()
    SqlAlchemyAgentStore(db_uri).create(agent_id, name="test", bundle_location="test:///bundle")
    root = store.create_conversation(agent_id=agent_id, runner_id="runner-exited")
    store.set_host_id(root.id, _HOST_ID, workspace="/workspace")
    child = store.create_conversation(
        kind="sub_agent",
        agent_id=agent_id,
        parent_conversation_id=root.id,
        runner_id="runner-exited",
    )
    saved_root = store.get_conversation(root.id)
    saved_child = store.get_conversation(child.id)
    assert saved_root is not None and saved_child is not None
    return saved_root, saved_child


async def _reattach(
    store: SqlAlchemyConversationStore,
    child: Conversation,
    *,
    user_id: str | None = None,
    permission_store: SqlAlchemyPermissionStore | None = None,
) -> httpx.AsyncClient | None:
    return await routes_events._reattach_subagent_runner(
        child,
        app_state=SimpleNamespace(),
        conversation_store=store,
        runner_router=None,
        user_id=user_id,
        permission_store=permission_store,
    )


async def test_sub_agent_rebinds_to_relaunched_root_runner(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    root, child = _root_and_sub_agent(store, db_uri)
    runner_client = httpx.AsyncClient(base_url="http://runner")

    async def _relaunch(*, session_id: str, **_kwargs: object):
        store.replace_runner_id(session_id, "runner-replacement")
        return runner_client, store.get_conversation(session_id)

    async def _live_runner(session_id: str, *_args: object, **_kwargs: object):
        current = store.get_conversation(session_id)
        live = current is not None and current.runner_id == "runner-replacement"
        return runner_client if live else None

    ensure = AsyncMock(side_effect=_relaunch)
    monkeypatch.setattr(routes_events, "ensure_runner_connected", ensure)
    monkeypatch.setattr(sessions, "_get_runner_client", _live_runner)

    try:
        client = await _reattach(store, child)
    finally:
        await runner_client.aclose()

    assert client is runner_client
    assert ensure.await_args.kwargs["session_id"] == root.id
    saved = store.get_conversation(child.id)
    assert saved is not None and saved.runner_id == "runner-replacement"
    assert saved.host_id is None


@pytest.mark.parametrize("root_level", [None, LEVEL_READ])
async def test_root_runner_relaunch_requires_edit_access_to_root(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, root_level: int | None
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    root, child = _root_and_sub_agent(store, db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user in (_OWNER, _OTHER):
        permissions.ensure_user(user)
    permissions.grant(_OWNER, root.id, LEVEL_OWNER)
    if root_level is not None:
        permissions.grant(_OTHER, root.id, root_level)
    ensure = AsyncMock()
    monkeypatch.setattr(routes_events, "ensure_runner_connected", ensure)
    monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=None))

    client = await _reattach(store, child, user_id=_OTHER, permission_store=permissions)

    assert client is None
    ensure.assert_not_awaited()
    saved = store.get_conversation(child.id)
    assert saved is not None and saved.runner_id == "runner-exited"


async def test_sub_agent_binding_unchanged_when_root_runner_unavailable(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    root, child = _root_and_sub_agent(store, db_uri)
    ensure = AsyncMock(return_value=(None, root))
    monkeypatch.setattr(routes_events, "ensure_runner_connected", ensure)
    monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=None))

    client = await _reattach(store, child)

    assert client is None
    ensure.assert_awaited_once()
    saved = store.get_conversation(child.id)
    assert saved is not None and saved.runner_id == "runner-exited"


@pytest.mark.parametrize("case", ["side-chat", "closed-root"])
async def test_root_runner_is_not_relaunched_for_side_chats_or_closed_roots(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    root, child = _root_and_sub_agent(store, db_uri)
    if case == "side-chat":
        store.set_labels(
            child.id,
            {
                "omnigent.wrapper": "codex-native-ui-subagent",
                "omnigent.codex_native.agent_nickname": "Side chat",
                "omnigent.codex_native.subagent_thread_id": "thread-side",
            },
        )
    else:
        store.set_labels(root.id, {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE})
    labeled = store.get_conversation(child.id)
    assert labeled is not None
    ensure = AsyncMock()
    monkeypatch.setattr(routes_events, "ensure_runner_connected", ensure)
    monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=None))

    assert await _reattach(store, labeled) is None
    ensure.assert_not_awaited()
