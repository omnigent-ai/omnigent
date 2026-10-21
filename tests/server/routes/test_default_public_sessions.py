"""Tests for the server-wide default-public-sessions policy.

The admin picks which NEW sessions start with a ``__public__`` read grant:
``off`` (all private), ``sandbox`` (managed cloud-sandbox sessions only) or
``all``. Covered end to end through the real :func:`create_app`:

- :meth:`DefaultPublicSessions.coerce` fails closed to ``off``.
- Wiring: env default, file override, static/callable (not editable).
- ``GET`` / ``PUT /v1/sharing`` read and persist the setting.
- ``POST /v1/sessions`` (JSON + multipart bundle), fork, and a managed
  (``host_type="managed"``) create each apply the policy, and the default
  never grants past ``sharing_mode`` / ``public_sharing``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import sharing_settings
from omnigent.server.app import create_app
from omnigent.server.auth import (
    LEVEL_READ,
    RESERVED_USER_PUBLIC,
    SharingMode,
    UnifiedAuthProvider,
)
from omnigent.server.managed_hosts import parse_sandbox_config
from omnigent.server.routes._sessions.helpers import _grant_default_public
from omnigent.server.sharing_settings import (
    DefaultPublicSessions,
    new_session_starts_public,
    read_default_public_sessions_override,
    write_default_public_sessions_override,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import (
    FakeSandboxLauncher,
    build_agent_bundle,
    create_test_agent,
    install_fake_modal_launcher,
)

_ADMIN = "admin@public-default.test"
_USER = "alice@public-default.test"


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the file-backed overrides per test and clear the env default."""
    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", str(tmp_path / "admin-credentials"))
    monkeypatch.delenv("OMNIGENT_DEFAULT_PUBLIC_SESSIONS", raising=False)
    monkeypatch.delenv("OMNIGENT_SHARING_MODE", raising=False)
    monkeypatch.delenv("OMNIGENT_PUBLIC_SHARING", raising=False)
    sharing_settings._cache = {}


def _build_app(
    db_uri: str,
    tmp_path: Path,
    *,
    default_public_sessions: object = None,
    sharing_mode: SharingMode | None = None,
    public_sharing: bool | None = None,
    managed: bool = False,
) -> tuple[FastAPI, SqlAlchemyPermissionStore]:
    """Real multi-user ``create_app`` on SQLite with an admin and a user."""
    permission_store = SqlAlchemyPermissionStore(db_uri)
    permission_store.ensure_user(_ADMIN, is_admin=True)
    permission_store.ensure_user(_USER)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    extra: dict[str, Any] = {}
    if managed:
        extra["host_store"] = HostStore(db_uri)
        extra["sandbox_config"] = parse_sandbox_config(
            {
                "provider": "modal",
                "server_url": "https://managed-test.example.com",
                "modal": {"image": "docker.io/test/omnigent-host:latest"},
            }
        )
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=permission_store,
        auth_provider=UnifiedAuthProvider(source="header"),
        sharing_mode=sharing_mode,
        public_sharing=public_sharing,
        default_public_sessions=default_public_sessions,  # type: ignore[arg-type]
        **extra,
    )
    return app, permission_store


@pytest_asyncio.fixture()
async def client_factory() -> AsyncIterator[Any]:
    """Yield a factory of in-process clients carrying a header identity."""
    clients: list[httpx.AsyncClient] = []

    def _make(app: FastAPI, email: str) -> httpx.AsyncClient:
        c = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Forwarded-Email": email},
        )
        clients.append(c)
        return c

    yield _make
    for c in clients:
        await c.aclose()


def _is_public(permission_store: SqlAlchemyPermissionStore, session_id: str) -> bool:
    grant = permission_store.get(RESERVED_USER_PUBLIC, session_id)
    return grant is not None and grant.level == LEVEL_READ


