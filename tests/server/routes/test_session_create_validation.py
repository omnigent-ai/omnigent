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
from omnigent.server.auth import RESERVED_USER_LOCAL
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
