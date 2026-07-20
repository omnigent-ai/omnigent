"""Tests for the per-server session-sharing mode gate.

Covers the whole feature surface:

- :meth:`SharingMode.coerce` — the fail-open-to-ON contract for the
  env-var and callable boundaries.
- ``create_app(sharing_mode=…)`` wiring — static value, per-request
  callable, and the ``OMNIGENT_SHARING_MODE`` env-var default.
- ``GET /v1/info`` reporting ``sharing_mode`` so the web app stays in
  lockstep with the server gate.
- The ``PUT /v1/sessions/{id}/permissions`` gate: ``OFF`` rejects all
  new grants (403), ``READ_ONLY`` caps grants at read (edit → 403,
  read → ok), and ``ON`` is behavior-preserving. Revoke stays allowed
  in every mode.

The app is built via the real :func:`create_app` so the tests exercise
the actual ``app.state.sharing_mode`` normalization and the route gate,
not a hand-rolled stub. Requests go through ``httpx.ASGITransport`` (no
lifespan) since none of these paths need the runtime.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import sharing_settings
from omnigent.server.app import create_app
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_MANAGE,
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_PUBLIC,
    AuthProvider,
    SharingMode,
    UnifiedAuthProvider,
    workspace_sharing_blocked,
)
from omnigent.server.sharing_settings import (
    read_public_sharing_override,
    read_sharing_mode_override,
    resolve_sharing_mode_path,
    write_public_sharing_override,
    write_sharing_mode_override,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

# Reserved test identities. The owner is granted MANAGE so it can reach
# the grant endpoint; the grantee is the target of each new grant; the admin
# manages the server-wide sharing mode.
_OWNER = "owner@sharing.test"
_GRANTEE = "bob@sharing.test"
_ADMIN = "admin@sharing.test"


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``resolve_data_dir()`` at the per-test tmp dir so the file-backed
    sharing overrides are isolated, and reset the module cache so no value
    leaks across tests."""
    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", str(tmp_path / "admin-credentials"))
    sharing_settings._cache = {}


def _build_app(
    db_uri: str,
    tmp_path: Path,
    *,
    sharing_mode: SharingMode | object | None = None,
    public_sharing: bool | object | None = None,
    permission_store: SqlAlchemyPermissionStore | None = None,
    auth_provider: AuthProvider | None = None,
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
        sharing_mode=sharing_mode,
        public_sharing=public_sharing,
    )


def _client(app: FastAPI, email: str | None = None) -> httpx.AsyncClient:
    """An in-process async client, optionally carrying a header identity."""
    headers = {"X-Forwarded-Email": email} if email else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    )


def _seed_owned_session(
    db_uri: str,
    tmp_path: Path,
    *,
    sharing_mode: SharingMode = SharingMode.ON,
    public_sharing: bool | object | None = None,
    workspace: str | None = None,
) -> tuple[FastAPI, str]:
    """Build an app whose ``_OWNER`` identity manages a real session.

    Seeds a conversation and an OWNER grant directly into the shared DB
    so ``PUT …/permissions`` gets past the manage-access check and hits
    the sharing gate (and, when allowed, actually persists the grant).
    ``workspace`` sets the session's recorded cwd, exercising the
    ``RESTRICTED_READ_ONLY`` home/root block; ``public_sharing`` exercises
    the public-access gate.
    """
    permission_store = SqlAlchemyPermissionStore(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)
    conv = conversation_store.create_conversation(workspace=workspace)
    permission_store.ensure_user(_OWNER)
    permission_store.grant(_OWNER, conv.id, LEVEL_OWNER)
    app = _build_app(
        db_uri,
        tmp_path,
        sharing_mode=sharing_mode,
        public_sharing=public_sharing,
        permission_store=permission_store,
        auth_provider=UnifiedAuthProvider(source="header"),
    )
    return app, conv.id


def _admin_app(
    db_uri: str,
    tmp_path: Path,
    *,
    sharing_mode: SharingMode | object | None = None,
    public_sharing: bool | object | None = None,
) -> FastAPI:
    """Build an app with a seeded admin identity for the sharing routes.

    A ``None`` setting yields the editable file-backed default; a static value
    yields the non-editable (managed) case.
    """
    permission_store = SqlAlchemyPermissionStore(db_uri)
    permission_store.ensure_user(_ADMIN, is_admin=True)
    return _build_app(
        db_uri,
        tmp_path,
        sharing_mode=sharing_mode,
        public_sharing=public_sharing,
        permission_store=permission_store,
        auth_provider=UnifiedAuthProvider(source="header"),
    )


