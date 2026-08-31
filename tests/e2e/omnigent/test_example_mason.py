"""Structural coverage for the Mason orchestrator bundle."""
from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "mason"
_WORKERS = {
    "claude_code": "claude-native", "codex": "codex-native",
    "opencode": "opencode-native", "cursor": "cursor-native",
    "codex_sol": "codex-native", "hermes": "hermes-native",
    "agy": "antigravity-native", "pi": "pi",
}


@pytest.fixture(scope="module")
def mason() -> AgentSpec:
    return load(_BUNDLE)


def test_mason_roster_resolves_and_is_cross_vendor(mason: AgentSpec) -> None:
    assert mason.name == "mason"
    assert set(mason.tools.agents) == set(_WORKERS)
    discovered = {a.name: a for a in mason.sub_agents}
    assert set(discovered) == set(_WORKERS)
    assert {
        name: agent.executor.config.get("harness")
        for name, agent in discovered.items()
    } == _WORKERS


def test_mason_codex_defaults_are_load_bearing(mason: AgentSpec) -> None:
    codex = next(a for a in mason.sub_agents if a.name == "codex")
    assert codex.executor.model == "gpt-5.6-luna"
    assert codex.executor.reasoning_effort == "high"
    sol = next(a for a in mason.sub_agents if a.name == "codex_sol")
    assert sol.executor.model == "gpt-5.6-sol"
    assert sol.executor.reasoning_effort == "high"
    assert "NET-SCOPE RULE" in sol.instructions


def test_mason_workflow_grants_and_skills(mason: AgentSpec) -> None:
    assert mason.os_env is not None and mason.os_env.sandbox.type == "none"
    assert mason.terminals and "shell" in mason.terminals
    assert mason.async_enabled and mason.timers
    assert sorted(skill.name for skill in mason.skills) == [
        "audit-rubric", "claim-ledger", "cross-review", "repair-loop",
        "rf-fix-pass", "scope-discipline", "work-package-planning",
    ]
    assert all(skill.content and skill.skill_dir.name == skill.name for skill in mason.skills)


def test_mason_has_no_polly_leak() -> None:
    assert not any(
        "polly" in path.read_text().lower()
        for path in _BUNDLE.rglob("*")
        if path.is_file()
    )


def test_mason_scope_and_delivery_boundaries(mason: AgentSpec) -> None:
    assert "Scope may SHRINK freely. Scope may GROW" in mason.instructions
    assert "being pointed at a branch is not authorisation to repair it" in mason.instructions
    assert "~/.mason/<task_id>/" in mason.instructions
    planning = next(skill for skill in mason.skills if skill.name == "work-package-planning")
    assert "top 10 blocking" in planning.content
    for worker in mason.sub_agents:
        assert "DISCOVERED — OUT OF SCOPE" in worker.instructions
