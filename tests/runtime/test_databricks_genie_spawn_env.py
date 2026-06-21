"""
Tests for ``_build_databricks_genie_spawn_env`` in ``omnigent/runtime/workflow.py``.

The builder maps ``spec`` fields to the ``HARNESS_DATABRICKS_GENIE_*`` env vars
the databricks-genie harness wrap reads at first-turn time: the Genie space id
from ``executor.model`` and the Databricks profile from ``executor.auth``
(``type: databricks``) or the legacy ``executor.profile`` /
``executor.config["profile"]``. There is NO gateway / base-URL resolution.

This is a unit test — no subprocess spawn. Mirrors ``test_cursor_spawn_env.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runtime.workflow import _build_databricks_genie_spawn_env
from omnigent.spec.types import (
    AgentSpec,
    ApiKeyAuth,
    DatabricksAuth,
    ExecutorSpec,
    LLMConfig,
)


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point OMNIGENT_CONFIG_HOME at an empty temp dir so the developer's real
    ``~/.omnigent/config.yaml`` can't leak into resolution."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))


def _make_spec(
    *,
    model: str | None = "space-xyz",
    profile: str | None = None,
    config_profile: str | None = None,
    auth: ApiKeyAuth | DatabricksAuth | None = None,
) -> AgentSpec:
    """Build a minimal databricks-genie :class:`AgentSpec` for the tests."""
    config: dict[str, object] = {"harness": "databricks-genie"}
    if model is not None:
        config["model"] = model
    if config_profile is not None:
        config["profile"] = config_profile
    return AgentSpec(
        spec_version=1,
        name="test-genie",
        instructions="You are a test agent.",
        executor=ExecutorSpec(
            type="omnigent", config=config, model=model, profile=profile, auth=auth
        ),
        llm=LLMConfig(model=model) if model is not None else None,
    )


def test_model_threads_into_space_id_env_var() -> None:
    """``executor.model`` (the space id) → ``HARNESS_DATABRICKS_GENIE_MODEL``."""
    env = _build_databricks_genie_spawn_env(_make_spec(model="space-xyz"))
    assert env["HARNESS_DATABRICKS_GENIE_MODEL"] == "space-xyz"


def test_no_model_produces_no_model_env_var() -> None:
    """A spec with no model omits the space-id env var (surfaces as a turn error)."""
    env = _build_databricks_genie_spawn_env(_make_spec(model=None))
    assert "HARNESS_DATABRICKS_GENIE_MODEL" not in env


def test_databricks_auth_profile_threads_into_env() -> None:
    """``executor.auth: {type: databricks, profile}`` → the profile env var."""
    env = _build_databricks_genie_spawn_env(_make_spec(auth=DatabricksAuth(profile="oss")))
    assert env["HARNESS_DATABRICKS_GENIE_PROFILE"] == "oss"


def test_legacy_executor_profile_threads_into_env() -> None:
    """The legacy ``executor.profile`` shorthand still resolves the profile."""
    env = _build_databricks_genie_spawn_env(_make_spec(profile="legacy"))
    assert env["HARNESS_DATABRICKS_GENIE_PROFILE"] == "legacy"


def test_legacy_config_profile_threads_into_env() -> None:
    """The legacy ``executor.config["profile"]`` shorthand resolves the profile."""
    env = _build_databricks_genie_spawn_env(_make_spec(config_profile="cfgprof"))
    assert env["HARNESS_DATABRICKS_GENIE_PROFILE"] == "cfgprof"


def test_no_profile_omits_profile_env_var() -> None:
    """With no auth and no legacy profile, the profile env var is omitted."""
    env = _build_databricks_genie_spawn_env(_make_spec())
    assert "HARNESS_DATABRICKS_GENIE_PROFILE" not in env


def test_api_key_auth_does_not_set_profile() -> None:
    """A non-Databricks (api_key) auth has no profile; the env var is omitted."""
    env = _build_databricks_genie_spawn_env(_make_spec(auth=ApiKeyAuth(api_key="k")))
    assert "HARNESS_DATABRICKS_GENIE_PROFILE" not in env
