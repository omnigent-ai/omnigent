"""
Tests for ``_build_claude_sdk_spawn_env`` in
``omnigent/runtime/workflow.py``.

The spawn-env builder maps ``spec.executor`` fields to
``HARNESS_CLAUDE_SDK_*`` env vars that the claude-sdk harness wrap reads
at executor-construction time.  Mirrors the pattern of
``test_openai_agents_sdk_spawn_env.py`` for the openai-agents harness.

This is a unit test — no subprocess spawn, no real claude CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml as _yaml

from omnigent.runtime.workflow import _build_claude_sdk_spawn_env
from omnigent.spec.types import (
    AgentSpec,
    ApiKeyAuth,
    DatabricksAuth,
    ExecutorSpec,
    LLMConfig,
)


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Point OMNIGENT_CONFIG_HOME at an empty temp dir for every test in
    this file so tests that don't explicitly set up a global config are
    not affected by the developer's real ``~/.omnigent/config.yaml``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for the isolated config.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))


def _make_spec(
    *,
    model: str | None = "databricks-claude-sonnet-4-6",
    profile: str | None = None,
    auth: ApiKeyAuth | DatabricksAuth | None = None,
) -> AgentSpec:
    """
    Build a minimal claude-sdk :class:`AgentSpec` for spawn-env tests.

    :param model: Model identifier threaded into executor config and
        ``spec.llm``, e.g. ``"databricks-claude-sonnet-4-6"``.
    :param profile: Legacy profile set via ``executor.config["profile"]``.
        ``None`` omits it (no profile declared in YAML).
    :param auth: Typed auth object placed on ``spec.executor.auth``.
        ``None`` omits it (harness falls back to legacy / global config).
    :returns: A populated :class:`AgentSpec`.
    """
    config: dict[str, object] = {"harness": "claude-sdk"}
    if model is not None:
        config["model"] = model
    if profile is not None:
        config["profile"] = profile
    return AgentSpec(
        spec_version=1,
        name="test-claude-sdk",
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnigent", config=config, model=model, auth=auth),
        llm=LLMConfig(model=model) if model is not None else None,
    )


def test_databricks_auth_sets_databricks_env_vars() -> None:
    """
    ``executor.auth: {type: databricks, profile: …}`` sets
    ``HARNESS_CLAUDE_SDK_GATEWAY=true`` and
    ``HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE``.

    Failure means a spec that explicitly declares Databricks auth still
    gets routed to api.anthropic.com and fails with "model not found".
    """
    spec = _make_spec(auth=DatabricksAuth(profile="my-profile"))
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_GATEWAY"] == "true"
    assert env["HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE"] == "my-profile"


def test_api_key_auth_sets_helper_env_var() -> None:
    """
    ``executor.auth: {type: api_key, api_key: …}`` sets
    ``HARNESS_CLAUDE_SDK_API_KEY_HELPER`` to a printf shell command.

    Failure means the API key never reaches the Claude CLI's
    ``settings.apiKeyHelper`` and the agent falls back to subscription
    auth silently.
    """
    spec = _make_spec(model=None, auth=ApiKeyAuth(api_key="sk-ant-test-123"))
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert "HARNESS_CLAUDE_SDK_API_KEY_HELPER" in env
    # The helper command must echo the literal key (shlex-quoted for safety).
    assert "sk-ant-test-123" in env["HARNESS_CLAUDE_SDK_API_KEY_HELPER"]
    # api_key auth does not trigger Databricks routing.
    assert "HARNESS_CLAUDE_SDK_GATEWAY" not in env


def test_api_key_auth_with_special_chars_is_shell_safe() -> None:
    """
    API keys containing shell-special characters (spaces, quotes, ``$``)
    are safely quoted in the helper command via ``shlex.quote``.

    Failure means a key like ``sk-$weird`` could be misinterpreted by
    the shell when the Claude CLI invokes the helper command.
    """
    spec = _make_spec(model=None, auth=ApiKeyAuth(api_key="sk-$weird 'key'"))
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    helper = env["HARNESS_CLAUDE_SDK_API_KEY_HELPER"]
    # The raw key must NOT appear unquoted.
    assert "sk-$weird 'key'" not in helper
    # shlex-quoted form must be present.
    assert "sk-" in helper


def test_global_config_databricks_auth_applied_when_spec_has_no_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    When the spec declares no auth, ``_load_global_auth()`` is consulted
    and a global ``auth: {type: databricks, profile: …}`` is applied.

    Failure means ``omnigent setup`` auth configuration is silently
    ignored for claude-sdk agents (it was applied to openai-agents but
    not claude-sdk before this fix).
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_yaml.dump({"auth": {"type": "databricks", "profile": "global-profile"}}))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))

    spec = _make_spec(auth=None, profile=None)
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env.get("HARNESS_CLAUDE_SDK_GATEWAY") == "true"
    assert env.get("HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE") == "global-profile"


def test_global_config_not_applied_when_spec_has_legacy_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    When the spec uses a legacy ``executor.config["profile"]``, the global
    config ``auth:`` block is not applied — spec-level auth always wins.

    Failure means a YAML with ``executor.profile: oss`` gets silently
    overridden by the user's global api_key config.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_yaml.dump({"auth": {"type": "api_key", "api_key": "sk-global"}}))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))

    spec = _make_spec(auth=None, profile="oss-from-spec")
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    # Legacy profile must be used; global api_key must not interfere.
    assert env.get("HARNESS_CLAUDE_SDK_GATEWAY") == "true"
    assert env.get("HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE") == "oss-from-spec"
    assert "HARNESS_CLAUDE_SDK_API_KEY_HELPER" not in env


def _ucode_state_without_model(monkeypatch: pytest.MonkeyPatch, *, model: str | None):
    """
    Mock ucode resolution to a claude agent with the given model.

    Builds a workspace state whose ``claude`` agent carries a gateway URL +
    auth command but ``model=model`` and no ``claude_models`` tiers, then
    monkeypatches the workflow module's ucode lookups to return it.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param model: Per-agent ucode model, e.g. ``None`` to simulate a
        workspace that caches no model, or ``"databricks-claude-sonnet-4-6"``.
    """
    from omnigent.onboarding.ucode_state import UcodeAgentState, UcodeWorkspaceState

    state = UcodeWorkspaceState(
        workspace_url="https://example.databricks.com",
        claude_models={},
        agents={
            "claude": UcodeAgentState(
                model=model,
                base_url="https://example.databricks.com/ai-gateway/anthropic",
                auth_command="printf token",
            )
        },
    )
    monkeypatch.setattr(
        "omnigent.runtime.workflow.get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.runtime.workflow.read_ucode_state",
        lambda workspace_url: state,
    )


def test_ucode_state_without_model_falls_back_to_databricks_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A modelless ucode state resolves the Databricks gateway default model.

    Reproduces the nessie failure: a profile-backed claude-sdk agent with no
    spec model, whose workspace ucode state caches a gateway URL but no model.
    Without the producer default the CLI falls back to its host-config model
    (an Anthropic-direct id the gateway rejects), so the model env var must be
    set to a routable ``databricks-*`` endpoint name.
    """
    _ucode_state_without_model(monkeypatch, model=None)

    spec = _make_spec(model=None, profile="oss")
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_GATEWAY"] == "true"
    # The verified routable gateway endpoint name, not the CLI's own default.
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "databricks-claude-opus-4-8"


