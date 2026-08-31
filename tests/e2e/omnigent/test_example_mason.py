"""Structural coverage for the Mason orchestrator bundle."""
import re
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


def test_mason_spec_and_roster(mason: AgentSpec) -> None:
    assert mason.name == "mason"
    assert set(mason.tools.agents) == set(_WORKERS)
    discovered = {agent.name: agent for agent in mason.sub_agents}
    assert set(discovered) == set(_WORKERS)
    assert {
        name: agent.executor.config.get("harness")
        for name, agent in discovered.items()
    } == _WORKERS


def test_mason_uses_one_final_pull_request(mason: AgentSpec) -> None:
    instructions = " ".join(mason.instructions.split())
    assert "integration branch" in instructions
    assert (
        "git worktree add -b <tracking-branch> <integration-path> <base-branch>"
        in instructions
    )
    assert (
        "git worktree add -b <work-item-branch> <path> <tracking-branch>"
        in instructions
    )
    assert "git -C <integration-path> merge --no-ff <work-item-branch>" in instructions
    assert "git -C <integration-path> merge --abort" in instructions
    assert "re-dispatch the affected" in instructions
    assert "gh pr create --base <base-branch> --head <tracking-branch>" in instructions
    assert "exactly ONE pull request" in instructions
    assert "single PR is ready for approval and merge" in instructions


def test_mason_requires_explicit_plan_approval(mason: AgentSpec) -> None:
    instructions = " ".join(mason.instructions.split())
    assert "PLAN APPROVAL GATE (hard stop)" in instructions
    assert "Then STOP and end the turn" in instructions
    assert "explicit affirmative approval" in instructions
    assert "does NOT create" in instructions
    assert "Human questions, corrections, or requested changes mean revise" in instructions
    assert "This human-driven plan loop has no bound" in instructions
    assert "Never infer approval from silence" in instructions
    assert "only read-only `explore` or `search` workers" in instructions


def test_mason_dispatch_and_integration_guardrails(mason: AgentSpec) -> None:
    config = (_BUNDLE / "config.yaml").read_text()
    assert "args.purpose" in mason.instructions
    assert "task-based `title`" in mason.instructions
    assert "same agent and same title continues" in mason.instructions
    assert "SAME turn" in mason.instructions
    assert "dispatches are already in flight" in mason.instructions
    assert mason.guardrails is not None
    assert mason.guardrails.policies is not None
    assert "blast_radius" in {policy.name for policy in mason.guardrails.policies}
    assert "dispatch_tools: [sys_session_send]" in config
    assert "sys_session_create" not in config


def test_mason_codex_pins_are_load_bearing(mason: AgentSpec) -> None:
    codex = next(agent for agent in mason.sub_agents if agent.name == "codex")
    assert codex.executor.model == "gpt-5.6-luna"
    assert codex.executor.reasoning_effort == "high"
    sol = next(agent for agent in mason.sub_agents if agent.name == "codex_sol")
    assert sol.executor.model == "gpt-5.6-sol"
    assert sol.executor.reasoning_effort == "high"


def test_mason_has_exactly_two_skills(mason: AgentSpec) -> None:
    assert mason.os_env is not None and mason.os_env.sandbox.type == "none"
    assert mason.terminals and "shell" in mason.terminals
    assert mason.async_enabled and mason.timers
    assert sorted(skill.name for skill in mason.skills) == [
        "cross-review", "rf-repo",
    ]
    assert all(skill.content and skill.skill_dir.name == skill.name
               for skill in mason.skills)


def test_every_child_keeps_delivery_boundaries(mason: AgentSpec) -> None:
    child_configs = (_BUNDLE / "agents").glob("*/config.yaml")
    assert all("gh pr create" not in path.read_text() for path in child_configs)
    for worker in mason.sub_agents:
        assert "DISCOVERED — OUT OF SCOPE" in worker.instructions
        assert "NEVER merge a PR" in worker.instructions


def test_mason_bundle_has_no_polly_or_deleted_audit_concepts() -> None:
    files = [path for path in _BUNDLE.rglob("*") if path.is_file()]
    bundle = "\n".join(path.read_text() for path in files)
    assert "polly" not in bundle.lower()
    for deleted_concept in ("claim-ledger", "scope_ledger", "~/.mason"):
        assert deleted_concept not in bundle
    assert re.search(r"D-\d+", bundle) is None