# ── SharingMode.coerce — fail-open-to-ON contract ────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (SharingMode.OFF, SharingMode.OFF),
        (SharingMode.READ_ONLY, SharingMode.READ_ONLY),
        (SharingMode.RESTRICTED_READ_ONLY, SharingMode.RESTRICTED_READ_ONLY),
        (SharingMode.ON, SharingMode.ON),
        ("off", SharingMode.OFF),
        ("read_only", SharingMode.READ_ONLY),
        ("restricted_read_only", SharingMode.RESTRICTED_READ_ONLY),
        ("on", SharingMode.ON),
        ("READ_ONLY", SharingMode.READ_ONLY),  # case-insensitive
        ("  Restricted_Read_Only  ", SharingMode.RESTRICTED_READ_ONLY),
        (" On ", SharingMode.ON),  # whitespace-tolerant
        (None, SharingMode.ON),  # unset → fail open
        ("", SharingMode.ON),  # empty → fail open
        ("garbage", SharingMode.ON),  # unrecognized → fail open
        (123, SharingMode.ON),  # wrong type → fail open
    ],
)
def test_coerce_fails_open_to_on(value: object, expected: SharingMode) -> None:
    """Anything unset/unrecognized coerces to ON; valid values round-trip."""
    assert SharingMode.coerce(value) is expected


# ── create_app wiring: env default / static / callable ───────────────


