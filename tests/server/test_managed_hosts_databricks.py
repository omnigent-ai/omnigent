"""YAML ``sandbox: provider: databricks`` parsing and owner-identity binding."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest
from fastapi import HTTPException

from omnigent.onboarding.sandboxes.base import OwnerCredentialMissingError
from omnigent.onboarding.sandboxes.databricks_sandbox import DatabricksSandboxLauncher
from omnigent.server.managed_hosts import (
    DATABRICKS_MANAGED_TOKEN_TTL_S,
    ManagedSandboxDeployment,
    build_owner_launcher,
    launch_managed_host,
    parse_sandbox_config,
)
from omnigent.stores.host_store import HostStore


def test_full_section_reaches_the_launcher() -> None:
    cfg = parse_sandbox_config(
        {
            "provider": "databricks",
            "server_url": "https://omnigent.example.com/",
            "databricks": {
                "profile": "sandbox-sp",
                "sandbox_id_prefix": "team",
                "inactivity_timeout_s": 3600,
                "bootstrap_command": "true",
                "proxy_bearer": True,
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
    assert launcher._profile == "sandbox-sp"
    assert launcher._prefix == "team"
    assert launcher._inactivity_timeout_s == 3600
    assert launcher._bootstrap_command == "true"
    assert launcher._proxy_bearer is True


def test_provider_and_server_url_alone_use_defaults() -> None:
    cfg = parse_sandbox_config({"provider": "databricks", "server_url": "https://x.example"})
    assert cfg is not None
    launcher = cfg.default.launcher_factory()
    assert isinstance(launcher, DatabricksSandboxLauncher)
    assert launcher._profile is None
    assert launcher._prefix == "omnigent"
    assert launcher._proxy_bearer is False


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown key"):
        parse_sandbox_config(
            {
                "provider": "databricks",
                "server_url": "https://x.example",
                "databricks": {"cli_path": "/usr/bin/databricks"},
            }
        )


# ── Owner identity (sandbox.databricks.identity: owner) ──────────────────────


def _owner_deployment(
    resolvers: dict[str, Callable[[str], dict[str, object] | None]] | None = None,
    **section: object,
) -> ManagedSandboxDeployment:
    cfg = parse_sandbox_config(
        {
            "provider": "databricks",
            "server_url": "https://x.example",
            "databricks": {"identity": "owner", **section},
        }
    )
    assert cfg is not None
    if resolvers is not None:
        cfg = replace(cfg, owner_credentials=resolvers)
    return cfg


def test_owner_identity_and_workspace_host_reach_the_launcher() -> None:
    cfg = _owner_deployment(workspace_host="https://ws.cloud.databricks.com/")
    launcher = cfg.default.launcher_factory()
    assert isinstance(launcher, DatabricksSandboxLauncher)
    assert launcher.owner_credential_provider == "databricks"
    assert launcher._workspace_host == "https://ws.cloud.databricks.com"


def test_identity_defaults_to_server() -> None:
    cfg = parse_sandbox_config({"provider": "databricks", "server_url": "https://x.example"})
    assert cfg is not None
    assert cfg.default.launcher_factory().owner_credential_provider is None


@pytest.mark.parametrize(
    ("section", "match"),
    [
        ({"identity": "robot"}, "identity"),
        ({"identity": "owner", "profile": "sp"}, "profile"),
    ],
)
def test_invalid_identity_config_is_rejected(section: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
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


def test_build_owner_launcher_leaves_server_identity_launchers_unbound() -> None:
    cfg = parse_sandbox_config({"provider": "databricks", "server_url": "https://x.example"})
    assert cfg is not None
    calls: list[str] = []
    cfg = replace(cfg, owner_credentials={"databricks": lambda u: calls.append(u)})
    launcher = build_owner_launcher(cfg.default, cfg, "alice@example.com")
    assert isinstance(launcher, DatabricksSandboxLauncher)
    assert launcher._owner_resolver is None
    assert calls == []


async def test_launch_for_an_unconnected_owner_is_a_409_connect_prompt(db_uri: str) -> None:
    cfg = _owner_deployment({"databricks": lambda _user: None})

    with pytest.raises(HTTPException) as info:
        await launch_managed_host(
            config=cfg, owner="alice@example.com", host_store=HostStore(db_uri)
        )

    assert info.value.status_code == 409
    assert "Connect Databricks" in str(info.value.detail)
