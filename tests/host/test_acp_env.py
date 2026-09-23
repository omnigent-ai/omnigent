"""ACP environment exclusions survive each process launch boundary."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.cli import _build_host_daemon_env
from omnigent.host.connect import _build_runner_env
from omnigent.inner.acp_harness import _build_acp_executor
from omnigent.runtime.workflow import _build_acp_spawn_env
from omnigent.spec.types import AgentSpec, ExecutorSpec


@pytest.mark.parametrize("server_url", [None, "https://example.databricksapps.com"])
def test_acp_env_exclusion_reaches_vendor_through_daemon_and_runner(
    tmp_path: Path, server_url: str | None
) -> None:
    """Keep requested credentials available to other harnesses, then strip them for ACP."""
    # Remote daemons inherit Databricks credentials; local daemons also inherit API keys.
    prefix = "DATABRICKS_" if server_url else ""
    excluded_name = f"{prefix}OPENAI_API_KEY"
    retained_name = f"{prefix}ANTHROPIC_API_KEY"
    spec = AgentSpec(
        spec_version=1,
        name="test-acp",
        instructions="Test agent",
        executor=ExecutorSpec(
            type="omnigent",
            config={
                "harness": "acp:helper",
                "acp_agent": {
                    "name": "Helper",
                    "command": "helper --acp",
                    "env_passthrough": [excluded_name, retained_name],
                },
            },
        ),
    )
    host_env = {
        "OMNIGENT_CONFIG_HOME": str(tmp_path),
        "OMNIGENT_ACP_ENV_UNSET": f"{excluded_name},UNRELATED_SECRET",
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH": f"{excluded_name},{retained_name}",
        excluded_name: "synthetic-excluded-key",
        retained_name: "synthetic-retained-key",
        "UNRELATED_SECRET": "synthetic-unrelated-secret",
    }
    with patch.dict(os.environ, host_env, clear=True):
        daemon_env = _build_host_daemon_env(server_url=server_url)
        runner_env = _build_runner_env(
            daemon_env,
            server_url=server_url or "http://localhost:6767",
            runner_id="runner_acp_env",
            binding_token="synthetic-binding",
            workspace=str(tmp_path),
            parent_pid=12345,
        )
    for env in (daemon_env, runner_env):
        assert env["OMNIGENT_ACP_ENV_UNSET"] == host_env["OMNIGENT_ACP_ENV_UNSET"]
        assert env[excluded_name] == host_env[excluded_name]
        assert "UNRELATED_SECRET" not in env

    with patch.dict(os.environ, runner_env, clear=True):
        overrides = _build_acp_spawn_env(spec)
        with patch.dict(os.environ, overrides):
            executor = _build_acp_executor()
            vendor_env = executor._build_spawn_env()
    assert excluded_name not in vendor_env
    assert vendor_env[retained_name] == host_env[retained_name]
    assert "OMNIGENT_ACP_ENV_UNSET" not in vendor_env
    assert "UNRELATED_SECRET" not in vendor_env