def test_wiring_defaults_to_on_when_env_unset(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No arg + unset env → the top-level default is ON."""
    monkeypatch.delenv("OMNIGENT_SHARING_MODE", raising=False)
    app = _build_app(db_uri, tmp_path)
    assert app.state.sharing_mode() is SharingMode.ON


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("off", SharingMode.OFF),
        ("read_only", SharingMode.READ_ONLY),
        ("on", SharingMode.ON),
        ("nonsense", SharingMode.ON),  # fail open
    ],
)
def test_wiring_reads_env_var(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: SharingMode,
) -> None:
    """``OMNIGENT_SHARING_MODE`` is the top-level control when no arg is given."""
    monkeypatch.setenv("OMNIGENT_SHARING_MODE", raw)
    app = _build_app(db_uri, tmp_path)
    assert app.state.sharing_mode() is expected


def test_wiring_static_value_overrides_env(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``sharing_mode=`` beats the env var."""
    monkeypatch.setenv("OMNIGENT_SHARING_MODE", "off")
    app = _build_app(db_uri, tmp_path, sharing_mode=SharingMode.READ_ONLY)
    assert app.state.sharing_mode() is SharingMode.READ_ONLY


def test_wiring_callable_is_resolved_per_request(db_uri: str, tmp_path: Path) -> None:
    """A callable is invoked (and coerced) on each resolution, not cached."""
    modes = iter(["on", "off", "garbage"])
    app = _build_app(db_uri, tmp_path, sharing_mode=lambda: next(modes))
    assert app.state.sharing_mode() is SharingMode.ON
    assert app.state.sharing_mode() is SharingMode.OFF
    # The callable boundary also fails open for a bad value.
    assert app.state.sharing_mode() is SharingMode.ON


# ── GET /v1/info reports the mode ────────────────────────────────────


async def test_info_reports_default_on(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_SHARING_MODE", raising=False)
    app = _build_app(db_uri, tmp_path)
    async with _client(app) as c:
        resp = await c.get("/v1/info")
        assert resp.status_code == 200
        assert resp.json()["sharing_mode"] == "on"


@pytest.mark.parametrize(
    "mode,expected",
    [
        (SharingMode.OFF, "off"),
        (SharingMode.READ_ONLY, "read_only"),
        (SharingMode.RESTRICTED_READ_ONLY, "restricted_read_only"),
        (SharingMode.ON, "on"),
    ],
)
async def test_info_reports_configured_mode(
    db_uri: str, tmp_path: Path, mode: SharingMode, expected: str
) -> None:
    app = _build_app(db_uri, tmp_path, sharing_mode=mode)
    async with _client(app) as c:
        resp = await c.get("/v1/info")
        assert resp.json()["sharing_mode"] == expected


# ── The grant gate — no permission store needed (gate precedes it) ───


async def test_off_rejects_new_grant_at_any_level(db_uri: str, tmp_path: Path) -> None:
    """OFF blocks a new grant regardless of level, before the store check."""
    app = _build_app(db_uri, tmp_path, sharing_mode=SharingMode.OFF)
    async with _client(app) as c:
        for level in (LEVEL_READ, LEVEL_EDIT, LEVEL_MANAGE):
            resp = await c.put(
                "/v1/sessions/conv_absent/permissions",
                json={"user_id": _GRANTEE, "level": level},
            )
            assert resp.status_code == 403, resp.text
            assert "disabled" in resp.text.lower()


async def test_read_only_rejects_edit_grant(db_uri: str, tmp_path: Path) -> None:
    """READ_ONLY rejects an edit (level > read) grant with 403."""
    app = _build_app(db_uri, tmp_path, sharing_mode=SharingMode.READ_ONLY)
    async with _client(app) as c:
        resp = await c.put(
            "/v1/sessions/conv_absent/permissions",
            json={"user_id": _GRANTEE, "level": LEVEL_EDIT},
        )
        assert resp.status_code == 403, resp.text
        assert "read-only" in resp.text.lower()


# ── The grant gate — allowed paths persist against a real store ──────


async def test_on_allows_edit_grant(db_uri: str, tmp_path: Path) -> None:
    """ON is behavior-preserving: an edit grant succeeds (200)."""
    app, sid = _seed_owned_session(db_uri, tmp_path, sharing_mode=SharingMode.ON)
    async with _client(app, _OWNER) as c:
        resp = await c.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": _GRANTEE, "level": LEVEL_EDIT},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["level"] == LEVEL_EDIT


async def test_read_only_allows_read_but_not_edit(db_uri: str, tmp_path: Path) -> None:
    """READ_ONLY lets a read grant through but still rejects edit."""
    app, sid = _seed_owned_session(db_uri, tmp_path, sharing_mode=SharingMode.READ_ONLY)
    async with _client(app, _OWNER) as c:
        ok = await c.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": _GRANTEE, "level": LEVEL_READ},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["level"] == LEVEL_READ

        denied = await c.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": _GRANTEE, "level": LEVEL_EDIT},
        )
        assert denied.status_code == 403, denied.text


async def test_off_rejects_grant_even_with_manage_access(db_uri: str, tmp_path: Path) -> None:
    """Even a legitimate manager cannot create a grant when sharing is OFF."""
    app, sid = _seed_owned_session(db_uri, tmp_path, sharing_mode=SharingMode.OFF)
    async with _client(app, _OWNER) as c:
        resp = await c.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": _GRANTEE, "level": LEVEL_READ},
        )
        assert resp.status_code == 403, resp.text


async def test_revoke_is_unaffected_by_read_only(db_uri: str, tmp_path: Path) -> None:
    """Revoke stays allowed in READ_ONLY — only *new* grants are gated."""
    app, sid = _seed_owned_session(db_uri, tmp_path, sharing_mode=SharingMode.READ_ONLY)
    async with _client(app, _OWNER) as c:
        await c.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": _GRANTEE, "level": LEVEL_READ},
        )
        revoke = await c.delete(f"/v1/sessions/{sid}/permissions/{_GRANTEE}")
        assert revoke.status_code == 204, revoke.text


# ── workspace_sharing_blocked — the home/root cwd predicate ──────────


@pytest.mark.parametrize(
    "workspace",
    [
        "/",
        "/root",
        "/root/",  # trailing slash normalized
        "/home/alice",
        "/Users/bob",
        "/var/home/carol",  # ostree home layout
    ],
)
def test_workspace_sharing_blocked_true(workspace: str) -> None:
    """The filesystem root and user home dirs are blocked, host-agnostically."""
    assert workspace_sharing_blocked(workspace) is True


@pytest.mark.parametrize(
    "workspace",
    [
        None,  # no recorded cwd
        "",
        "/home/alice/project",  # a subdirectory of home is fine
        "/Users/bob/code",
        "/var/home/carol/repo",
        "/home",  # the parent container itself is not a home dir
        "/var/home",
        "/workspaces/omnigent",  # a project checkout, not a home
        "/srv/work",
        "/tmp/session",
    ],
)
def test_workspace_sharing_blocked_false(workspace: str | None) -> None:
    """A subdirectory / project / arbitrary path (or no cwd) is shareable."""
    assert workspace_sharing_blocked(workspace) is False


