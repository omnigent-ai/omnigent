"""
Tests for ``_build_pi_spawn_env`` in ``omnigent/runtime/workflow.py``.

The spawn-env builder maps ``spec.executor`` fields to ``HARNESS_PI_*``
env vars that the pi harness wrap reads at executor-construction time.
Mirrors ``test_claude_sdk_spawn_env.py`` — pi must have the same
Databricks-gateway default-model parity that claude-sdk has.

This is a unit test — no subprocess spawn, no real pi CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runtime.workflow import _build_pi_spawn_env
from omnigent.spec.types import AgentSpec, ExecutorSpec, LLMConfig


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Point OMNIGENT_CONFIG_HOME at an empty temp dir for every test in
    this file so the developer's real ``~/.omnigent/config.yaml`` (e.g.
    a default provider) cannot hijack the legacy-profile path under test.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for the isolated config.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))


def _make_spec(*, model: str | None = None, profile: str | None = None) -> AgentSpec:
    """
    Build a minimal pi :class:`AgentSpec` for spawn-env tests.

    :param model: Model identifier threaded into executor config and
        ``spec.llm``, e.g. ``"databricks-claude-sonnet-4-6"``. ``None``
        omits it (no model pinned in YAML — the nessie shape).
    :param profile: Legacy profile set via ``executor.config["profile"]``.
        ``None`` omits it (no profile declared in YAML).
    :returns: A populated :class:`AgentSpec`.
    """
    config: dict[str, object] = {"harness": "pi"}
    if model is not None:
        config["model"] = model
    if profile is not None:
        config["profile"] = profile
    return AgentSpec(
        spec_version=1,
        name="test-pi",
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnigent", config=config, model=model),
        llm=LLMConfig(model=model) if model is not None else None,
    )


def test_pi_spawn_env_threads_cwd_separately_from_bundle_dir(tmp_path: Path) -> None:
    """
    Pi gets the session workspace as ``HARNESS_PI_CWD``.

    ``workdir`` is the extracted agent bundle, not the user's project
    workspace. If these are conflated, Pi launches in the wrong repository.
    """
    workspace = tmp_path / "repo"
    workspace.mkdir()
    bundle_dir = tmp_path / "runner-specs" / "ag_pi-v1"
    bundle_dir.mkdir(parents=True)

    env = _build_pi_spawn_env(_make_spec(), cwd=workspace, workdir=bundle_dir)

    assert env["HARNESS_PI_CWD"] == str(workspace)
    assert env["HARNESS_PI_BUNDLE_DIR"] == str(bundle_dir)


def _ucode_state_for_pi(
    monkeypatch: pytest.MonkeyPatch, *, model: str | None, with_pi_entry: bool
):
    """
    Mock ucode resolution to a workspace state with or without a pi agent.

    Builds a workspace state whose ``pi`` agent carries gateway URLs +
    auth command but ``model=model``, then monkeypatches the workflow
    module's ucode lookups to return it.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param model: Per-agent ucode model, e.g. ``None`` to simulate a
        workspace that caches no model, or ``"databricks-claude-sonnet-4-6"``.
    :param with_pi_entry: ``False`` builds a state with no ``pi`` agent
        entry at all, exercising the early-return in
        ``configure_agent_harness_with_ucode``.
    """
    from omnigent.onboarding.ucode_state import UcodeAgentState, UcodeWorkspaceState

    agents = (
        {
            "pi": UcodeAgentState(
                model=model,
                base_urls={
                    "claude": "https://example.databricks.com/ai-gateway/anthropic",
                    "openai": "https://example.databricks.com/ai-gateway/codex/v1",
                },
                auth_command="printf token",
            )
        }
        if with_pi_entry
        else {}
    )
    state = UcodeWorkspaceState(
        workspace_url="https://example.databricks.com",
        agents=agents,
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

    Reproduces the nessie failure shape on pi: a profile-backed pi agent
    with no spec model, whose workspace ucode state caches gateway URLs but
    no model. Without the producer default pi falls back to its own host
    default (an Anthropic-direct id the gateway rejects), so the model env
    var must be set to a routable ``databricks-*`` endpoint name.
    """
    _ucode_state_for_pi(monkeypatch, model=None, with_pi_entry=True)

    spec = _make_spec(model=None, profile="oss")
    env = _build_pi_spawn_env(spec, workdir=None)

    assert env["HARNESS_PI_GATEWAY"] == "true"
    # The verified routable gateway endpoint name, not pi's own default.
    assert env["HARNESS_PI_MODEL"] == "databricks-claude-opus-4-8"


def test_ucode_state_with_model_is_not_overridden_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ucode-supplied model is used as-is; the default does not clobber it.

    Failure means the producer's missing-model fallback would override a
    workspace that correctly caches its own model.
    """
    _ucode_state_for_pi(monkeypatch, model="databricks-claude-sonnet-4-6", with_pi_entry=True)

    spec = _make_spec(model=None, profile="oss")
    env = _build_pi_spawn_env(spec, workdir=None)

    assert env["HARNESS_PI_MODEL"] == "databricks-claude-sonnet-4-6"


def test_spec_model_wins_over_ucode_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A spec-pinned model takes precedence over both ucode and the default.

    Failure means the ucode/default plumbing clobbers an explicit
    ``executor.model`` from the agent YAML.
    """
    _ucode_state_for_pi(monkeypatch, model=None, with_pi_entry=True)

    spec = _make_spec(model="databricks-gpt-5-4", profile="oss")
    env = _build_pi_spawn_env(spec, workdir=None)

    assert env["HARNESS_PI_MODEL"] == "databricks-gpt-5-4"


def test_no_ucode_pi_entry_leaves_model_to_executor_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Without a ucode ``pi`` entry the producer sets no model env var.

    ``configure_agent_harness_with_ucode`` early-returns before its
    default-model fallback when the workspace state has no ``pi`` agent.
    The spawn env must still enable the gateway + carry the profile so
    the executor's own profile-derived Databricks default (see
    ``PiExecutor._resolve_model``) covers this path — asserting the model
    var is absent proves that executor-side fallback is actually reached.
    """
    _ucode_state_for_pi(monkeypatch, model=None, with_pi_entry=False)

    spec = _make_spec(model=None, profile="oss")
    env = _build_pi_spawn_env(spec, workdir=None)

    assert env["HARNESS_PI_GATEWAY"] == "true"
    assert env["HARNESS_PI_DATABRICKS_PROFILE"] == "oss"
    # No producer model — the executor's profile-path default applies.
    assert "HARNESS_PI_MODEL" not in env
