"""Tests for the generated, broker-backed Databricks token helper."""

from __future__ import annotations

import os
import subprocess

import pytest


def test_generated_helper_uses_central_broker_and_never_refreshes_directly() -> None:
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    command = databricks_bearer_token_command("https://example.databricks.com", "agent")

    assert "omnigent.databricks_auth_broker" in command
    assert "--workspace-host https://example.databricks.com" in command
    assert "--profile agent" in command
    assert "databricks auth token" not in command
    assert "--force-refresh" not in command


def test_generated_helper_selects_by_host_without_a_profile() -> None:
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    command = databricks_bearer_token_command("https://example.databricks.com/", None)

    assert "--host https://example.databricks.com" in command
    assert "--profile" not in command


def test_recorded_fallback_command_is_not_embedded() -> None:
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    command = databricks_bearer_token_command(
        "https://example.databricks.com",
        "agent",
        fallback_command="dangerous-independent-refresh --force-refresh",
    )

    assert "dangerous-independent-refresh" not in command
    assert "--force-refresh" not in command


@pytest.mark.posix_only
def test_explicit_injected_bearer_still_short_circuits_broker() -> None:
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    command = databricks_bearer_token_command("https://example.databricks.com", "agent")
    env = {**os.environ, "DATABRICKS_BEARER": "injected"}
    result = subprocess.run(
        ["sh", "-c", command], env=env, capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "injected"
