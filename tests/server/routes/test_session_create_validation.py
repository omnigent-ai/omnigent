"""Tests for the shared session-create validation helper.

Focused on :func:`validate_session_agent`'s owning-session authorization for
session-scoped agents. The interactive ``POST /v1/sessions`` route and the
scheduled-task create/fire paths all funnel through here, so a single-user
server (which persists the local owner as ``None``) must authorize a
session-scoped agent the same way the interactive route does with the ``local``
sentinel — rather than tripping the ``require_access`` unauthenticated guard.
"""

from __future__ import annotations

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_READ, RESERVED_USER_LOCAL
from omnigent.server.routes._session_create_validation import validate_session_agent
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)


@pytest.fixture()
def agent_store(db_uri: str) -> SqlAlchemyAgentStore:
    return SqlAlchemyAgentStore(db_uri)


@pytest.fixture()
def conv_store(db_uri: str) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(db_uri)


@pytest.fixture()
def perm_store(db_uri: str) -> SqlAlchemyPermissionStore:
    return SqlAlchemyPermissionStore(db_uri)


def _mint_session_agent(
    conv_store: SqlAlchemyConversationStore,
    *,
    agent_id: str = "a0000000000000000000000000000010",
) -> str:
    """Mint a session-scoped agent and return its id."""
    created = conv_store.create_session_with_agent(
        agent_id=agent_id,
        agent_name="claude-native-ui (fork ag_x)",
        agent_bundle_location="ag_x/bundle",
        agent_description=None,
        title="session-scoped claude",
    )
    return created.agent.id