# ── RESTRICTED_READ_ONLY gate — home/root cwd blocked entirely ───────


@pytest.mark.parametrize("blocked_workspace", ["/", "/home/alice", "/root"])
async def test_restricted_blocks_home_or_root_session_even_read(
    db_uri: str, tmp_path: Path, blocked_workspace: str
) -> None:
    """RESTRICTED_READ_ONLY rejects *all* grants (even read) on a session
    whose cwd is a home dir or the filesystem root."""
    app, sid = _seed_owned_session(
        db_uri,
        tmp_path,
        sharing_mode=SharingMode.RESTRICTED_READ_ONLY,
        workspace=blocked_workspace,
    )
    async with _client(app, _OWNER) as c:
        resp = await c.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": _GRANTEE, "level": LEVEL_READ},
        )
        assert resp.status_code == 403, resp.text
        assert "cannot be shared" in resp.text.lower()


async def test_restricted_allows_read_on_normal_session(db_uri: str, tmp_path: Path) -> None:
    """RESTRICTED_READ_ONLY behaves like READ_ONLY for a non-home/root cwd:
    a read grant is allowed, an edit grant is rejected."""
    app, sid = _seed_owned_session(
        db_uri,
        tmp_path,
        sharing_mode=SharingMode.RESTRICTED_READ_ONLY,
        workspace="/home/alice/project",
    )
    async with _client(app, _OWNER) as c:
        ok = await c.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": _GRANTEE, "level": LEVEL_READ},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["level"] == LEVEL_READ

        denied = await c.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": _GRANTEE, "level": LEVEL_EDIT},
        )
        assert denied.status_code == 403, denied.text
        assert "read-only" in denied.text.lower()


async def test_restricted_allows_read_when_no_workspace(db_uri: str, tmp_path: Path) -> None:
    """A session with no recorded cwd is not treated as home/root — a read
    grant is allowed under RESTRICTED_READ_ONLY."""
    app, sid = _seed_owned_session(
        db_uri, tmp_path, sharing_mode=SharingMode.RESTRICTED_READ_ONLY, workspace=None
    )
    async with _client(app, _OWNER) as c:
        resp = await c.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": _GRANTEE, "level": LEVEL_READ},
        )
        assert resp.status_code == 200, resp.text


# ── File-backed override: persistence + create_app precedence ────────


def test_override_file_roundtrip(tmp_path: Path) -> None:
    """write/read round-trips the override; an unset file reads as None."""
    assert read_sharing_mode_override() is None
    write_sharing_mode_override(SharingMode.RESTRICTED_READ_ONLY)
    assert resolve_sharing_mode_path().exists()
    assert read_sharing_mode_override() is SharingMode.RESTRICTED_READ_ONLY