# ── coerce + wiring ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("off", DefaultPublicSessions.OFF),
        ("sandbox", DefaultPublicSessions.SANDBOX),
        (" ALL ", DefaultPublicSessions.ALL),
        (DefaultPublicSessions.ALL, DefaultPublicSessions.ALL),
        (None, DefaultPublicSessions.OFF),
        ("", DefaultPublicSessions.OFF),
        ("everything", DefaultPublicSessions.OFF),  # typo fails closed (private)
        (1, DefaultPublicSessions.OFF),
    ],
)
def test_coerce_fails_closed_to_off(value: object, expected: DefaultPublicSessions) -> None:
    assert DefaultPublicSessions.coerce(value) is expected


def test_wiring_defaults_off_and_editable(db_uri: str, tmp_path: Path) -> None:
    app, _ = _build_app(db_uri, tmp_path)
    assert app.state.default_public_sessions() is DefaultPublicSessions.OFF
    assert app.state.default_public_sessions_writable is True


def test_wiring_env_then_file_override(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_DEFAULT_PUBLIC_SESSIONS", "sandbox")
    app, _ = _build_app(db_uri, tmp_path)
    assert app.state.default_public_sessions() is DefaultPublicSessions.SANDBOX
    write_default_public_sessions_override(DefaultPublicSessions.ALL)
    assert read_default_public_sessions_override() is DefaultPublicSessions.ALL
    assert app.state.default_public_sessions() is DefaultPublicSessions.ALL


def test_wiring_static_and_callable_not_editable(db_uri: str, tmp_path: Path) -> None:
    static, _ = _build_app(db_uri, tmp_path, default_public_sessions="all")
    assert static.state.default_public_sessions() is DefaultPublicSessions.ALL
    assert static.state.default_public_sessions_writable is False
    box = {"v": "off"}
    dynamic, _ = _build_app(db_uri, tmp_path, default_public_sessions=lambda: box["v"])
    assert dynamic.state.default_public_sessions() is DefaultPublicSessions.OFF
    box["v"] = "sandbox"
    assert dynamic.state.default_public_sessions() is DefaultPublicSessions.SANDBOX
    assert dynamic.state.default_public_sessions_writable is False


# ── new_session_starts_public: policy × gates ────────────────────────


class _State:
    def __init__(
        self,
        policy: str,
        mode: SharingMode = SharingMode.ON,
        public: bool = True,
    ) -> None:
        self.default_public_sessions = lambda: policy
        self.sharing_mode = lambda: mode
        self.public_sharing = lambda: public


@pytest.mark.parametrize(
    "policy,managed,expected",
    [
        ("off", False, False),
        ("off", True, False),
        ("sandbox", False, False),
        ("sandbox", True, True),
        ("all", False, True),
        ("all", True, True),
    ],
)
def test_policy_matrix(policy: str, managed: bool, expected: bool) -> None:
    assert new_session_starts_public(_State(policy), managed=managed, workspace=None) is expected


def test_default_respects_sharing_gates() -> None:
    assert not new_session_starts_public(
        _State("all", mode=SharingMode.OFF), managed=True, workspace=None
    )
    assert not new_session_starts_public(_State("all", public=False), managed=True, workspace=None)
    restricted = _State("all", mode=SharingMode.RESTRICTED_READ_ONLY)
    assert not new_session_starts_public(restricted, managed=False, workspace="/home/alice")
    assert new_session_starts_public(restricted, managed=False, workspace="/home/alice/proj")
    # Unknown cwd (forks bind later) fails closed, except for server sandboxes.
    assert not new_session_starts_public(restricted, managed=False, workspace=None)
    assert new_session_starts_public(restricted, managed=True, workspace=None)
    # A hand-built state without the attribute stays private.
    assert not new_session_starts_public(object(), managed=True, workspace=None)


# ── admin route ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_get_and_put_round_trip(
    db_uri: str, tmp_path: Path, client_factory: Any
) -> None:
    app, _ = _build_app(db_uri, tmp_path)
    admin = client_factory(app, _ADMIN)
    body = (await admin.get("/v1/sharing")).json()
    assert body["default_public_sessions"] == "off"
    assert body["default_public_sessions_editable"] is True
    assert body["default_public_sessions_options"] == ["off", "sandbox", "all"]

    put = await admin.put("/v1/sharing", json={"default_public_sessions": "sandbox"})
    assert put.status_code == 200, put.text
    assert put.json()["default_public_sessions"] == "sandbox"
    assert (sharing_settings.resolve_default_public_sessions_path()).read_text().strip() == (
        "sandbox"
    )
    assert (await admin.get("/v1/sharing")).json()["default_public_sessions"] == "sandbox"


