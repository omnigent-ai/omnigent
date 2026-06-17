"""Structural test for the Debby two-headed brainstorming bundle (examples/debby).

Debby never answers from a single model: every question is fanned out to BOTH a
Claude sub-agent and a GPT sub-agent — two plain (non-coding) responders on the
claude-sdk and openai-agents harnesses — and the ``debate`` skill has them
critique each other before converging. Pure spec-load — no LLM, no credentials —
modeled on ``test_example_polly.py``.

What breaks if this fails:
- the two heads collapse onto one vendor (no cross-model contrast — Debby's whole
  point), or a head is dropped entirely,
- a head silently switches harness (e.g. the GPT head ends up on claude-sdk),
- the ``debate`` skill is dropped or renamed (the critique loop regresses),
- the ``os_env`` block disappears (the heads lose the file/shell tools the
  brainstorming surface relies on).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_debby.py -> repo root is 3 parents up.
_DEBBY_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "debby"


@pytest.fixture(scope="module")
def debby_spec() -> AgentSpec:
    """Load and validate the debby bundle once for the module."""
    return load(_DEBBY_BUNDLE)


def test_debby_is_two_headed_cross_vendor(debby_spec: AgentSpec) -> None:
    """
    Debby has exactly two heads — ``claude`` on claude-sdk and ``gpt`` on
    openai-agents — so every answer contrasts two distinct vendors.

    A missing/renamed head, or both heads landing on the same harness, removes
    the cross-model contrast that is Debby's entire reason to exist.
    """
    assert debby_spec.name == "debby"
    fam = {a.name: a.executor.config.get("harness") for a in debby_spec.sub_agents}
    assert sorted(debby_spec.tools.agents) == ["claude", "gpt"]
    assert fam["claude"] == "claude-sdk"
    assert fam["gpt"] == "openai-agents"
    # Two distinct vendors → the heads always disagree across providers.
    assert len(set(fam.values())) == 2


def test_debby_heads_are_unpinned(debby_spec: AgentSpec) -> None:
    """
    Neither head pins a model: each inherits whatever Claude / OpenAI provider
    the user configured (Anthropic key, subscription, gateway, or Databricks).

    Un-pinning is load-bearing for OSS — a Databricks-specific model id would
    404 on a plain Anthropic / OpenAI key. Re-introducing a pin re-couples a
    head to one provider, so fail here if a model reappears.
    """
    by_name = {a.name: a for a in debby_spec.sub_agents}
    for name in ("claude", "gpt"):
        assert by_name[name].executor.model is None, name
        assert by_name[name].executor.profile is None, name


def test_debby_debate_skill_present(debby_spec: AgentSpec) -> None:
    """The ``debate`` skill is discovered from skills/debate/SKILL.md."""
    assert sorted(s.name for s in debby_spec.skills) == ["debate"]


def test_debby_has_os_env(debby_spec: AgentSpec) -> None:
    """
    Debby carries an ``os_env`` block so the bridged ``sys_os_*`` tools register
    for the brainstorming surface. The shipped sandbox is ``type: none`` so the
    bundle loads on macOS too. Dropping ``os_env`` would leave the heads with no
    file/shell tools at all.
    """
    assert debby_spec.os_env is not None
    assert debby_spec.os_env.type == "caller_process"
    assert debby_spec.os_env.sandbox is not None
    assert debby_spec.os_env.sandbox.type == "none"
