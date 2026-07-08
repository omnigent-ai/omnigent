"""Tests for ``_build_hermes_acp_spawn_env`` in ``omnigent/runtime/workflow.py``.

The builder maps spec.executor fields to the shared ``HARNESS_HERMES_*`` env
vars the hermes wraps read. Hermes owns its own auth, so no credential is
wired; a ``databricks-*`` model is dropped in favour of the agent's own model.

Unit test — no subprocess spawn. End-to-end verification of the wrap →
executor path lives in ``tests/inner/test_hermes_acp_executor.py``.
"""

from __future__ import annotations

from omnigent.runtime.workflow import _build_hermes_acp_spawn_env
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _make_spec(*, model: str | None = None) -> AgentSpec:
    config: dict[str, object] = {"harness": "hermes-acp"}
    if model is not None:
        config["model"] = model
    return AgentSpec(
        spec_version=1,
        name="test-hermes-acp",
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnigent", config=config, model=model),
    )


def test_model_from_spec_is_forwarded() -> None:
    env = _build_hermes_acp_spawn_env(_make_spec(model="hermes-4.3-large"))
    assert env["HARNESS_HERMES_MODEL"] == "hermes-4.3-large"


def test_no_model_forwards_nothing() -> None:
    env = _build_hermes_acp_spawn_env(_make_spec())
    assert "HARNESS_HERMES_MODEL" not in env


def test_databricks_model_is_dropped() -> None:
    env = _build_hermes_acp_spawn_env(_make_spec(model="databricks-claude-sonnet-4"))
    assert "HARNESS_HERMES_MODEL" not in env


def test_skills_filter_always_threaded() -> None:
    """The wrap defaults an absent skills var to "all", so the builder must
    always emit it -- a restricted spec must not launch with every skill."""
    import json

    spec = _make_spec()
    spec.skills_filter = "none"
    env = _build_hermes_acp_spawn_env(spec)
    assert json.loads(env["HARNESS_HERMES_SKILLS_FILTER"]) == "none"

    default_env = _build_hermes_acp_spawn_env(_make_spec())
    assert "HARNESS_HERMES_SKILLS_FILTER" in default_env


def test_dispatcher_routes_hermes_acp() -> None:
    """The runner's spec spawn-env dispatcher has a hermes-acp branch (a missing
    branch silently returns None and the wrap loses spec config)."""
    from omnigent.runner.app import _build_spawn_env_from_spec

    env = _build_spawn_env_from_spec(_make_spec(model="hermes-4.3-large"), "hermes-acp")
    assert env is not None
    assert env["HARNESS_HERMES_MODEL"] == "hermes-4.3-large"


def test_required_cli_mapping_covers_hermes_acp() -> None:
    """Missing-CLI preflight must know hermes-acp needs the hermes binary."""
    from omnigent.onboarding.harness_install import required_cli_for_harness

    spec = required_cli_for_harness("hermes-acp")
    assert spec is not None
    assert spec.binary == "hermes"


def test_model_override_wins() -> None:
    from omnigent.runner.app import _build_spawn_env_from_spec

    env = _build_spawn_env_from_spec(
        _make_spec(model="hermes-4.3-large"), "hermes-acp", model_override="hermes-4.3-mini"
    )
    assert env is not None
    assert env["HARNESS_HERMES_MODEL"] == "hermes-4.3-mini"