def test_ucode_state_with_model_is_not_overridden_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ucode-supplied model is used as-is; the default does not clobber it.

    Failure means the producer's missing-model fallback would override a
    workspace that correctly caches its own model.
    """
    _ucode_state_without_model(monkeypatch, model="databricks-claude-sonnet-4-6")

    spec = _make_spec(model=None, profile="oss")
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "databricks-claude-sonnet-4-6"


def _write_databricks_models_config(tmp_path: Path, *, models: dict[str, str]) -> None:
    """
    Write a ``providers:`` config with a databricks entry carrying *models*.

    The autouse ``_isolate_global_config`` fixture already points
    ``$OMNIGENT_CONFIG_HOME`` at *tmp_path*, so this exercises the real
    config-loading path behind ``databricks_provider_models``.

    :param tmp_path: The per-test config home.
    :param models: The tier→model overrides, e.g.
        ``{"default": "main.agents.my-opus-endpoint"}``.
    """
    (tmp_path / "config.yaml").write_text(
        _yaml.safe_dump(
            {
                "providers": {
                    "databricks": {"kind": "databricks", "profile": "oss", "models": models}
                }
            }
        )
    )


def test_provider_models_default_beats_ucode_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    The provider entry's ``models.default`` outranks the ucode-cached model.

    The user picked an explicit endpoint on the ``kind: databricks``
    provider in ``~/.omnigent/config.yaml``; the ucode state cache must
    not shadow it (config > ucode > hardcoded default).
    """
    _ucode_state_without_model(monkeypatch, model="databricks-claude-sonnet-4-6")
    _write_databricks_models_config(tmp_path, models={"default": "main.agents.my-opus-endpoint"})

    spec = _make_spec(model=None, profile="oss")
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "main.agents.my-opus-endpoint"


