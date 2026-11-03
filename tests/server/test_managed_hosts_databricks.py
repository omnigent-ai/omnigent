"""YAML ``sandbox: provider: databricks`` parsing and owner-identity binding."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace

import pytest
from fastapi import HTTPException

from omnigent.db.utils import now_epoch
from omnigent.onboarding.sandboxes.base import OwnerCredentialMissingError
from omnigent.onboarding.sandboxes.databricks_sandbox import DatabricksSandboxLauncher
from omnigent.server.managed_hosts import (
    DATABRICKS_MANAGED_TOKEN_TTL_S,
    ManagedSandboxConfig,
    ManagedSandboxDeployment,
    build_owner_launcher,
    launch_managed_host,
    parse_sandbox_config,
    relaunch_managed_host,
    resume_managed_host,
)
from omnigent.stores.host_store import Host, HostStore
from tests.server.helpers import FakeSandboxLauncher

_WS = "https://ws.cloud.databricks.com"


def test_full_section_reaches_the_launcher() -> None:
    cfg = parse_sandbox_config(
        {
            "provider": "databricks",
            "server_url": "https://omnigent.example.com/",
            "databricks": {
                "sandbox_id_prefix": "team",
                "inactivity_timeout_s": 3600,
                "bootstrap_command": "true",
                "proxy_bearer": True,
                "workspace_host": _WS + "/",
            },
        }
    )
    assert cfg is not None
    entry = cfg.default
    assert entry.provider == "databricks"
    assert entry.managed_launch_supported is True
    assert entry.token_ttl_s == DATABRICKS_MANAGED_TOKEN_TTL_S
    launcher = entry.launcher_factory()
    assert isinstance(launcher, DatabricksSandboxLauncher)
    assert launcher._prefix == "team"
    assert launcher._inactivity_timeout_s == 3600
    assert launcher._bootstrap_command == "true"
    assert launcher._proxy_bearer is True
    assert launcher._workspace_host == _WS


def test_workspace_host_is_required() -> None:
    with pytest.raises(ValueError, match="workspace_host' is required"):
        parse_sandbox_config({"provider": "databricks", "server_url": "https://x.example"})


def test_workspace_host_alone_uses_defaults() -> None:
    cfg = parse_sandbox_config(
        {
            "provider": "databricks",
            "server_url": "https://x.example",
            "databricks": {"workspace_host": _WS},
        }
    )
    assert cfg is not None
    launcher = cfg.default.launcher_factory()
    assert isinstance(launcher, DatabricksSandboxLauncher)
    assert launcher._prefix == "omnigent"
    assert launcher._proxy_bearer is False
    # Every sandbox runs as its owner; nothing falls back to the server identity.
    assert launcher.owner_credential_provider == "databricks"


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown key"):
        parse_sandbox_config(
            {
                "provider": "databricks",
                "server_url": "https://x.example",
                "databricks": {"cli_path": "/usr/bin/databricks"},
            }
        )


# ── Owner identity ───────────────────────────────────────────────────────────


def _owner_deployment(
    resolvers: dict[str, Callable[[str], dict[str, object] | None]] | None = None,
    **section: object,
) -> ManagedSandboxDeployment:
    cfg = parse_sandbox_config(
        {
            "provider": "databricks",
            "server_url": "https://x.example",
            "databricks": {"workspace_host": _WS, **section},
        }
    )
    assert cfg is not None
    if resolvers is not None:
        cfg = replace(cfg, owner_credentials=resolvers)
    return cfg


@pytest.mark.parametrize("section", [{"identity": "server"}, {"profile": "sp"}])
def test_shared_server_identity_cannot_be_configured(section: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="unknown key"):
        parse_sandbox_config(
            {"provider": "databricks", "server_url": "https://x.example", "databricks": section}
        )


def test_build_owner_launcher_binds_the_owners_resolver() -> None:
    seen: list[str] = []

    def resolve(user_id: str) -> dict[str, object] | None:
        seen.append(user_id)
        return None

    cfg = _owner_deployment({"databricks": resolve})
    launcher = build_owner_launcher(cfg.default, cfg, "alice@example.com")

    with pytest.raises(OwnerCredentialMissingError, match="Connect Databricks"):
        launcher.prepare()
    assert seen == ["alice@example.com"]


def test_build_owner_launcher_without_a_resolver_refuses_not_falls_back() -> None:
    cfg = _owner_deployment()
    launcher = build_owner_launcher(cfg.default, cfg, "alice@example.com")

    with pytest.raises(OwnerCredentialMissingError, match="OMNIGENT_DATABRICKS_CLIENT_ID"):
        launcher.prepare()


async def test_launch_for_an_unconnected_owner_is_a_409_connect_prompt(db_uri: str) -> None:
    cfg = _owner_deployment({"databricks": lambda _user: None})

    with pytest.raises(HTTPException) as info:
        await launch_managed_host(
            config=cfg, owner="alice@example.com", host_store=HostStore(db_uri)
        )

    assert info.value.status_code == 409
    assert "Connect Databricks" in str(info.value.detail)


_DORMANT_HOST_ID = "d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0"


def _dormant_owner_host(host_store: HostStore) -> Host:
    host_store.register_managed_host(
        host_id=_DORMANT_HOST_ID,
        name="managed-dormant",
        user_id="alice@example.com",
        token="tok-dormant",
        provider="databricks",
        sandbox_id="sb-dormant",
        token_expires_at=now_epoch() + 3600,
    )
    host = host_store.get_host(_DORMANT_HOST_ID)
    assert host is not None
    return host


def _assert_dormant_host_kept(host_store: HostStore) -> None:
    host = host_store.get_host(_DORMANT_HOST_ID)
    assert host is not None
    assert host.sandbox_id == "sb-dormant"
    assert host_store.resolve_launch_token(_DORMANT_HOST_ID, "tok-dormant") is not None


async def test_relaunch_for_an_unconnected_owner_is_a_409_and_keeps_the_host(
    db_uri: str,
) -> None:
    host_store = HostStore(db_uri)
    host = _dormant_owner_host(host_store)
    cfg = _owner_deployment({"databricks": lambda _user: None})

    with pytest.raises(HTTPException) as info:
        await relaunch_managed_host(config=cfg, host=host, host_store=host_store)

    assert info.value.status_code == 409
    assert "Connect Databricks" in str(info.value.detail)
    _assert_dormant_host_kept(host_store)


async def test_resume_for_an_unconnected_owner_is_a_409_and_keeps_the_host(
    db_uri: str,
) -> None:
    host_store = HostStore(db_uri)
    _dormant_owner_host(host_store)
    cfg = _owner_deployment({"databricks": lambda _user: None})

    with pytest.raises(HTTPException) as info:
        await resume_managed_host(_DORMANT_HOST_ID, host_store, cfg)

    assert info.value.status_code == 409
    assert "Connect Databricks" in str(info.value.detail)
    _assert_dormant_host_kept(host_store)


class _OwnerBoundFakeLauncher(FakeSandboxLauncher):
    """Fake launcher that, like Databricks, acts only as its bound owner."""

    def __init__(self, terminated_as: list[tuple[str, object]]) -> None:
        super().__init__(fail_on_host_start=True)
        self._terminated_as = terminated_as
        self._resolve: Callable[[], Mapping[str, object] | None] | None = None

    @property
    def owner_credential_provider(self) -> str | None:
        return "databricks"

    def bind_owner_credential(self, resolve: Callable[[], Mapping[str, object] | None]) -> None:
        self._resolve = resolve

    def terminate(self, sandbox_id: str) -> None:
        if self._resolve is None:
            raise OwnerCredentialMissingError("no owner credential bound")
        credential = self._resolve()
        assert credential is not None
        self._terminated_as.append((sandbox_id, credential["token"]))
        super().terminate(sandbox_id)


async def test_first_launch_failure_terminates_the_sandbox_as_its_owner(db_uri: str) -> None:
    # A failure after the host row is armed must still tear the Sandbox down
    # with the owner's credential, not an unbound launcher that refuses.
    terminated_as: list[tuple[str, object]] = []
    cfg = ManagedSandboxDeployment(
        configs=(
            ManagedSandboxConfig(
                server_url="https://x.example",
                launcher_factory=lambda: _OwnerBoundFakeLauncher(terminated_as),
                token_ttl_s=3600,
            ),
        ),
        owner_credentials={"databricks": lambda user: {"token": f"tok-{user}"}},
    )
    host_store = HostStore(db_uri)
    marked: list[str] = []
    mark = host_store.mark_sandbox_terminated

    def _spy_mark(host_id: str, *, sandbox_id: str) -> bool:
        marked.append(sandbox_id)
        return mark(host_id, sandbox_id=sandbox_id)

    host_store.mark_sandbox_terminated = _spy_mark  # type: ignore[method-assign]

    with pytest.raises(HTTPException) as info:
        await launch_managed_host(config=cfg, owner="alice@example.com", host_store=host_store)

    assert info.value.status_code == 502
    assert terminated_as == [("sb-fake-1", "tok-alice@example.com")]
    assert marked == ["sb-fake-1"]
    assert host_store.list_hosts("alice@example.com") == []


@pytest.mark.parametrize(("timeout_s", "ok"), [(1199, False), (1200, True)])
def test_inactivity_timeout_must_outlast_the_keepalive(
    monkeypatch: pytest.MonkeyPatch, timeout_s: int, ok: bool
) -> None:
    monkeypatch.delenv("OMNIGENT_MANAGED_KEEPALIVE_INTERVAL_S", raising=False)
    raw = {
        "provider": "databricks",
        "server_url": "https://x.example",
        "databricks": {"workspace_host": _WS, "inactivity_timeout_s": timeout_s},
    }
    if ok:
        assert parse_sandbox_config(raw) is not None
    else:
        with pytest.raises(ValueError, match="twice the keepalive interval"):
            parse_sandbox_config(raw)
