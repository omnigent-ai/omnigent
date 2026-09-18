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

import re
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
    host_id: str | None = None,
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
    conv = conversation_store.create_conversation(
        agent_id=agent_id, workspace=workspace, host_id=host_id
    )
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


async def test_offline_absolute_change_validates_against_the_host_boundary(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An offline absolute change runs the shared workspace validator.

    With no runner to resolve against, the session's host must validate the
    path exactly like session create does (existence, canonicalization, the
    agent's ``os_env.cwd`` boundary) — and the canonical path it returns is
    what persists. Persisting the raw client string unvalidated would make
    it the runner root (and sandbox base) on automatic relaunch.
    """
    import uuid

    from omnigent.server.routes._sessions import helpers as sessions_helpers

    host_id = uuid.uuid4().hex
    app, session_id, store = _seed_session(
        db_uri,
        tmp_path,
        grants={_OWNER: LEVEL_OWNER},
        workspace="/home/user/project",
        host_id=host_id,
    )

    seen: dict[str, object] = {}

    async def _fake_validate(**kwargs: object) -> str:
        seen.update(kwargs)
        return "/home/user/project/subdir-canonical"

    monkeypatch.setattr(sessions_helpers, "_validate_session_workspace", _fake_validate)
    async with _client(app, _OWNER) as c:
        resp = await c.patch(
            f"/v1/sessions/{session_id}",
            json={"workspace": "/home/user/project/subdir"},
        )
        assert resp.status_code == 200, resp.text

    assert seen.get("host_id") == host_id
    assert seen.get("workspace") == "/home/user/project/subdir"
    conv = store.get_conversation(session_id)
    assert conv is not None
    # The HOST's canonical answer persists, not the raw client string.
    assert conv.workspace == "/home/user/project/subdir-canonical"


async def test_offline_absolute_change_rejected_by_the_validator_persists_nothing(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boundary-refused offline change is surfaced and leaves no trace."""
    import uuid

    from omnigent.errors import ErrorCode, OmnigentError
    from omnigent.server.routes._sessions import helpers as sessions_helpers

    app, session_id, store = _seed_session(
        db_uri,
        tmp_path,
        grants={_OWNER: LEVEL_OWNER},
        workspace="/home/user/project",
        host_id=uuid.uuid4().hex,
    )

    async def _refuse(**kwargs: object) -> str:
        del kwargs
        raise OmnigentError(
            "workspace is outside the boundary required by this agent",
            code=ErrorCode.INVALID_INPUT,
        )

    monkeypatch.setattr(sessions_helpers, "_validate_session_workspace", _refuse)
    async with _client(app, _OWNER) as c:
        resp = await c.patch(
            f"/v1/sessions/{session_id}",
            json={"workspace": "/outside/boundary"},
        )
        assert resp.status_code == 400, resp.text

    conv = store.get_conversation(session_id)
    assert conv is not None
    assert conv.workspace == "/home/user/project"


async def test_offline_absolute_change_without_a_host_is_refused(
    db_uri: str, tmp_path: Path
) -> None:
    """With no runner and no host there is nothing to validate against.

    Runner availability must not decide whether the agent's workspace
    boundary is enforced, so an absolute change with no validatable host is
    refused (503) rather than persisted blind.
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
        assert resp.status_code == 503, resp.text

    conv = store.get_conversation(session_id)
    assert conv is not None
    assert conv.workspace == "/home/user/project"


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


async def test_absolute_workspace_change_requires_owner(db_uri: str, tmp_path: Path) -> None:
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


async def test_relative_workspace_change_requires_edit(db_uri: str, tmp_path: Path) -> None:
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


async def test_runner_reach_refusal_surfaces_as_forbidden(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner's out-of-reach verdict is a permission refusal (403).

    Flattening it to 400 would tell the client its request was malformed
    when the real fact is that the target sits outside the session's
    reach — the same distinction the browse endpoints preserve.
    """
    from omnigent.server.routes import sessions as sessions_facade
    from omnigent.server.routes._sessions.helpers import _RunnerForwardResult

    app, session_id, store = _seed_session(
        db_uri,
        tmp_path,
        grants={_OWNER: LEVEL_OWNER},
        workspace="/home/user/project",
    )

    async def _forward(*args: object, **kwargs: object) -> _RunnerForwardResult:
        del args, kwargs
        return _RunnerForwardResult(
            status_code=403,
            body='{"error": "forbidden", "detail": "outside this session\'s reach"}',
        )

    monkeypatch.setattr(sessions_facade, "_forward_session_change_to_runner", _forward)
    async with _client(app, _OWNER) as c:
        resp = await c.patch(
            f"/v1/sessions/{session_id}",
            json={"workspace": "/outside/reach"},
        )
        assert resp.status_code == 403, resp.text

    conv = store.get_conversation(session_id)
    assert conv is not None
    assert conv.workspace == "/home/user/project"


async def test_failed_persist_rolls_the_runner_back(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persist failure after the runner applied the change must roll it back.

    The runner moves its live cwd when it answers the forward; if the
    server then fails to persist, live and stored state diverge — later
    turns cd somewhere the snapshot does not record. The server must
    point the runner back at the previously persisted workspace before
    surfacing the error.
    """
    from omnigent.server.routes import sessions as sessions_facade
    from omnigent.server.routes._sessions.helpers import _RunnerForwardResult
    from omnigent.stores.conversation_store import ConversationNotFoundError

    app, session_id, _store = _seed_session(
        db_uri,
        tmp_path,
        grants={_OWNER: LEVEL_OWNER},
        workspace="/home/user/project",
    )

    forwarded: list[dict[str, object]] = []

    async def _forward(
        _session_id: str, _router: object, event: dict[str, object], **kwargs: object
    ) -> _RunnerForwardResult:
        del kwargs
        forwarded.append(event)
        return _RunnerForwardResult(
            status_code=200,
            body='{"object": "session.workspace_changed",'
            ' "workspace": "/home/user/project/subdir"}',
        )

    def _persist_fails(
        self: SqlAlchemyConversationStore, conversation_id: str, workspace: str
    ) -> None:
        del self, conversation_id, workspace
        raise ConversationNotFoundError("simulated persist failure")

    monkeypatch.setattr(sessions_facade, "_forward_session_change_to_runner", _forward)
    monkeypatch.setattr(SqlAlchemyConversationStore, "set_workspace", _persist_fails)
    async with _client(app, _OWNER) as c:
        resp = await c.patch(
            f"/v1/sessions/{session_id}",
            json={"workspace": "subdir"},
        )
        assert resp.status_code == 404, resp.text

    assert [e.get("workspace") for e in forwarded] == ["subdir", "/home/user/project"]


def _py313_strict_ntpath_isabs(path: str) -> bool:
    """``ntpath.isabs`` as Python 3.13 implements it: drive-rooted or UNC only.

    3.13 stopped reporting rooted-but-driveless forms (``"/etc"``, ``"\\etc"``)
    as absolute, so a gate relying on ``ntpath.isabs`` alone fails open there.
    """
    return bool(re.match(r"^(\\\\|//|[A-Za-z]:[\\/])", path))


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/etc", True),
        ("/home/user/project", True),
        ("\\\\server\\share", True),
        ("C:\\Users\\alice", True),
        ("C:/Users/alice", True),
        ("\\etc", True),
        ("src", False),
        ("src/app", False),
        ("", False),
    ],
    ids=[
        "posix_abs",
        "posix_abs_deep",
        "unc",
        "drive_backslash",
        "drive_slash",
        "rooted_backslash",
        "relative",
        "relative_subpath",
        "empty",
    ],
)
def test_is_absolute_workspace_fails_closed_on_py313_ntpath(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    expected: bool,
) -> None:
    """The owner gate's absolute classification holds under 3.13 semantics.

    An absolute workspace is owner-gated; classifying one as relative would
    admit it at the edit tier. Simulate Python 3.13's stricter
    ``ntpath.isabs`` and assert the classification still fails closed for
    every absolute form on every supported interpreter.
    """
    import ntpath

    from omnigent.server.routes.sessions.routes_core import _is_absolute_workspace

    monkeypatch.setattr(ntpath, "isabs", _py313_strict_ntpath_isabs)
    assert _is_absolute_workspace(path) is expected


async def test_absolute_workspace_stays_owner_gated_under_py313_ntpath(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An editor's absolute PATCH is still 403 when ``ntpath.isabs`` is strict.

    Route-level twin of the classification test: under 3.13 semantics an
    absolute POSIX path must not slip through the edit-tier gate.
    """
    import ntpath

    monkeypatch.setattr(ntpath, "isabs", _py313_strict_ntpath_isabs)
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


async def test_concurrent_changes_persist_the_last_applied_workspace(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two racing changes leave the store agreeing with the runner.

    The forward and the persist are two steps. Without per-session ordering,
    change A could forward first, stall before persisting, and then overwrite
    change B's already-persisted value — the runner living in B while the
    stored workspace says A, permanently. The persisted workspace must equal
    the last workspace the runner applied.
    """
    import asyncio as _asyncio
    import contextlib
    import json as _json
    import time as _time

    from omnigent.server.routes import sessions as sessions_facade
    from omnigent.server.routes._sessions.helpers import _RunnerForwardResult

    app, session_id, store = _seed_session(
        db_uri,
        tmp_path,
        grants={_OWNER: LEVEL_OWNER},
        workspace="/home/user/project",
    )

    applied: list[str] = []
    a_forwarded = _asyncio.Event()

    async def _forward(
        _sid: object, _router: object, payload: dict[str, str]
    ) -> _RunnerForwardResult:
        workspace = payload["workspace"]
        # Deterministic interleave: B forwards only after A has forwarded, so
        # without ordering B's forward+persist land inside A's forward→persist
        # window and A's stale persist overwrites B's. Bounded: when the pair
        # is serialized and B goes first, A cannot forward until B finishes,
        # so B stops waiting instead of deadlocking against the ordering.
        if workspace == "/ws/a":
            a_forwarded.set()
        else:
            with contextlib.suppress(TimeoutError):
                await _asyncio.wait_for(a_forwarded.wait(), timeout=1.0)
        applied.append(workspace)
        return _RunnerForwardResult(
            status_code=200,
            body=_json.dumps({"object": "session.workspace_changed", "workspace": workspace}),
        )

    real_set_workspace = SqlAlchemyConversationStore.set_workspace

    def _slow_set_workspace(
        self: SqlAlchemyConversationStore, conversation_id: str, workspace: str
    ) -> None:
        # Stall change A between its forward and its persist, inviting change
        # B to forward AND persist in that window (runs on a worker thread via
        # asyncio.to_thread, so only this persist is delayed).
        if workspace == "/ws/a":
            _time.sleep(0.2)
        real_set_workspace(self, conversation_id, workspace)

    monkeypatch.setattr(sessions_facade, "_forward_session_change_to_runner", _forward)
    monkeypatch.setattr(SqlAlchemyConversationStore, "set_workspace", _slow_set_workspace)

    async with _client(app, _OWNER) as c:
        resp_a, resp_b = await _asyncio.gather(
            c.patch(f"/v1/sessions/{session_id}", json={"workspace": "/ws/a"}),
            c.patch(f"/v1/sessions/{session_id}", json={"workspace": "/ws/b"}),
        )
        assert resp_a.status_code == 200, resp_a.text
        assert resp_b.status_code == 200, resp_b.text

    assert len(applied) == 2, applied
    conv = store.get_conversation(session_id)
    assert conv is not None
    assert conv.workspace == applied[-1], (
        f"Persisted workspace {conv.workspace!r} diverged from the runner's "
        f"last applied workspace {applied[-1]!r} — forward+persist interleaved."
    )