@pytest.mark.asyncio
async def test_admin_put_rejects_unknown_and_non_admin(
    db_uri: str, tmp_path: Path, client_factory: Any
) -> None:
    app, _ = _build_app(db_uri, tmp_path)
    bad = await client_factory(app, _ADMIN).put(
        "/v1/sharing", json={"default_public_sessions": "everything"}
    )
    assert bad.status_code == 400
    denied = await client_factory(app, _USER).put(
        "/v1/sharing", json={"default_public_sessions": "all"}
    )
    assert denied.status_code == 403
    assert read_default_public_sessions_override() is None


@pytest.mark.asyncio
async def test_admin_put_rejected_when_deployment_managed_and_atomic(
    db_uri: str, tmp_path: Path, client_factory: Any
) -> None:
    app, _ = _build_app(db_uri, tmp_path, default_public_sessions="off")
    resp = await client_factory(app, _ADMIN).put(
        "/v1/sharing", json={"public_sharing": False, "default_public_sessions": "all"}
    )
    assert resp.status_code == 403
    # Neither setting was written (validated before any write).
    assert sharing_settings.read_public_sharing_override() is None


# ── session creation applies the policy ──────────────────────────────


async def _json_create(client: httpx.AsyncClient, agent_id: str, **extra: Any) -> str:
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy,expected_public",
    [("off", False), ("sandbox", False), ("all", True)],
)
async def test_local_create_follows_policy(
    db_uri: str,
    tmp_path: Path,
    client_factory: Any,
    policy: str,
    expected_public: bool,
) -> None:
    """Non-managed sessions (JSON create, bundle create, fork) are public only under ``all``."""
    write_default_public_sessions_override(DefaultPublicSessions(policy))
    app, perms = _build_app(db_uri, tmp_path)
    alice = client_factory(app, _USER)
    agent = await create_test_agent(alice, name="default-public-agent", user=_USER)
    bundle_session = agent["_session_id"]
    json_session = await _json_create(alice, agent["id"])
    fork = await alice.post(f"/v1/sessions/{json_session}/fork", json={})
    assert fork.status_code == 201, fork.text

    for sid in (bundle_session, json_session, fork.json()["id"]):
        assert _is_public(perms, sid) is expected_public, sid
    if expected_public:
        # Anyone signed in can now read it; the owner can still revoke it.
        bob = client_factory(app, "bob@public-default.test")
        assert (await bob.get(f"/v1/sessions/{json_session}")).status_code == 200
        revoke = await alice.delete(f"/v1/sessions/{json_session}/permissions/__public__")
        assert revoke.status_code == 204, revoke.text
        assert not _is_public(perms, json_session)


@pytest.mark.asyncio
async def test_side_chat_fork_stays_private(
    db_uri: str, tmp_path: Path, client_factory: Any
) -> None:
    write_default_public_sessions_override(DefaultPublicSessions.ALL)
    app, perms = _build_app(db_uri, tmp_path)
    alice = client_factory(app, _USER)
    agent = await create_test_agent(alice, name="side-chat-agent", user=_USER)
    source = await _json_create(alice, agent["id"])
    side = await alice.post(f"/v1/sessions/{source}/fork", json={"side_chat": True})
    assert side.status_code == 201, side.text
    assert _is_public(perms, source)
    assert not _is_public(perms, side.json()["id"])


