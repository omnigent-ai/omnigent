"""Regression (#2426): the runner dispatch gate and the sub-agent spec
lookup must AGREE about web_fetch's synthesized ``__web_researcher``.

``WebFetchTool.__init__`` appends ``__web_researcher`` to the owner's live
``sub_agents`` in memory but never serializes it, so the runner's re-parsed
spec lacks it. An earlier fix admitted the researcher through the gate
(``_has_subagent``) alone, but ``_find_subagent_spec`` / ``_subagent_harness``
/ ``_subagent_allowed_harnesses`` still returned ``None`` -- the gate said the
sub-agent existed while every downstream resolver disagreed (the
CHANGES_REQUESTED objection on #2439). Routing them all through a single
resolver that reconstructs the researcher from its ``web_fetch`` owner makes
the answers consistent.
"""

from __future__ import annotations

import shutil

import pytest

from omnigent.runner.tool_dispatch import (
    _find_subagent_spec,
    _has_subagent,
    _subagent_allowed_harnesses,
    _subagent_harness,
)
from omnigent.spec.types import (
    AgentSpec,
    BuiltinToolConfig,
    ExecutorSpec,
    LLMConfig,
    ToolsConfig,
)
from omnigent.tools.builtins.web_fetch import RESEARCHER_NAME


@pytest.fixture(autouse=True)
def _default_sandbox_binary_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the seed-time sandbox probe host-independent (bwrap / sandbox-exec).

    ``build_researcher_spec`` probes the host PATH for a no-``os_env`` parent;
    these resolution tests must not depend on bubblewrap being installed.
    """
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/bin/{cmd}")


def _parent(*, web_fetch: bool = True, harness: str = "claude-sdk") -> AgentSpec:
    """A coordinator as re-parsed from its bundle -- researcher NOT persisted."""
    builtins = [BuiltinToolConfig(name="web_fetch")] if web_fetch else []
    return AgentSpec(
        spec_version=1,
        name="coordinator",
        llm=LLMConfig(model="openai/gpt-5.4"),
        executor=ExecutorSpec(max_iterations=40, config={"harness": harness}),
        tools=ToolsConfig(builtins=builtins),
    )


def test_gate_and_lookup_agree_for_researcher() -> None:
    """All resolvers give consistent answers for ``__web_researcher``.

    Before the fix ``_has_subagent`` returned True while ``_subagent_harness``
    returned ``None`` for the same name -- the exact disagreement flagged on
    #2439.
    """
    parent = _parent(web_fetch=True)
    assert RESEARCHER_NAME not in [s.name for s in parent.sub_agents]

    assert _has_subagent(RESEARCHER_NAME, parent) is True
    spec = _find_subagent_spec(RESEARCHER_NAME, parent)
    assert spec is not None
    assert spec.name == RESEARCHER_NAME
    # The lean researcher, not a parent clone.
    assert spec.executor.max_iterations == 5
    assert spec.interaction.conversational is False
    # Gate agreeing means the harness resolves too (no longer None).
    assert _subagent_harness(RESEARCHER_NAME, parent) == "claude-sdk"
    # The researcher opts into no per-dispatch harness override.
    assert _subagent_allowed_harnesses(RESEARCHER_NAME, parent) == frozenset()


def test_researcher_inherits_owner_harness() -> None:
    parent = _parent(web_fetch=True, harness="codex")
    assert _subagent_harness(RESEARCHER_NAME, parent) == "codex"


def test_no_researcher_without_web_fetch_builtin() -> None:
    """Security boundary: no ``web_fetch`` builtin -> the name resolves to nothing.

    Reconstructing unconditionally would let a caller-supplied name coerce an
    arbitrary parent into a shell-capable researcher.
    """
    parent = _parent(web_fetch=False)
    assert _has_subagent(RESEARCHER_NAME, parent) is False
    assert _find_subagent_spec(RESEARCHER_NAME, parent) is None
    assert _subagent_harness(RESEARCHER_NAME, parent) is None
    assert _subagent_allowed_harnesses(RESEARCHER_NAME, parent) == frozenset()


def test_researcher_resolves_from_nested_owner() -> None:
    """The ``web_fetch`` owner may be a nested sub-agent, not the root."""
    owner = _parent(web_fetch=True)
    owner.name = "researcher_host"
    owner.llm = LLMConfig(model="anthropic/claude-nested-7")
    root = AgentSpec(
        spec_version=1,
        name="coordinator",
        llm=LLMConfig(model="openai/gpt-5.4"),
        executor=ExecutorSpec(max_iterations=40, config={"harness": "claude-sdk"}),
        tools=ToolsConfig(builtins=[]),
        sub_agents=[owner],
    )
    assert _has_subagent(RESEARCHER_NAME, root) is True
    spec = _find_subagent_spec(RESEARCHER_NAME, root)
    assert spec is not None
    # Rebuilt from the OWNER -> inherits the owner's LLM, not the root's.
    assert spec.llm is not None
    assert spec.llm.model == "anthropic/claude-nested-7"


def test_declared_sub_agent_still_resolves() -> None:
    """A normally declared sub-agent still resolves via the top-level list."""
    child = AgentSpec(spec_version=1, name="reviewer")
    parent = _parent(web_fetch=True)
    parent.sub_agents.append(child)
    assert _has_subagent("reviewer", parent) is True
    assert _find_subagent_spec("reviewer", parent) is child


def test_unknown_name_still_missing() -> None:
    """The researcher fallback is scoped to ``__web_researcher`` only."""
    parent = _parent(web_fetch=True)
    assert _has_subagent("does-not-exist", parent) is False
    assert _find_subagent_spec("does-not-exist", parent) is None