@pytest.mark.asyncio
async def test_single_user_none_authorizes_session_scoped_agent(
    agent_store: SqlAlchemyAgentStore,
    conv_store: SqlAlchemyConversationStore,
    perm_store: SqlAlchemyPermissionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-user ``None`` owner reaches a ``local``-owned session's agent.

    The scheduled-task path persists the local owner as ``None``; grants are
    keyed by the ``local`` sentinel. On a single-user server that ``None`` must
    resolve to ``local`` so the owning-session READ check passes, instead of the
    old 401.
    """
    monkeypatch.setattr(
        "omnigent.server.routes._session_create_validation.local_single_user_enabled",
        lambda: True,
    )
    agent_id = _mint_session_agent(conv_store)
    # The mint's spawn-tree root is owned by the local sentinel, exactly as a
    # single-user server records it.
    agent = agent_store.get(agent_id)
    assert agent is not None and agent.session_id is not None
    perm_store.ensure_user(RESERVED_USER_LOCAL)
    perm_store.grant(RESERVED_USER_LOCAL, agent.session_id, 4)

    result = await validate_session_agent(
        user_id=None,
        agent_id=agent_id,
        agent_store=agent_store,
        permission_store=perm_store,
        conversation_store=conv_store,
    )
    assert result.id == agent_id


@pytest.mark.asyncio
async def test_multi_user_none_still_unauthorized_for_session_scoped_agent(
    agent_store: SqlAlchemyAgentStore,
    conv_store: SqlAlchemyConversationStore,
    perm_store: SqlAlchemyPermissionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-user server keeps 401ing an unauthenticated session-scoped bind.

    Off the single-user path, ``None`` means "no identity" and the fail-closed
    guard must still reject — the local fallback must not leak into multi-user.
    """
    monkeypatch.setattr(
        "omnigent.server.routes._session_create_validation.local_single_user_enabled",
        lambda: False,
    )
    agent_id = _mint_session_agent(conv_store)

    with pytest.raises(OmnigentError) as excinfo:
        await validate_session_agent(
            user_id=None,
            agent_id=agent_id,
            agent_store=agent_store,
            permission_store=perm_store,
            conversation_store=conv_store,
        )
    assert excinfo.value.code == ErrorCode.UNAUTHORIZED


@pytest.mark.asyncio
async def test_template_agent_skips_owning_session_check(
    agent_store: SqlAlchemyAgentStore,
    conv_store: SqlAlchemyConversationStore,
    perm_store: SqlAlchemyPermissionStore,
) -> None:
    """A template agent (no owning session) needs no access check.

    This is the ``builtin``/template path a correctly-seeded harness resolves
    to; it authorizes regardless of ``user_id``.
    """
    created = agent_store.create(
        "a0000000000000000000000000000011",
        "claude-native-ui",
        "ag_tmpl/bundle",
    )
    assert created.session_id is None

    result = await validate_session_agent(
        user_id=None,
        agent_id=created.id,
        agent_store=agent_store,
        permission_store=perm_store,
        conversation_store=conv_store,
    )
    assert result.id == created.id


def _owners_session_and_fork(conv_store: SqlAlchemyConversationStore) -> tuple[str, str, str]:
    """Alice's session and its fork, sharing one agent row; returns (agent, S, F)."""
    created = conv_store.create_session_with_agent(
        agent_id="a0000000000000000000000000000020",
        agent_name="orion",
        agent_bundle_location="a0000000000000000000000000000020/bundle",
        agent_description=None,
        title="alice's session",
        created_by="alice@example.com",
    )
    fork = conv_store.fork_conversation(created.conversation.id, agent_id=created.agent.id)
    return created.agent.id, created.conversation.id, fork.id


@pytest.mark.asyncio
async def test_read_on_any_session_sharing_an_agent_authorizes_it(
    agent_store: SqlAlchemyAgentStore,
    conv_store: SqlAlchemyConversationStore,
    perm_store: SqlAlchemyPermissionStore,
) -> None:
    """Bob can read only the fork; the reverse lookup may pick the other root."""
    agent_id, source_id, fork_id = _owners_session_and_fork(conv_store)
    perm_store.ensure_user("bob@example.com")
    perm_store.grant("bob@example.com", fork_id, LEVEL_READ)
    # Worst case for Bob: the lookup returns the session he cannot read.
    agent_store._session_id_for_agent = lambda _agent_id: source_id  # type: ignore[method-assign]

    result = await validate_session_agent(
        user_id="bob@example.com",
        agent_id=agent_id,
        agent_store=agent_store,
        permission_store=perm_store,
        conversation_store=conv_store,
    )
    assert result.id == agent_id

    perm_store.ensure_user("carol@example.com")
    with pytest.raises(OmnigentError) as excinfo:
        await validate_session_agent(
            user_id="carol@example.com",
            agent_id=agent_id,
            agent_store=agent_store,
            permission_store=perm_store,
            conversation_store=conv_store,
        )
    assert excinfo.value.code in (ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND)


@pytest.mark.asyncio
async def test_an_agents_owner_can_always_use_it(
    agent_store: SqlAlchemyAgentStore,
    conv_store: SqlAlchemyConversationStore,
    perm_store: SqlAlchemyPermissionStore,
) -> None:
    """No session grant needed: the owner check runs before the session lookup."""
    agent_id, _source_id, _fork_id = _owners_session_and_fork(conv_store)
    perm_store.ensure_user("alice@example.com")

    result = await validate_session_agent(
        user_id="alice@example.com",
        agent_id=agent_id,
        agent_store=agent_store,
        permission_store=perm_store,
        conversation_store=conv_store,
    )
    assert result.id == agent_id


def test_session_roots_for_an_agent_are_distinct_and_bounded(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    agent_id, source_id, fork_id = _owners_session_and_fork(conv_store)
    conv_store.create_conversation(
        agent_id=agent_id, parent_conversation_id=source_id, kind="sub_agent", title="child"
    )

    assert sorted(conv_store.list_session_roots_for_agent(agent_id, 50)) == sorted(
        [source_id, fork_id]
    )
    assert len(conv_store.list_session_roots_for_agent(agent_id, 1)) == 1


def test_many_children_of_one_session_never_crowd_out_another_root(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """A source session's children all share its root, so they must not use up the
    limit and hide the fork a reader may have been shared. More rows than one page."""
    agent_id, source_id, fork_id = _owners_session_and_fork(conv_store)
    for i in range(250):
        conv_store.create_conversation(
            agent_id=agent_id, parent_conversation_id=source_id, kind="sub_agent", title=f"c{i}"
        )

    for limit in (2, 50):
        assert sorted(conv_store.list_session_roots_for_agent(agent_id, limit)) == sorted(
            [source_id, fork_id]
        )


@pytest.mark.asyncio
async def test_sandbox_preview_authorizes_a_shared_agent_like_binding(
    agent_store: SqlAlchemyAgentStore,
    conv_store: SqlAlchemyConversationStore,
    perm_store: SqlAlchemyPermissionStore,
) -> None:
    """The model-options preview accepts READ on any session sharing the agent,
    exactly as session binding does, instead of 404ing on the lookup's root."""
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.routes.sandbox_inference import create_sandbox_inference_router

    agent_id, source_id, fork_id = _owners_session_and_fork(conv_store)
    perm_store.ensure_user("bob@example.com")
    perm_store.grant("bob@example.com", fork_id, LEVEL_READ)
    agent_store._session_id_for_agent = lambda _agent_id: source_id  # type: ignore[method-assign]
    spec = SimpleNamespace(
        executor=SimpleNamespace(config={"harness": "claude-sdk"}, type="omnigent", auth=None)
    )
    cache = SimpleNamespace(load=lambda *_args, **_kwargs: SimpleNamespace(spec=spec))
    catalog = {"configured": True, "models": ["m"]}

    class _Service:
        async def prepare(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {"catalog": catalog}

    app = FastAPI()
    app.state.inference_catalog = _Service()

    @app.exception_handler(OmnigentError)
    async def _handle(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.message})

    app.include_router(
        create_sandbox_inference_router(
            agent_store=agent_store,
            agent_cache=cache,
            conversation_store=conv_store,
            permission_store=perm_store,
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        ),
        prefix="/v1",
    )
    url = f"/v1/sandbox-providers/p/harnesses/claude-sdk/model-options?agent_id={agent_id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        bob = await client.get(url, headers={"X-Forwarded-Email": "bob@example.com"})
        carol = await client.get(url, headers={"X-Forwarded-Email": "carol@example.com"})
        unknown = await client.get(
            url.replace(agent_id, "f" * 32), headers={"X-Forwarded-Email": "bob@example.com"}
        )

    assert bob.status_code == 200, bob.text
    assert bob.json() == catalog
    assert carol.status_code == 404, carol.text
    # Clients read the missing-agent reason from ``detail``.
    assert unknown.status_code == 404, unknown.text
    assert unknown.json() == {"detail": "Agent not found"}
