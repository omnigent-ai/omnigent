"""Structural test for the SWE-org engineering-director example
(``tests/resources/examples/swe_org.yaml``).

A single-file multi-agent demo in the spirit of ``coding_supervisor.yaml``: a
director root delegates to a team of role sub-agents that run on *different*
models for different jobs (Claude for backend / QA / staff-review, GPT for
frontend / design), all able to inspect the repo through inherited ``os_env``
tools, with two function-policy guardrails. Pure spec-load — no LLM, no
credentials — modeled on ``test_example_polly.py``.

What breaks if this fails:
- a team role is dropped/renamed (the org loses a function),
- the deliberate cross-model split collapses onto one vendor (the demo's
  "different models for different jobs" point),
- the ``os_env`` block disappears (agents can no longer inspect/edit the repo),
- a guardrail (per-turn tool rate-limit or search budget) is removed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_swe_org.py -> repo root is 3 parents up.
_SWE_ORG_YAML = (
    Path(__file__).resolve().parents[3] / "tests" / "resources" / "examples" / "swe_org.yaml"
)


@pytest.fixture(scope="module")
def swe_org_spec() -> AgentSpec:
    """Load and validate the swe_org example once for the module."""
    return load(_SWE_ORG_YAML)


def test_director_root(swe_org_spec: AgentSpec) -> None:
    """
    The root is the engineering director on the openai-agents harness. Its name
    is intentionally ``swe_org_director`` (the file stem ``swe_org`` is the
    agent *identity* for coverage; the spec ``name`` is the in-product role).
    """
    assert swe_org_spec.name == "swe_org_director"
    assert swe_org_spec.executor.config.get("harness") == "openai-agents"


def test_team_roles_and_cross_model_split(swe_org_spec: AgentSpec) -> None:
    """
    Exactly five team roles, with a deliberate cross-model split: backend / QA /
    staff-reviewer on claude-sdk (Sonnet), frontend / product-designer on
    openai-agents (GPT). Collapsing every role onto one harness/model defeats
    the "different models for different jobs" point of the demo.
    """
    by_name = {a.name: a for a in swe_org_spec.sub_agents}
    assert sorted(swe_org_spec.tools.agents) == [
        "backend_engineer",
        "frontend_engineer",
        "product_designer",
        "qa_engineer",
        "staff_reviewer",
    ]
    claude_roles = {"backend_engineer", "qa_engineer", "staff_reviewer"}
    gpt_roles = {"frontend_engineer", "product_designer"}
    for name in claude_roles:
        assert by_name[name].executor.config.get("harness") == "claude-sdk", name
    for name in gpt_roles:
        assert by_name[name].executor.config.get("harness") == "openai-agents", name
    # Both vendors are represented — not a single-model org.
    harnesses = {a.executor.config.get("harness") for a in swe_org_spec.sub_agents}
    assert harnesses == {"claude-sdk", "openai-agents"}


def test_director_has_os_env(swe_org_spec: AgentSpec) -> None:
    """
    The director carries an ``os_env`` block so the inherited ``sys_os_*`` tools
    register and the org can inspect/edit the repo. Dropping it would leave the
    director claiming repo access it cannot exercise.
    """
    assert swe_org_spec.os_env is not None
    assert swe_org_spec.os_env.type == "caller_process"


def test_director_guardrails(swe_org_spec: AgentSpec) -> None:
    """
    The director carries both function-policy guardrails: a per-turn tool-call
    rate limit and a search budget. Each must keep non-empty ``function``
    wiring or the resolver would fail closed on the first gated call.
    """
    assert swe_org_spec.guardrails is not None
    by_name = {p.name: p for p in swe_org_spec.guardrails.policies}
    assert sorted(by_name) == ["rate_limit", "search_budget"]
    # The rate limit pins a concrete per-turn cap via factory kwargs.
    rate_args = by_name["rate_limit"].function.arguments
    assert rate_args["factory_kwargs"]["limit"] == 25
    # Each policy names a resolvable handler target.
    for name in ("rate_limit", "search_budget"):
        assert by_name[name].function.arguments.get("target")
