"""Tests for the ``workspace`` field on ``PATCH /v1/sessions/{id}``.

Browsing to a folder in the Files rail repoints the session's workdir:
the web client PATCHes the session with a wire-form ``workspace`` and the
server gates the change, forwards it to the bound runner to resolve
against its live env root, and persists the resolved absolute path so the
runner cd's there on the next turn. These tests cover the server half:

- the permission gate (a relative change is edit-gated; an absolute
  change targets the owner's machine and is owner-gated), and
- the runner-offline fallback (an absolute path is already canonical and
  is persisted as-is; a relative path can only be resolved by the runner,
  so with no runner the change is refused rather than guessed).

The app is built via the real :func:`create_app` so the actual route gate
and persistence run, not a stub. No runner is bound, so the forward
resolves to ``None`` (offline) and the offline branch is exercised
directly. Requests go through ``httpx.ASGITransport`` (no lifespan).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_OWNER,
    LEVEL_READ,
    AuthProvider,
    UnifiedAuthProvider,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_OWNER = "owner@workdir.test"
_EDITOR = "editor@workdir.test"
_VIEWER = "viewer@workdir.test"


def _build_app(
    db_uri: str,
    tmp_path: Path,
    *,
    permission_store: SqlAlchemyPermissionStore,
    auth_provider: AuthProvider,
) -> FastAPI:
    """Build a real ``create_app`` wired to per-test SQLite stores."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=permission_store,
        auth_provider=auth_provider,
    )


def _client(app: FastAPI, email: str) -> httpx.AsyncClient:
    """An in-process async client carrying a header identity."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Forwarded-Email": email},
    )


def _seed_session(
    db_uri: str,
    tmp_path: Path,
    *,
    grants: dict[str, int],
    workspace: str | None = None,
) -> tuple[FastAPI, str, SqlAlchemyConversationStore]:
    """Seed a session with the given per-user grant levels.

    Returns the app, the session id, and the conversation store so a test
    can assert what actually persisted (independent of the response shape).
    """
    from omnigent.db.utils import generate_agent_id

    permission_store = SqlAlchemyPermissionStore(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)
    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="workdir-agent", bundle_location="test:///bundle")
    conv = conversation_store.create_conversation(agent_id=agent_id, workspace=workspace)
    for email, level in grants.items():
        permission_store.ensure_user(email)
        permission_store.grant(email, conv.id, level)
    app = _build_app(
        db_uri,
        tmp_path,
        permission_store=permission_store,
        auth_provider=UnifiedAuthProvider(source="header"),
    )
    return app, conv.id, conversation_store


async def test_owner_repoints_workspace_to_absolute_path(
    db_uri: str, tmp_path: Path
) -> None:
    """An owner PATCHing an absolute ``workspace`` persists it.

    With no runner bound the forward is offline, but an absolute wire-form
    path is already the canonical target on the owner's machine, so the
    server persists it directly. If this regresses, the double-click that
    re-roots the Files rail leaves ``Conversation.workspace`` untouched and
    the runner keeps cd'ing to the old directory.
    """
    app, session_id, store = _seed_session(
        db_uri,
        tmp_path,
        grants={_OWNER: LEVEL_OWNER},
        workspace="/home/user/project",
    )
    async with _client(app, _OWNER) as c:
        resp = await c.patch(
            f"/v1/sessions/{session_id}",
            json={"workspace": "/home/user/project/subdir"},
        )
        assert resp.status_code == 200, resp.text

    conv = store.get_conversation(session_id)
    assert conv is not None
    assert conv.workspace == "/home/user/project/subdir"


async def test_relative_workspace_change_requires_online_runner(
    db_uri: str, tmp_path: Path
) -> None:
    """A relative ``workspace`` needs the runner to resolve it.

    A relative wire path only means something against the runner's live
    env root, which the server does not know for a runner-only session.
    With no runner to resolve against, the server must refuse (503) rather
    than persist a half-resolved guess. The owner grant isolates this to
    the offline behavior, not the permission gate.
    """
    app, session_id, store = _seed_session(
        db_uri,
        tmp_path,
        grants={_OWNER: LEVEL_OWNER},
        workspace="/home/user/project",
    )
    async with _client(app, _OWNER) as c:
        resp = await c.patch(
            f"/v1/sessions/{session_id}",
            json={"workspace": "subdir"},
        )
        assert resp.status_code == 503, resp.text

    # Nothing persisted: the recorded workspace is unchanged.
    conv = store.get_conversation(session_id)
    assert conv is not None
    assert conv.workspace == "/home/user/project"


async def test_absolute_workspace_change_requires_owner(
    db_uri: str, tmp_path: Path
) -> None:
    """An absolute ``workspace`` targets the owner's machine, so it is
    owner-gated: an editor collaborator PATCHing an absolute path gets 403
    and nothing is persisted."""
    app, session_id, store = _seed_session(
        db_uri,
        tmp_path,
        grants={_OWNER: LEVEL_OWNER, _EDITOR: LEVEL_EDIT},
        workspace="/home/user/project",
    )
    async with _client(app, _EDITOR) as c:
        resp = await c.patch(
            f"/v1/sessions/{session_id}",
            json={"workspace": "/etc"},
        )
        assert resp.status_code == 403, resp.text

    conv = store.get_conversation(session_id)
    assert conv is not None
    assert conv.workspace == "/home/user/project"


async def test_relative_workspace_change_requires_edit(
    db_uri: str, tmp_path: Path
) -> None:
    """Repointing the workspace is an edit to the session, so a read-only
    collaborator PATCHing even a relative (in-workspace) path gets 403 and
    nothing is persisted."""
    app, session_id, store = _seed_session(
        db_uri,
        tmp_path,
        grants={_OWNER: LEVEL_OWNER, _VIEWER: LEVEL_READ},
        workspace="/home/user/project",
    )
    async with _client(app, _VIEWER) as c:
        resp = await c.patch(
            f"/v1/sessions/{session_id}",
            json={"workspace": "subdir"},
        )
        assert resp.status_code == 403, resp.text

    conv = store.get_conversation(session_id)
    assert conv is not None
    assert conv.workspace == "/home/user/project"
