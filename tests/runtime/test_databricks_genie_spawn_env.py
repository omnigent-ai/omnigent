"""
Tests for ``_build_databricks_genie_spawn_env`` in ``omnigent/runtime/workflow.py``.

The builder maps ``spec`` fields to the ``HARNESS_DATABRICKS_GENIE_*`` env vars
the databricks-genie harness wrap reads at first-turn time: the Genie space id
from ``executor.model``, the Databricks profile from ``executor.auth``
(``type: databricks``) or the legacy ``executor.profile`` /
``executor.config["profile"]``, and the visualization opt-in from
``executor.config["enable_viz"]``. It also sizes the scaffold idle watchdog
(``HARNESS_TURN_TIMEOUT_S``) above the stream idle timeout unless the ambient
environment already sets it. There is NO gateway / base-URL resolution.

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
    enable_viz: str | None = None,
    auth: ApiKeyAuth | DatabricksAuth | None = None,
) -> AgentSpec:
    """Build a minimal databricks-genie :class:`AgentSpec` for the tests.

    :param enable_viz: Value for ``executor.config["enable_viz"]``. The spec
        parser stringifies config scalars, so a YAML ``true`` reaches the
        builder as ``"True"`` — pass the string form the parser would produce.
    """
    config: dict[str, object] = {"harness": "databricks-genie"}
    if model is not None:
        config["model"] = model
    if config_profile is not None:
        config["profile"] = config_profile
    if enable_viz is not None:
        config["enable_viz"] = enable_viz
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


def test_enable_viz_true_threads_into_env() -> None:
    """``executor.config: {enable_viz: true}`` → the env var, as its string form."""
    env = _build_databricks_genie_spawn_env(_make_spec(enable_viz="True"))
    assert env["HARNESS_DATABRICKS_GENIE_ENABLE_VIZ"] == "True"


def test_enable_viz_false_threads_into_env() -> None:
    """An explicit ``false`` is still exported; the wrap parses it back to off."""
    env = _build_databricks_genie_spawn_env(_make_spec(enable_viz="False"))
    assert env["HARNESS_DATABRICKS_GENIE_ENABLE_VIZ"] == "False"


def test_unset_enable_viz_omits_the_env_var() -> None:
    """No key in ``executor.config`` → no env var at all, not an empty one."""
    env = _build_databricks_genie_spawn_env(_make_spec())
    assert "HARNESS_DATABRICKS_GENIE_ENABLE_VIZ" not in env


def test_executor_profile_wins_over_config_profile() -> None:
    """Both legacy spellings set → ``executor.profile`` wins, matching
    ``_resolve_provider_for_build``'s precedence."""
    env = _build_databricks_genie_spawn_env(
        _make_spec(profile="from-executor", config_profile="from-config")
    )
    assert env["HARNESS_DATABRICKS_GENIE_PROFILE"] == "from-executor"


@pytest.fixture
def _clear_watchdog_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip the ambient watchdog / genie-timeout vars so sizing is deterministic."""
    monkeypatch.delenv("HARNESS_TURN_TIMEOUT_S", raising=False)
    monkeypatch.delenv("HARNESS_DATABRICKS_GENIE_TIMEOUT", raising=False)


def test_watchdog_sized_above_the_default_stream_idle_timeout(
    _clear_watchdog_env: None,
) -> None:
    """With nothing configured, the watchdog sits one margin above the 900s default."""
    env = _build_databricks_genie_spawn_env(_make_spec())
    assert env["HARNESS_TURN_TIMEOUT_S"] == "960.0"


def test_watchdog_sized_above_an_ambient_genie_timeout(
    _clear_watchdog_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raised stream idle timeout raises the watchdog with it."""
    monkeypatch.setenv("HARNESS_DATABRICKS_GENIE_TIMEOUT", "1200")
    env = _build_databricks_genie_spawn_env(_make_spec())
    assert env["HARNESS_TURN_TIMEOUT_S"] == "1260.0"


def test_ambient_watchdog_setting_wins(
    _clear_watchdog_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator-set ``HARNESS_TURN_TIMEOUT_S`` is inherited, never overridden."""
    monkeypatch.setenv("HARNESS_TURN_TIMEOUT_S", "300")
    env = _build_databricks_genie_spawn_env(_make_spec())
    assert "HARNESS_TURN_TIMEOUT_S" not in env


def test_malformed_genie_timeout_falls_back_to_default_watchdog_sizing(
    _clear_watchdog_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-positive genie timeout falls back to the default before sizing."""
    monkeypatch.setenv("HARNESS_DATABRICKS_GENIE_TIMEOUT", "-5")
    env = _build_databricks_genie_spawn_env(_make_spec())
    assert env["HARNESS_TURN_TIMEOUT_S"] == "960.0"