@pytest.mark.asyncio
async def test_fork_stays_private_under_restricted_read_only(
    db_uri: str, tmp_path: Path, client_factory: Any
) -> None:
    """A fork has no workspace until it binds, and binding doesn't re-check the
    grant, so restricted mode must not make it public up front."""
    write_default_public_sessions_override(DefaultPublicSessions.ALL)
    app, perms = _build_app(db_uri, tmp_path, sharing_mode=SharingMode.RESTRICTED_READ_ONLY)
    alice = client_factory(app, _USER)
    agent = await create_test_agent(alice, name="restricted-fork-agent", user=_USER)
    source = await _json_create(alice, agent["id"])
    fork = await alice.post(f"/v1/sessions/{source}/fork", json={})
    assert fork.status_code == 201, fork.text
    assert not _is_public(perms, fork.json()["id"])


@pytest.mark.asyncio
async def test_bundle_child_of_runnerless_parent_stays_private(
    db_uri: str, tmp_path: Path, client_factory: Any
) -> None:
    """A bundle-uploaded child follows its parent's grants even when it didn't
    inherit a runner (the parent has none), so it gets no public grant of its own."""
    app, perms = _build_app(db_uri, tmp_path)
    alice = client_factory(app, _USER)
    parent = (await create_test_agent(alice, name="runnerless-parent", user=_USER))["_session_id"]
    assert not _is_public(perms, parent)
    write_default_public_sessions_override(DefaultPublicSessions.ALL)
    resp = await alice.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"parent_session_id": parent})},
        files={"bundle": ("agent.tar.gz", build_agent_bundle(name="child"), "application/gzip")},
    )
    assert resp.status_code == 201, resp.text
    child = resp.json()["session_id"]
    assert not _is_public(perms, child)


def _public_sessions(db_uri: str, perms: SqlAlchemyPermissionStore) -> list[str]:
    """Every session in the DB that carries a ``__public__`` grant."""
    convs = SqlAlchemyConversationStore(db_uri).list_conversations(limit=200).data
    return [c.id for c in convs if _is_public(perms, c.id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["sandbox", "all"])
async def test_rejected_managed_request_leaves_no_public_session(
    db_uri: str, tmp_path: Path, client_factory: Any, policy: str
) -> None:
    """A managed request the server rejects (no ``sandbox:`` config) may leave the
    owner's session behind, but never a public one: the default grant is written
    only after the launch is accepted. Covers JSON create, bundle create and fork."""
    app, perms = _build_app(db_uri, tmp_path)  # no sandbox config
    alice = client_factory(app, _USER)
    agent = await create_test_agent(alice, name="rejected-managed-agent", user=_USER)
    source = await _json_create(alice, agent["id"])
    write_default_public_sessions_override(DefaultPublicSessions(policy))
    public_before = set(_public_sessions(db_uri, perms))

    json_create = await alice.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "host_type": "managed",
            "initial_items": [
                {
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                }
            ],
        },
    )
    bundle_create = await alice.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"host_type": "managed"})},
        files={"bundle": ("agent.tar.gz", build_agent_bundle(name="managed"), "application/gzip")},
    )
    fork = await alice.post(f"/v1/sessions/{source}/fork", json={"host_type": "managed"})

    for resp in (json_create, bundle_create, fork):
        assert resp.status_code >= 400, resp.text
    assert set(_public_sessions(db_uri, perms)) == public_before


