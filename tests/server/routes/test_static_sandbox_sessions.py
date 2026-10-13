"""Sessions on static sandboxes: create-time resolution, wake, and reconnect."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from omnigent.entities.conversation import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import vm_hosts
from omnigent.server.app import create_app
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from omnigent.server.routes._sandbox_target import resolve_sandbox_target
from omnigent.server.routes._sessions import orchestration
from omnigent.server.schemas import SessionCreateRequest
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import FakeSandboxLauncher
from tests.server.static_sandbox_helpers import OWNER, WS, VmShim, make_deployment, make_shim

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    vm_hosts._lookup_cache.clear()
    vm_hosts._providers_cache.clear()


@pytest.fixture(autouse=True)
def _restore_server_globals() -> Iterator[None]:
    """create_app stashes its runner router and host registry in module globals."""
    from omnigent.server.routes._sessions import common

    router, registry = common.get_server_runner_router(), common.get_server_host_registry()
    yield
    common.set_server_runner_router(router)
    common.set_server_host_registry(registry)


@pytest.fixture()
def host_store(db_uri: str) -> HostStore:
    return HostStore(db_uri)


@pytest.fixture()
def shim(host_store: HostStore) -> VmShim:
    service = make_shim(host_store)
    service.workspace_path = "/workspaces/web-app"
    return service


@pytest.fixture()
def deployment(shim: VmShim) -> ManagedSandboxDeployment:
    return make_deployment(shim)


def _request(deployment: ManagedSandboxDeployment, host_store: HostStore) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(sandbox_config=deployment, host_store=host_store)
        )
    )


def _body(**kwargs: Any) -> SessionCreateRequest:
    return SessionCreateRequest(
        **{"agent_id": "ag", **kwargs},
        sandbox_target={"kind": "static", "provider": "remote", "sandbox_id": WS},
    )


# -- schema ------------------------------------------------------------------


def test_sandbox_target_rejects_host_id_and_managed() -> None:
    with pytest.raises(ValueError, match="host_type 'external' without host_id"):
        _body(host_id="h")
    with pytest.raises(ValueError, match="host_type 'external'"):
        _body(host_type="managed")
    with pytest.raises(ValueError, match="repository URL"):
        _body(workspace="not a url")


def test_sandbox_target_counts_as_a_host_for_git() -> None:
    assert _body(git={"branch_name": "fix-login"}).git is not None


def test_unknown_target_kind_is_rejected() -> None:
    with pytest.raises(ValueError):
        SessionCreateRequest(agent_id="ag", sandbox_target={"kind": "session", "session_id": "x"})


# -- create-time resolution --------------------------------------------------


async def test_resolves_host_workspace_and_a_fresh_branch(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    body = await resolve_sandbox_target(
        _body(workspace="https://github.com/example/sample-app#main"),
        user_id=OWNER,
        request=_request(deployment, host_store),
    )
    assert body.host_id == shim.static_hosts[WS]
    assert host_store.is_online(body.host_id)
    assert body.workspace == "/workspaces/web-app"
    # The requested branch is the worktree's base, never checked out in the clone.
    assert shim.body(f"/v1/sandboxes/{WS}/workspace") == {
        "repo_url": "https://github.com/example/sample-app",
        "repo_branch": None,
        "repo_name": "web-app",
    }
    assert body.git is not None
    assert body.git.base_branch == "main"
    assert body.git.branch_name.startswith("omnigent/")


@pytest.mark.parametrize("cloned", [True, False])
async def test_an_existing_clone_skips_the_shim_workspace_call(
    shim: VmShim,
    deployment: ManagedSandboxDeployment,
    host_store: HostStore,
    monkeypatch: pytest.MonkeyPatch,
    cloned: bool,
) -> None:
    from omnigent.server.routes import _sandbox_target

    stat_paths: list[str] = []

    async def fake_stat(*, host_registry: Any, host_conn: Any, path: str) -> dict[str, Any]:
        stat_paths.append(path)
        return {"status": "ok", "exists": cloned, "type": "directory" if cloned else None}

    monkeypatch.setattr(_sandbox_target, "ask_host_stat", fake_stat)
    request = _request(deployment, host_store)
    request.app.state.host_registry = SimpleNamespace(get=lambda host_id: object())
    body = await resolve_sandbox_target(
        _body(workspace="https://github.com/example/Sample-App.git"),
        user_id=OWNER,
        request=request,
    )
    # The provider names the checkout path; the host says whether it exists.
    assert stat_paths == ["/workspaces/web-app/.git"]
    assert shim.request_queries[shim.paths().index(f"/v1/static-sandboxes/{WS}/checkout")] == {
        "repo_url": "https://github.com/example/Sample-App.git"
    }
    asked_shim = f"/v1/sandboxes/{WS}/workspace" in shim.paths("POST")
    assert asked_shim is not cloned
    assert body.workspace == "/workspaces/web-app"


async def test_a_resolved_target_survives_project_revalidation(
    deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    from omnigent.server.routes._session_create_validation import (
        resolve_project_session_create,
    )

    body = await resolve_sandbox_target(
        _body(project_id="proj_1"), user_id=OWNER, request=_request(deployment, host_store)
    )
    assert body.sandbox_target is None
    projects = SimpleNamespace(
        get=lambda project_id, user_id: SimpleNamespace(config={"agent_id": "ag"})
    )
    resolved = await resolve_project_session_create(
        body=body, user_id=OWNER, project_store=projects
    )
    assert resolved.project_id == "proj_1"
    assert resolved.body.host_id == body.host_id


async def test_every_session_gets_its_own_branch(
    deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    request = _request(deployment, host_store)
    first = await resolve_sandbox_target(_body(), user_id=OWNER, request=request)
    second = await resolve_sandbox_target(_body(), user_id=OWNER, request=request)
    assert first.host_id == second.host_id
    assert first.git and second.git
    assert first.git.branch_name != second.git.branch_name


async def test_omitted_workspace_uses_the_vms_default_repo_url(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    shim.static_repo = "example/Sample-App"
    await resolve_sandbox_target(_body(), user_id=OWNER, request=_request(deployment, host_store))
    assert shim.body(f"/v1/sandboxes/{WS}/workspace")["repo_url"] == (  # type: ignore[index]
        "https://github.com/example/Sample-App"
    )


async def test_omitted_workspace_without_a_primary_repo_is_refused(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    shim.static_repo = ""
    with pytest.raises(OmnigentError, match="has no primary repository") as exc:
        await resolve_sandbox_target(
            _body(), user_id=OWNER, request=_request(deployment, host_store)
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert not any(p.endswith("/workspace") for p in shim.paths("POST"))


async def test_an_absolute_path_is_used_as_is(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    body = await resolve_sandbox_target(
        _body(workspace="/home/dev/src/app", git={"branch_name": "fix-login"}),
        user_id=OWNER,
        request=_request(deployment, host_store),
    )
    assert body.workspace == "/home/dev/src/app"
    assert body.git is not None and body.git.branch_name == "fix-login"
    assert not any(p.endswith("/workspace") for p in shim.paths("POST"))


async def test_unknown_target_is_404_and_gone_vm_is_410(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    request = _request(deployment, host_store)
    with pytest.raises(OmnigentError) as missing:
        await resolve_sandbox_target(
            SessionCreateRequest(
                agent_id="ag",
                sandbox_target={"kind": "static", "provider": "remote", "sandbox_id": "nope"},
            ),
            user_id=OWNER,
            request=request,
        )
    assert missing.value.http_status == 404
    with pytest.raises(OmnigentError) as bad_provider:
        await resolve_sandbox_target(
            SessionCreateRequest(
                agent_id="ag",
                sandbox_target={"kind": "static", "provider": "modal", "sandbox_id": WS},
            ),
            user_id=OWNER,
            request=request,
        )
    assert bad_provider.value.http_status == 400
    del shim.vms[WS]
    with pytest.raises(OmnigentError) as gone:
        await resolve_sandbox_target(_body(), user_id=OWNER, request=request)
    assert gone.value.code == ErrorCode.SANDBOX_GONE and gone.value.http_status == 410


async def test_a_signed_out_caller_is_refused_before_the_shim(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    before = len(shim.requests)
    with pytest.raises(OmnigentError, match="signed-in user") as exc:
        await resolve_sandbox_target(
            _body(), user_id=None, request=_request(deployment, host_store)
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert len(shim.requests) == before


async def test_a_failed_clone_is_typed(
    shim: VmShim,
    deployment: ManagedSandboxDeployment,
    host_store: HostStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import click

    from omnigent.community.sandbox.remote.launcher import RemoteSandboxLauncher

    def _fail(*_a: Any, **_k: Any) -> str:
        raise click.ClickException("repository not found")

    monkeypatch.setattr(RemoteSandboxLauncher, "materialize_workspace", _fail)
    with pytest.raises(OmnigentError, match="repository not found") as exc:
        await resolve_sandbox_target(
            _body(), user_id=OWNER, request=_request(deployment, host_store)
        )
    assert exc.value.code == ErrorCode.WORKSPACE_PREPARE_FAILED
    assert exc.value.http_status == 502


async def test_precheck_rejects_a_bad_agent_or_project_without_the_shim(
    db_uri: str, shim: VmShim
) -> None:
    from omnigent.server.routes._sandbox_target import precheck_sandbox_target_create

    stores = {
        "agent_store": SqlAlchemyAgentStore(db_uri),
        "permission_store": None,
        "conversation_store": SqlAlchemyConversationStore(db_uri),
    }
    before = len(shim.requests)
    with pytest.raises(OmnigentError, match="Agent not found") as agent:
        await precheck_sandbox_target_create(
            _body(agent_id="f" * 32), user_id=OWNER, project_store=None, **stores
        )
    assert agent.value.http_status == 404
    no_projects = SimpleNamespace(get=lambda project_id, user_id: None)
    with pytest.raises(OmnigentError, match="Project not found"):
        await precheck_sandbox_target_create(
            _body(project_id="proj_1"), user_id=OWNER, project_store=no_projects, **stores
        )
    with pytest.raises(OmnigentError, match="signed-in user"):
        await precheck_sandbox_target_create(_body(), user_id=None, project_store=None, **stores)
    assert len(shim.requests) == before


# -- reconnect hook (ensure_runner_connected) --------------------------------


class _Registry:
    """A host registry where a host is connected once its row reads online."""

    def __init__(self, host_store: HostStore) -> None:
        self._host_store = host_store
        self.conn = SimpleNamespace(owner=OWNER)

    def get(self, host_id: str) -> Any:
        return self.conn if self._host_store.is_online(host_id) else None


async def _arm_offline_static_host(
    deployment: ManagedSandboxDeployment, host_store: HostStore
) -> str:
    host_id = await vm_hosts.ensure_vm_host(
        owner=OWNER,
        provider="remote",
        sandbox_id=WS,
        sandbox_config=deployment,
        host_store=host_store,
    )
    host_store.set_offline(host_id)
    vm_hosts._lookup_cache.clear()
    vm_hosts._providers_cache.clear()
    return host_id


def _conv(host_id: str) -> Conversation:
    return Conversation(
        id="conv_1",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_1",
        agent_id="agent_1",
        host_id=host_id,
        workspace="/w",
    )


class _Store:
    def __init__(self, conv: Conversation) -> None:
        self._conv = conv

    def get_conversation(self, _id: str) -> Conversation:
        return self._conv


def _patch_runner_launch(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    launched: list[Any] = []

    async def _none(*_a: Any, **_k: Any) -> Any:
        return None

    async def _false(*_a: Any, **_k: Any) -> bool:
        return False

    async def _launch(conv: Conversation, *_a: Any, **_k: Any) -> Any:
        launched.append(conv.id)
        return SimpleNamespace(runner_id="runner_new", error_code=None, error=None)

    async def _wait(*_a: Any, **_k: Any) -> Any:
        return object()

    monkeypatch.setattr(orchestration, "_get_runner_client", _none)
    monkeypatch.setattr(orchestration, "_maybe_wake_stale_resumable_managed_sandbox", _false)
    monkeypatch.setattr(orchestration, "_launch_runner_on_host", _launch)
    monkeypatch.setattr(orchestration, "_wait_for_runner_client", _wait)
    return launched


async def _connect(app_state: Any, host_id: str) -> Any:
    conv = _conv(host_id)
    return await orchestration.ensure_runner_connected(
        session_id="conv_1",
        conv=conv,
        app_state=app_state,
        conversation_store=_Store(conv),
        runner_router=None,
    )


async def test_an_offline_static_host_restarts_before_the_runner_launches(
    shim: VmShim,
    deployment: ManagedSandboxDeployment,
    host_store: HostStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_id = await _arm_offline_static_host(deployment, host_store)
    shim.vms[WS] = "paused"
    launched = _patch_runner_launch(monkeypatch)
    app_state = SimpleNamespace(
        host_store=host_store,
        sandbox_config=deployment,
        host_registry=_Registry(host_store),
        tunnel_registry=None,
    )
    client, _ = await _connect(app_state, host_id)
    assert client is not None
    assert launched == ["conv_1"]
    assert f"/v1/static-sandboxes/{WS}/resume" in shim.paths("POST")
    assert len(shim.started_tokens) == 2


async def test_concurrent_wakes_resume_once_and_start_one_host(
    shim: VmShim,
    deployment: ManagedSandboxDeployment,
    host_store: HostStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_id = await _arm_offline_static_host(deployment, host_store)
    shim.vms[WS] = "paused"
    _patch_runner_launch(monkeypatch)
    app_state = SimpleNamespace(
        host_store=host_store,
        sandbox_config=deployment,
        host_registry=_Registry(host_store),
        tunnel_registry=None,
    )
    await asyncio.gather(*(_connect(app_state, host_id) for _ in range(3)))
    assert shim.paths("POST").count(f"/v1/static-sandboxes/{WS}/resume") == 1
    assert len(shim.started_tokens) == 2  # the initial start plus one restart


async def test_a_gone_vm_surfaces_a_clear_error(
    shim: VmShim,
    deployment: ManagedSandboxDeployment,
    host_store: HostStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_id = await _arm_offline_static_host(deployment, host_store)
    del shim.vms[WS]
    _patch_runner_launch(monkeypatch)
    app_state = SimpleNamespace(
        host_store=host_store,
        sandbox_config=deployment,
        host_registry=_Registry(host_store),
        tunnel_registry=None,
    )
    with pytest.raises(OmnigentError, match="no longer exists"):
        await _connect(app_state, host_id)


async def test_an_unlinked_external_host_is_not_static(
    shim: VmShim,
    deployment: ManagedSandboxDeployment,
    host_store: HostStore,
) -> None:
    host_store.upsert_on_connect(host_id="1" * 32, name="laptop", user_id=OWNER)
    host_store.set_offline("1" * 32)
    before = len(shim.requests)
    app_state = SimpleNamespace(
        host_store=host_store, sandbox_config=deployment, host_registry=_Registry(host_store)
    )
    for _ in range(2):
        assert (
            await orchestration._maybe_ensure_vm_host(conv=_conv("1" * 32), app_state=app_state)
            is None
        )
    # One capabilities read and one cached list of the owner's entries; nothing touches a VM.
    assert [r[:2] for r in shim.requests[before:]] == [
        ("GET", "/v1/capabilities"),
        ("GET", "/v1/static-sandboxes"),
    ]


# -- POST /v1/sessions/{id}/wake ---------------------------------------------


class _SignedIn:
    """Every request is :data:`OWNER`."""

    def get_user_id(self, request: Any) -> str | None:
        return OWNER


def _app(
    db_uri: str, tmp_path: Path, host_store: HostStore, config: Any, auth_provider: Any = None
) -> Any:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        auth_provider=auth_provider,
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        host_store=host_store,
        sandbox_config=config,
    )


async def _post_wake(app: Any, session_id: str) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(f"/v1/sessions/{session_id}/wake")


async def test_wake_endpoint_wakes_a_static_host(
    db_uri: str,
    tmp_path: Path,
    shim: VmShim,
    deployment: ManagedSandboxDeployment,
    host_store: HostStore,
) -> None:
    shim_owner_host = await vm_hosts.ensure_vm_host(
        owner=OWNER,
        provider="remote",
        sandbox_id=WS,
        sandbox_config=deployment,
        host_store=host_store,
    )
    host_store.set_offline(shim_owner_host)
    vm_hosts._lookup_cache.clear()
    vm_hosts._providers_cache.clear()
    shim.vms[WS] = "paused"
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(
        agent_id="a" * 32, host_id=shim_owner_host, workspace="/w"
    )
    resp = await _post_wake(_app(db_uri, tmp_path, host_store, deployment), conv.id)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"host_id": shim_owner_host, "kind": "static"}
    assert host_store.is_online(shim_owner_host)
    assert f"/v1/static-sandboxes/{WS}/resume" in shim.paths("POST")


async def test_wake_endpoint_refuses_a_host_without_resume(
    db_uri: str, tmp_path: Path, host_store: HostStore
) -> None:
    fake = FakeSandboxLauncher()  # Modal-style: no resume
    config = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://s", launcher_factory=lambda: fake, token_ttl_s=60, provider="modal"
        )
    )
    host_store.register_managed_host(
        host_id="2" * 32,
        name="managed-2",
        user_id=OWNER,
        token="t",
        provider="modal",
        sandbox_id="sb-1",
        token_expires_at=10**10,
    )
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(
        agent_id="a" * 32, host_id="2" * 32, workspace="/w"
    )
    resp = await _post_wake(_app(db_uri, tmp_path, host_store, config), conv.id)
    assert resp.status_code == 409
    assert "not_resumable" in resp.text
    assert fake.provisioned_names == [] and fake.resumed == []


async def test_snapshot_reports_a_paused_static_host_and_skips_healthy_ones(
    db_uri: str,
    tmp_path: Path,
    shim: VmShim,
    deployment: ManagedSandboxDeployment,
    host_store: HostStore,
) -> None:
    host_id = await vm_hosts.ensure_vm_host(
        owner=OWNER,
        provider="remote",
        sandbox_id=WS,
        sandbox_config=deployment,
        host_store=host_store,
    )
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(
        agent_id="a" * 32, host_id=host_id, workspace="/w"
    )
    app = _app(db_uri, tmp_path, host_store, deployment)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        before = len(shim.requests)
        healthy = (await client.get(f"/v1/sessions/{conv.id}")).json()
        assert healthy["sandbox_state"] is None
        assert len(shim.requests) == before  # a live host costs no shim call

        host_store.set_offline(host_id)
        shim.vms[WS] = "paused"
        paused = (await client.get(f"/v1/sessions/{conv.id}")).json()
        assert paused["sandbox_state"] == "paused"
        assert paused["sandbox_name"] == WS
        assert paused["host_resumable"] is True

        vm_hosts._lookup_cache.clear()
        vm_hosts._providers_cache.clear()
        del shim.vms[WS]
        gone = (await client.get(f"/v1/sessions/{conv.id}")).json()
        assert gone["sandbox_state"] == "gone"
        assert gone["host_resumable"] is False


# -- plain host_id create and host lists --------------------------------------


async def test_plain_host_id_create_wakes_an_offline_static_host(
    db_uri: str,
    tmp_path: Path,
    shim: VmShim,
    deployment: ManagedSandboxDeployment,
    host_store: HostStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes.sessions import routes_core

    host_id = await _arm_offline_static_host(deployment, host_store)
    shim.vms[WS] = "paused"
    seen: list[bool] = []

    async def _create(*_a: Any, **_k: Any) -> Any:
        # Workspace validation runs in here; the host must already be back.
        seen.append(host_store.is_online(host_id))
        raise OmnigentError("stop here", code=ErrorCode.INVALID_INPUT)

    monkeypatch.setattr(routes_core, "_create_session_from_existing_agent", _create)
    app = _app(db_uri, tmp_path, host_store, deployment)
    app.state.host_registry = _Registry(host_store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/sessions",
            json={"agent_id": "a" * 32, "host_id": host_id, "workspace": "/w"},
        )
    assert resp.status_code == 400, resp.text
    assert seen == [True]
    assert f"/v1/static-sandboxes/{WS}/resume" in shim.paths("POST")


async def test_plain_host_id_wake_skips_other_users_and_ordinary_hosts(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    host_id = await _arm_offline_static_host(deployment, host_store)
    host_store.upsert_on_connect(host_id="1" * 32, name="laptop", user_id=OWNER)
    host_store.set_offline("1" * 32)
    before = len(shim.requests)
    for hid, user in ((host_id, "mallory"), ("1" * 32, OWNER)):
        await vm_hosts.wake_static_host_for_create(
            hid,
            user_id=user,
            host_store=host_store,
            sandbox_config=deployment,
            host_registry=_Registry(host_store),
        )
    assert all(method == "GET" for method, *_ in shim.requests[before:])
    assert not host_store.is_online(host_id)


def _arm_static_row(host_store: HostStore, host_id: str, sandbox_id: str, user: str) -> None:
    host_store.upsert_static_host(
        host_id=host_id,
        name=vm_hosts.static_host_name(sandbox_id),
        user_id=user,
        token=f"token-{host_id}",
        token_expires_at=10**10,
    )


async def test_host_list_names_the_static_sandbox_from_its_link(
    db_uri: str,
    tmp_path: Path,
    shim: VmShim,
    deployment: ManagedSandboxDeployment,
    host_store: HostStore,
) -> None:
    host_id = "3" * 32
    shim.static_hosts[WS] = host_id
    _arm_static_row(host_store, host_id, "any name", OWNER)
    # A host named like the sandbox id, but unlinked, is not static.
    host_store.upsert_on_connect(host_id="1" * 32, name=WS, user_id=OWNER)
    app = _app(db_uri, tmp_path, host_store, deployment, auth_provider=_SignedIn())
    before = len(shim.requests)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listed = (await client.get("/v1/hosts")).json()["hosts"]
        single = (await client.get(f"/v1/hosts/{host_id}")).json()
    by_id = {h["host_id"]: h for h in listed}
    assert by_id[host_id]["static_sandbox"] == {"provider": "remote", "sandbox_id": WS}
    assert by_id["1" * 32]["static_sandbox"] is None
    assert single["static_sandbox"] == {"provider": "remote", "sandbox_id": WS}
    # One list for both requests: the lookup is cached per (provider, owner).
    assert [r[:2] for r in shim.requests[before:]] == [
        ("GET", "/v1/capabilities"),
        ("GET", "/v1/static-sandboxes"),
    ]


async def test_a_long_sandbox_id_resolves_through_the_link(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    long_ws = "w" * 60
    host_id = "4" * 32
    shim.vms[long_ws] = "paused"
    shim.static_labels[long_ws] = long_ws
    shim.static_hosts[long_ws] = host_id
    _arm_static_row(host_store, host_id, "my build box", OWNER)
    host = host_store.get_host(host_id)
    assert host is not None
    refs = await vm_hosts.static_sandbox_refs([host], deployment)
    assert {k: (r.provider, r.sandbox_id) for k, r in refs.items()} == {
        host_id: ("remote", long_ws)
    }


async def test_info_offers_static_sandboxes_only_with_auth(
    db_uri: str,
    tmp_path: Path,
    deployment: ManagedSandboxDeployment,
    host_store: HostStore,
) -> None:
    for auth_provider, offered in ((None, False), (_SignedIn(), True)):
        app = _app(db_uri, tmp_path, host_store, deployment, auth_provider=auth_provider)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            info = (await client.get("/v1/info")).json()
        assert info["static_sandboxes"] is offered
        assert info["static_sandbox_providers"] == (["remote"] if offered else [])


async def test_info_drops_static_sandboxes_when_the_service_does(
    db_uri: str,
    tmp_path: Path,
    shim: VmShim,
    deployment: ManagedSandboxDeployment,
    host_store: HostStore,
) -> None:
    del shim.declared_capabilities["static_sandboxes"]
    app = _app(db_uri, tmp_path, host_store, deployment, auth_provider=_SignedIn())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        info = (await client.get("/v1/info")).json()
        # The routes stay mounted and list nothing; the old paths are gone.
        listed = await client.get("/v1/static-sandboxes")
        old = await client.get("/v1/sandbox-targets")
    assert info["static_sandboxes"] is False and info["static_sandbox_providers"] == []
    assert listed.json() == {"static_sandboxes": []}
    assert old.status_code == 404
