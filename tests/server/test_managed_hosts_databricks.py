"""YAML ``sandbox: provider: databricks`` parsing."""

from __future__ import annotations

import pytest

from omnigent.onboarding.sandboxes.databricks_sandbox import DatabricksSandboxLauncher
from omnigent.server.managed_hosts import DATABRICKS_MANAGED_TOKEN_TTL_S, parse_sandbox_config


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