def test_provider_models_without_default_keeps_ucode_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A ``models:`` map with no ``default`` tier leaves the ucode model alone.

    Only the ``default`` tier feeds the single claude-sdk model env var;
    a per-tier-only map (e.g. just ``opus:``) must not clobber the
    ucode-resolved launch model.
    """
    _ucode_state_without_model(monkeypatch, model="databricks-claude-sonnet-4-6")
    _write_databricks_models_config(tmp_path, models={"opus": "main.agents.my-opus-endpoint"})

    spec = _make_spec(model=None, profile="oss")
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "databricks-claude-sonnet-4-6"


def test_spec_model_beats_provider_models_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A spec-pinned model still wins over the provider ``models:`` override.

    Failure means the config-level override clobbers an explicit
    ``executor.model`` from the agent YAML — spec must stay strongest.
    """
    _ucode_state_without_model(monkeypatch, model=None)
    _write_databricks_models_config(tmp_path, models={"default": "main.agents.my-opus-endpoint"})

    spec = _make_spec(model="databricks-claude-opus-4-8", profile="oss")
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "databricks-claude-opus-4-8"


def test_provider_models_apply_without_ucode_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    The override lands even when the profile has no ucode state at all.

    On the profile-derived gateway path (no ``~/.ucode/state.json`` entry)
    ``configure_agent_harness_with_ucode`` early-returns before its ucode
    lookups — the provider override must be applied before those checks or
    a configured endpoint would silently fall back to the executor's
    hardcoded Databricks default.
    """
    monkeypatch.setattr(
        "omnigent.runtime.workflow.get_workspace_url_for_profile",
        lambda profile: None,
    )
    _write_databricks_models_config(tmp_path, models={"default": "main.agents.my-opus-endpoint"})

    spec = _make_spec(model=None, profile="oss")
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_GATEWAY"] == "true"
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "main.agents.my-opus-endpoint"


def test_provider_models_1m_suffix_kept_for_claude_sdk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ``[1m]`` suffix passes through to the claude-sdk spawn env.

    The claude-sdk harness drives the claude CLI, which understands the
    suffix (1M-context accounting) and strips it before the API call — so
    unlike pi, the producer must NOT strip it here.
    """
    _ucode_state_without_model(monkeypatch, model=None)
    _write_databricks_models_config(
        tmp_path, models={"default": "main.agents.my-opus-endpoint[1m]"}
    )

    spec = _make_spec(model=None, profile="oss")
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "main.agents.my-opus-endpoint[1m]"
