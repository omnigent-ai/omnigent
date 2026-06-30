"""
Tests for ``_build_copilot_spawn_env`` in ``omnigent/runtime/workflow.py``.

The spawn-env builder maps ``spec`` fields to the ``HARNESS_COPILOT_*`` env
vars the copilot harness wrap reads at first-turn time. Like the cursor builder,
copilot has NO Databricks-gateway path: only an explicit ``api_key`` auth maps
to ``HARNESS_COPILOT_GITHUB_TOKEN`` (the GitHub token), a stored ``copilot:``
block or an ambient ``GH_TOKEN`` is the no-auth fallback, and a ``DatabricksAuth``
profile is deliberately ignored. Mirrors ``test_cursor_spawn_env.py``.

This is a unit test — no subprocess spawn. End-to-end verification of the
spawn-env → wrap → executor path lives in the harness e2e tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.runtime.workflow import _build_copilot_spawn_env
from omnigent.spec.types import (
    AgentSpec,
    ApiKeyAuth,
    DatabricksAuth,
    ExecutorSpec,
    LLMConfig,
)


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate the global config to an empty tmp dir and clear ambient GitHub
    tokens so the no-auth / DatabricksAuth cases are deterministic."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _make_spec(
    *,
    model: str | None = "claude-haiku-4.5",
    name: str = "test-copilot",
    auth: ApiKeyAuth | DatabricksAuth | None = None,
) -> AgentSpec:
    """Build a minimal copilot :class:`AgentSpec` for the spawn-env tests."""
    config: dict[str, object] = {"harness": "copilot"}
    if model is not None:
        config["model"] = model
    return AgentSpec(
        spec_version=1,
        name=name,
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnigent", config=config, model=model, auth=auth),
        llm=LLMConfig(model=model) if model is not None else None,
    )


def test_model_and_name_threaded() -> None:
    env = _build_copilot_spawn_env(_make_spec(model="gpt-5-mini", name="cop"))
    assert env["HARNESS_COPILOT_MODEL"] == "gpt-5-mini"
    assert env["HARNESS_COPILOT_AGENT_NAME"] == "cop"
    # skills filter is always set (parity with peer builders).
    assert "HARNESS_COPILOT_SKILLS_FILTER" in env


def test_api_key_auth_maps_to_github_token() -> None:
    env = _build_copilot_spawn_env(_make_spec(auth=ApiKeyAuth(api_key="gho_fromspec")))
    assert env["HARNESS_COPILOT_GITHUB_TOKEN"] == "gho_fromspec"


def test_databricks_auth_is_ignored() -> None:
    # A Databricks profile has no copilot equivalent and must not produce a token.
    env = _build_copilot_spawn_env(_make_spec(auth=DatabricksAuth(profile="oss")))
    assert "HARNESS_COPILOT_GITHUB_TOKEN" not in env


def test_no_auth_falls_back_to_ambient_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "gho_ambient")
    env = _build_copilot_spawn_env(_make_spec(auth=None))
    assert env["HARNESS_COPILOT_GITHUB_TOKEN"] == "gho_ambient"


def test_no_auth_prefers_copilot_specific_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # COPILOT_GITHUB_TOKEN wins over GH_TOKEN (the CLI/SDK precedence order).
    monkeypatch.setenv("GH_TOKEN", "gho_gh")
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_copilot")
    env = _build_copilot_spawn_env(_make_spec(auth=None))
    assert env["HARNESS_COPILOT_GITHUB_TOKEN"] == "gho_copilot"


def test_no_auth_prefers_stored_block_over_ambient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"copilot": {"github_token_ref": "env:STORED_COPILOT"}})
    )
    monkeypatch.setenv("STORED_COPILOT", "gho_stored")
    monkeypatch.setenv("GH_TOKEN", "gho_ambient")
    env = _build_copilot_spawn_env(_make_spec(auth=None))
    assert env["HARNESS_COPILOT_GITHUB_TOKEN"] == "gho_stored"


def test_bundle_dir_threaded(tmp_path: Path) -> None:
    env = _build_copilot_spawn_env(_make_spec(), workdir=tmp_path)
    assert env["HARNESS_COPILOT_BUNDLE_DIR"] == str(tmp_path)
