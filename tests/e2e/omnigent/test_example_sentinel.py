"""Structural test for the Sentinel security-review bundle (examples/sentinel).

Sentinel is a report-only security-review orchestrator: it collects audit scope,
delegates read-only code investigation to a ``scanner`` sub-agent (claude-sdk),
and can route the draft through an independent ``reviewer`` sub-agent (codex).
Pure spec-load — no LLM, no credentials — modeled on ``test_example_scribe.py``.

What breaks if this fails:
- a sub-agent is dropped or renamed (Sentinel loses scanning or fact-checking),
- the reviewer collapses onto claude-sdk (the cross-vendor check stops being
  independent),
- a sub-agent silently pins a model (re-coupling it to one provider),
- the ``security-audit`` skill is dropped or renamed,
- the report-only purpose guard starts allowing ``implement`` dispatches,
- the ``read_only_os`` write-deny policy is dropped from the orchestrator or a
  sub-agent (report-only stops being enforced at the policy layer).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_sentinel.py -> repo root is 3 parents up.
_SENTINEL_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "sentinel"


@pytest.fixture(scope="module")
def sentinel_spec() -> AgentSpec:
    """Load and validate the sentinel bundle once for the module."""
    return load(_SENTINEL_BUNDLE)


def test_sentinel_name_and_subagents(sentinel_spec: AgentSpec) -> None:  # DoC-1, DoC-2
    assert sentinel_spec.name == "sentinel"
    fam = {a.name: a.executor.config.get("harness") for a in sentinel_spec.sub_agents}
    assert sorted(sentinel_spec.tools.agents) == ["reviewer", "scanner"]
    assert fam["scanner"] == "claude-sdk"
    assert fam["reviewer"] == "codex"
    assert fam["scanner"] != fam["reviewer"]


def test_sentinel_sub_agents_are_unpinned(sentinel_spec: AgentSpec) -> None:  # DoC-3
    by_name = {a.name: a for a in sentinel_spec.sub_agents}
    for name in ("scanner", "reviewer"):
        assert by_name[name].executor.model is None, name
        assert by_name[name].executor.profile is None, name


def test_sentinel_skills_present(sentinel_spec: AgentSpec) -> None:  # DoC-4
    assert sorted(s.name for s in sentinel_spec.skills) == ["security-audit"]


def test_sentinel_has_os_env(sentinel_spec: AgentSpec) -> None:  # DoC-9
    assert sentinel_spec.os_env is not None
    assert sentinel_spec.os_env.type == "caller_process"
    # Sandboxed by default: the config omits ``sandbox`` so the runtime resolves
    # the platform backend (linux_bwrap / darwin_seatbelt, binding cwd
    # read-only). An unset spec-level sandbox means "use the platform default",
    # NOT unsandboxed -- ``type: none`` is the explicit opt-out, which Sentinel
    # deliberately does not use because it reviews untrusted code.
    assert sentinel_spec.os_env.sandbox is None


def test_sentinel_orchestrator_guardrails(sentinel_spec: AgentSpec) -> None:  # DoC-5
    names = {p.name for p in sentinel_spec.guardrails.policies}
    assert {"blast_radius", "read_only_os", "headless_subagent_purpose_guard"} <= names
    guard = next(
        p for p in sentinel_spec.guardrails.policies if p.name == "headless_subagent_purpose_guard"
    )
    allowed = guard.function.arguments["allowed_purposes"]
    assert "implement" not in allowed
    assert sorted(allowed) == ["explore", "review", "search"]


def test_sentinel_policy_paths_importable(sentinel_spec: AgentSpec) -> None:  # DoC-6
    for agent in (sentinel_spec, *sentinel_spec.sub_agents):
        for policy in agent.guardrails.policies:
            mod, _, fn = policy.function.path.rpartition(".")
            assert hasattr(importlib.import_module(mod), fn), policy.function.path


def test_sentinel_function_policies_have_nonempty_arguments(
    sentinel_spec: AgentSpec,
) -> None:  # DoC-7
    # ``read_only_os`` is intentionally argument-free (its deny set is fixed), so
    # it is exempt; every other policy must carry the arguments it needs.
    for agent in (sentinel_spec, *sentinel_spec.sub_agents):
        for policy in agent.guardrails.policies:
            if policy.name == "read_only_os":
                assert policy.function.arguments is None, (agent, policy.name)
                continue
            assert policy.function.arguments, (agent, policy.name)


def test_sentinel_subagent_guardrails(sentinel_spec: AgentSpec) -> None:  # DoC-8
    for agent in sentinel_spec.sub_agents:
        names = {p.name for p in agent.guardrails.policies}
        assert {"blast_radius", "read_only_os"} <= names, agent.name


def test_sentinel_findings_template_present(sentinel_spec: AgentSpec) -> None:  # DoC-10
    tokens = ["Critical", "High", "Medium", "Low", "Info"]
    prompt = sentinel_spec.instructions
    skill = (_SENTINEL_BUNDLE / "skills" / "security-audit" / "SKILL.md").read_text("utf-8")
    for token in tokens:
        assert token in prompt, ("prompt", token)
        assert token in skill, ("skill", token)