def test_invalid_override_file_fails_closed(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Garbage in the override file resolves to ``off`` even when the env default is
    permissive; only a missing file falls back to the env default."""
    monkeypatch.setenv("OMNIGENT_DEFAULT_PUBLIC_SESSIONS", "all")
    app, _ = _build_app(db_uri, tmp_path)
    assert app.state.default_public_sessions() is DefaultPublicSessions.ALL
    sharing_settings.resolve_default_public_sessions_path().parent.mkdir(
        parents=True, exist_ok=True
    )
    sharing_settings.resolve_default_public_sessions_path().write_text("everything\n")
    sharing_settings._cache = {}
    assert read_default_public_sessions_override() is DefaultPublicSessions.OFF
    assert app.state.default_public_sessions() is DefaultPublicSessions.OFF


@pytest.mark.asyncio
async def test_all_policy_is_inert_when_public_sharing_disabled(
    db_uri: str, tmp_path: Path, client_factory: Any
) -> None:
    write_default_public_sessions_override(DefaultPublicSessions.ALL)
    app, perms = _build_app(db_uri, tmp_path, public_sharing=False)
    alice = client_factory(app, _USER)
    agent = await create_test_agent(alice, name="inert-agent", user=_USER)
    assert not _is_public(perms, await _json_create(alice, agent["id"]))


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,expected_public", [("off", False), ("sandbox", True)])
async def test_managed_create_follows_policy(
    db_uri: str,
    tmp_path: Path,
    client_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    runtime_init: None,
    policy: str,
    expected_public: bool,
) -> None:
    """A ``host_type="managed"`` (cloud sandbox) session is public under ``sandbox``,
    while a local session on the same server stays private."""
    install_fake_modal_launcher(monkeypatch, FakeSandboxLauncher())
    # Keep the background launch from outliving the test.
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 0.1)
    write_default_public_sessions_override(DefaultPublicSessions(policy))
    app, perms = _build_app(db_uri, tmp_path, managed=True)
    alice = client_factory(app, _USER)
    agent = await create_test_agent(alice, name="managed-public-agent", user=_USER)
    managed = await _json_create(alice, agent["id"], host_type="managed")
    local = await _json_create(alice, agent["id"])
    assert _is_public(perms, managed) is expected_public
    assert not _is_public(perms, local)
    await asyncio.sleep(0.3)


# ── sessions bound to an existing sandbox host ───────────────────────

_SBX_HOST = "a" * 32
# A user's own machine that reconnected on a managed host's id under ordinary
# login: it keeps the sandbox_provider row but its live tunnel has no launch
# token, so it must NOT count as a sandbox (the Polly finding).
_RECONNECTED_LAPTOP = "b" * 32
_MISSING_HOST = "c" * 32
_BOUND_SESSION = "d" * 32


class _FakeHostConn:
    def __init__(self, registered_with_managed_token: bool) -> None:
        self.registered_with_managed_token = registered_with_managed_token


class _FakeHostRegistry:
    """Only the ``get`` the sandbox classifier calls, keyed on live provenance."""

    def __init__(self, sandbox_hosts: set[str]) -> None:
        self._sandbox_hosts = sandbox_hosts

    def get(self, host_id: str) -> _FakeHostConn | None:
        # Every registered host has a live connection; only sandbox launches
        # authenticated with a managed token.
        if host_id in (_SBX_HOST, _RECONNECTED_LAPTOP):
            return _FakeHostConn(host_id in self._sandbox_hosts)
        return None


def _host_state(policy: str) -> SimpleNamespace:
    """``app.state`` stand-in whose registry marks only ``_SBX_HOST`` a sandbox."""
    return SimpleNamespace(
        default_public_sessions=lambda: policy,
        sharing_mode=lambda: SharingMode.ON,
        public_sharing=lambda: True,
        host_registry=_FakeHostRegistry(sandbox_hosts={_SBX_HOST}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy,host_id,expected_public",
    [
        ("sandbox", _SBX_HOST, True),  # live sandbox connection counts as sandbox
        ("sandbox", _RECONNECTED_LAPTOP, False),  # local reconnect on a managed id stays private
        ("sandbox", _MISSING_HOST, False),
        ("sandbox", None, False),
        ("all", _RECONNECTED_LAPTOP, True),
        ("off", _SBX_HOST, False),
    ],
)
async def test_existing_sandbox_host_counts_as_sandbox(
    db_uri: str, policy: str, host_id: str | None, expected_public: bool
) -> None:
    """A session started on an already-running sandbox follows the sandbox policy,
    decided from the live connection's launch-token provenance — not a persisted
    marker a reconnecting local machine would keep."""
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(_USER)
    await _grant_default_public(
        _host_state(policy),
        perms,
        _BOUND_SESSION,
        managed=False,
        workspace=None,
        host_id=host_id,
    )
    assert _is_public(perms, _BOUND_SESSION) is expected_public