def test_override_beats_env_default(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When an override file exists, create_app's default resolver returns it,
    ignoring the env default; the path is marked editable."""
    monkeypatch.setenv("OMNIGENT_SHARING_MODE", "on")
    write_sharing_mode_override(SharingMode.OFF)
    app = _build_app(db_uri, tmp_path)  # None → file-backed default
    assert app.state.sharing_mode() is SharingMode.OFF
    assert app.state.sharing_mode_writable is True


def test_env_default_used_when_no_override(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no override file, the env default applies."""
    monkeypatch.setenv("OMNIGENT_SHARING_MODE", "read_only")
    app = _build_app(db_uri, tmp_path)
    assert app.state.sharing_mode() is SharingMode.READ_ONLY


# ── Admin route: GET / PUT /v1/sharing ───────────────────────────────


async def test_get_reports_state(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_SHARING_MODE", "on")
    app = _admin_app(db_uri, tmp_path)
    async with _client(app, _ADMIN) as c:
        resp = await c.get("/v1/sharing")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["sharing_mode"] == "on"
        assert body["editable"] is True
        assert body["options"] == ["on", "read_only", "restricted_read_only", "off"]


async def test_put_sets_mode_and_persists(db_uri: str, tmp_path: Path) -> None:
    """An admin PUT persists the override; GET, /v1/info, and the live
    resolver all reflect it."""
    app = _admin_app(db_uri, tmp_path)
    async with _client(app, _ADMIN) as c:
        put = await c.put("/v1/sharing", json={"sharing_mode": "restricted_read_only"})
        assert put.status_code == 200, put.text
        assert put.json()["sharing_mode"] == "restricted_read_only"

        assert (await c.get("/v1/sharing")).json()["sharing_mode"] == "restricted_read_only"
        assert (await c.get("/v1/info")).json()["sharing_mode"] == "restricted_read_only"
        assert app.state.sharing_mode() is SharingMode.RESTRICTED_READ_ONLY
        assert read_sharing_mode_override() is SharingMode.RESTRICTED_READ_ONLY


async def test_put_rejects_unknown_value(db_uri: str, tmp_path: Path) -> None:
    """A typo'd tier is a 400 — no silent fail-open coercion on an admin PUT."""
    app = _admin_app(db_uri, tmp_path)
    async with _client(app, _ADMIN) as c:
        resp = await c.put("/v1/sharing", json={"sharing_mode": "bogus"})
        assert resp.status_code == 400, resp.text
        # unchanged: nothing was persisted
        assert read_sharing_mode_override() is None


async def test_admin_endpoint_requires_admin(db_uri: str, tmp_path: Path) -> None:
    """A non-admin identity is forbidden from reading or writing the mode."""
    app = _admin_app(db_uri, tmp_path)  # only _ADMIN is an admin
    async with _client(app, "intruder@sharing.test") as c:
        assert (await c.get("/v1/sharing")).status_code == 403
        put = await c.put("/v1/sharing", json={"sharing_mode": "off"})
        assert put.status_code == 403, put.text


async def test_put_rejected_when_not_writable(db_uri: str, tmp_path: Path) -> None:
    """A deployment-managed mode (static/callable) reports editable=false and
    rejects writes."""
    app = _admin_app(db_uri, tmp_path, sharing_mode=SharingMode.ON)
    async with _client(app, _ADMIN) as c:
        assert (await c.get("/v1/sharing")).json()["editable"] is False
        put = await c.put("/v1/sharing", json={"sharing_mode": "off"})
        assert put.status_code == 403, put.text


async def test_put_requires_a_field(db_uri: str, tmp_path: Path) -> None:
    """An empty body updates nothing and is a 400."""
    app = _admin_app(db_uri, tmp_path)
    async with _client(app, _ADMIN) as c:
        resp = await c.put("/v1/sharing", json={})
        assert resp.status_code == 400, resp.text


# ── Public-access switch (OMNIGENT_PUBLIC_SHARING) ───────────────────


def test_public_sharing_defaults_enabled(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No arg + unset env → public sharing is enabled, file-editable."""
    monkeypatch.delenv("OMNIGENT_PUBLIC_SHARING", raising=False)
    app = _build_app(db_uri, tmp_path)
    assert app.state.public_sharing() is True
    assert app.state.public_sharing_writable is True


@pytest.mark.parametrize(
    "raw,expected", [("0", False), ("false", False), ("no", False), ("1", True), ("on", True)]
)
def test_public_sharing_reads_env_var(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    """``OMNIGENT_PUBLIC_SHARING`` is the top-level default when no arg is given."""
    monkeypatch.setenv("OMNIGENT_PUBLIC_SHARING", raw)
    app = _build_app(db_uri, tmp_path)
    assert app.state.public_sharing() is expected


def test_public_static_value_is_not_writable(db_uri: str, tmp_path: Path) -> None:
    """An explicit bool is authoritative and not admin-editable."""
    app = _build_app(db_uri, tmp_path, public_sharing=False)
    assert app.state.public_sharing() is False
    assert app.state.public_sharing_writable is False


def test_public_override_roundtrip_and_precedence(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The override file round-trips and beats the env default."""
    assert read_public_sharing_override() is None
    monkeypatch.setenv("OMNIGENT_PUBLIC_SHARING", "1")
    write_public_sharing_override(False)
    assert read_public_sharing_override() is False
    app = _build_app(db_uri, tmp_path)  # None → file-backed default
    assert app.state.public_sharing() is False


async def test_info_reports_public_sharing(db_uri: str, tmp_path: Path) -> None:
    app_on = _build_app(db_uri, tmp_path, public_sharing=True)
    async with _client(app_on) as c:
        assert (await c.get("/v1/info")).json()["public_sharing_enabled"] is True
    app_off = _build_app(db_uri, tmp_path, public_sharing=False)
    async with _client(app_off) as c:
        assert (await c.get("/v1/info")).json()["public_sharing_enabled"] is False


async def test_public_grant_blocked_when_disabled(db_uri: str, tmp_path: Path) -> None:
    """When public sharing is off, the ``__public__`` grant is 403 — but a
    normal user grant still succeeds (the two switches are independent)."""
    app, sid = _seed_owned_session(db_uri, tmp_path, public_sharing=False)
    async with _client(app, _OWNER) as c:
        public = await c.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": RESERVED_USER_PUBLIC, "level": LEVEL_READ},
        )
        assert public.status_code == 403, public.text
        assert "public access has been disabled" in public.text.lower()

        user = await c.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": _GRANTEE, "level": LEVEL_READ},
        )
        assert user.status_code == 200, user.text


async def test_public_grant_allowed_when_enabled(db_uri: str, tmp_path: Path) -> None:
    """The default (public enabled) still lets a ``__public__`` read grant through."""
    app, sid = _seed_owned_session(db_uri, tmp_path, public_sharing=True)
    async with _client(app, _OWNER) as c:
        resp = await c.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": RESERVED_USER_PUBLIC, "level": LEVEL_READ},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["level"] == LEVEL_READ


async def test_admin_get_reports_public_state(db_uri: str, tmp_path: Path) -> None:
    app = _admin_app(db_uri, tmp_path)
    async with _client(app, _ADMIN) as c:
        body = (await c.get("/v1/sharing")).json()
        assert body["public_sharing_enabled"] is True
        assert body["public_sharing_editable"] is True


async def test_admin_put_disables_public_and_gate_follows(db_uri: str, tmp_path: Path) -> None:
    """An admin PUT of public_sharing=false persists, is reflected in /v1/info,
    and makes the grant gate reject the ``__public__`` grant."""
    permission_store = SqlAlchemyPermissionStore(db_uri)
    permission_store.ensure_user(_ADMIN, is_admin=True)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation()
    permission_store.grant(_ADMIN, conv.id, LEVEL_OWNER)
    app = _build_app(
        db_uri,
        tmp_path,
        permission_store=permission_store,
        auth_provider=UnifiedAuthProvider(source="header"),
    )
    async with _client(app, _ADMIN) as c:
        put = await c.put("/v1/sharing", json={"public_sharing": False})
        assert put.status_code == 200, put.text
        assert put.json()["public_sharing_enabled"] is False

        assert (await c.get("/v1/info")).json()["public_sharing_enabled"] is False
        assert read_public_sharing_override() is False

        blocked = await c.put(
            f"/v1/sessions/{conv.id}/permissions",
            json={"user_id": RESERVED_USER_PUBLIC, "level": LEVEL_READ},
        )
        assert blocked.status_code == 403, blocked.text


async def test_admin_put_public_rejected_when_not_writable(db_uri: str, tmp_path: Path) -> None:
    """A deployment-managed public setting reports not-editable and rejects writes."""
    app = _admin_app(db_uri, tmp_path, public_sharing=True)
    async with _client(app, _ADMIN) as c:
        assert (await c.get("/v1/sharing")).json()["public_sharing_editable"] is False
        put = await c.put("/v1/sharing", json={"public_sharing": False})
        assert put.status_code == 403, put.text


async def test_admin_put_is_atomic_across_mixed_writability(db_uri: str, tmp_path: Path) -> None:
    """A both-fields PUT where only one setting is file-backed rejects the whole
    request without persisting the writable half (no partial apply).

    Mode is file-backed (editable); public access is deployment-managed (a
    callable, not editable). Setting both must 403 on public *before* the mode
    override is written.
    """
    app = _admin_app(db_uri, tmp_path, public_sharing=lambda: True)
    async with _client(app, _ADMIN) as c:
        resp = await c.put("/v1/sharing", json={"sharing_mode": "off", "public_sharing": False})
        assert resp.status_code == 403, resp.text
        # The writable half must NOT have been persisted (no partial apply).
        assert read_sharing_mode_override() is None
