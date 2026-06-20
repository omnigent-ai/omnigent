"""
Tests for the opencode harness spawn-env builder in
``omnigent/runtime/workflow.py``.

Covers the minimal contracts:
- ``HARNESS_OPENCODE_MODEL`` and ``HARNESS_OPENCODE_CWD`` from spec model +
  workdir.
- ``HARNESS_OPENCODE_GATEWAY_API_KEY`` from an explicit ``executor.auth``
  ``api_key`` — the shortest end-to-end proof that the auth branch fires.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runtime.workflow import _build_opencode_spawn_env
from omnigent.spec.types import (
    AgentSpec,
    ApiKeyAuth,
    ExecutorSpec,
    LLMConfig,
)


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Point OMNIGENT_CONFIG_HOME at an empty temp dir so ambient global
    config (auth:, providers:) cannot leak into the builder's resolution.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DATABRICKS_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def _spec(
    *,
    model: str | None = None,
    auth: ApiKeyAuth | None = None,
) -> AgentSpec:
    """
    Build a minimal AgentSpec for the opencode harness.

    :param model: Optional model string, e.g. ``"anthropic/claude-sonnet-4-5"``.
    :param auth: Optional executor auth.
    :returns: A populated AgentSpec.
    """
    config: dict[str, object] = {"harness": "opencode"}
    if model is not None:
        config["model"] = model
    return AgentSpec(
        spec_version=1,
        name="oc",
        instructions="hi",
        executor=ExecutorSpec(type="omnigent", config=config, model=model, auth=auth),
        llm=LLMConfig(model=model) if model is not None else None,
    )


def test_model_and_cwd(tmp_path: Path) -> None:
    """HARNESS_OPENCODE_MODEL and HARNESS_OPENCODE_CWD are set from spec."""
    env = _build_opencode_spawn_env(
        _spec(model="anthropic/claude-sonnet-4-5"),
        workdir=tmp_path,
    )
    assert env["HARNESS_OPENCODE_MODEL"] == "anthropic/claude-sonnet-4-5"
    assert env["HARNESS_OPENCODE_CWD"] == str(tmp_path)


def test_api_key_auth_bakes_gateway_key() -> None:
    """An explicit api_key auth sets HARNESS_OPENCODE_GATEWAY_API_KEY."""
    env = _build_opencode_spawn_env(
        _spec(auth=ApiKeyAuth(api_key="sk-xyz")),
    )
    assert env["HARNESS_OPENCODE_GATEWAY_API_KEY"] == "sk-xyz"
