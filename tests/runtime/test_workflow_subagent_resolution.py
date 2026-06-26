"""Regression: web_fetch's ``__web_researcher`` must resolve on a bundle re-parse.

``WebFetchTool`` synthesizes the ``__web_researcher`` sub-agent spec in memory
and appends it to the parent's live ``sub_agents`` list, but that spec is never
serialized into the parent's persisted bundle. A child ``__web_researcher``
session boots by re-parsing the bundle fresh (``runner/_entry.py`` spec
resolver), so the researcher is absent from the re-parsed tree.

Before the fix, :func:`_find_spec_by_name` returned ``None`` for that
resolve-miss, and every swap site (``runner/app.py`` POST /v1/sessions,
``_run_turn_bg``, ``_resolve_session_spec_entry``,
``_resolve_harness_and_spawn_env``; ``server/routes/sessions.py``) swaps only
``if ... is not None``, so the child silently booted as a full clone of the
parent. When the parent is a coordinator, every ``__web_researcher`` became a
coordinator clone that re-ran the whole panel: runaway recursion / fan-out via
``sys_session_send`` (the failure mode ``runner/app.py`` calls out by name).

The fix reconstructs the lean researcher from the parent on a resolve-miss, so
the child boots as the intended ``max_iterations=5`` curl helper instead.
"""

from __future__ import annotations

from omnigent.runtime.workflow import _find_spec_by_name
from omnigent.spec.types import (
    AgentSpec,
    BuiltinToolConfig,
    ExecutorSpec,
    LLMConfig,
    ToolsConfig,
)
from omnigent.tools.builtins.web_fetch import RESEARCHER_NAME, build_researcher_spec


def _coordinator_parent(*, web_fetch: bool = True) -> AgentSpec:
    """A coordinator-style parent as re-parsed from its persisted bundle.

    Critically, it does NOT contain ``__web_researcher`` in ``sub_agents`` --
    ``WebFetchTool`` only appends that spec in memory at runtime, and it is
    never serialized, so a fresh bundle parse lacks it. The ``panelist`` child
    stands in for the real sub-agents a grounded coordinator declares.

    :param web_fetch: When ``True`` (default), the parent declares the
        ``web_fetch`` builtin in ``tools.builtins`` -- the authored config
        that is the sole reason ``__web_researcher`` exists, and which IS
        serialized into the bundle. When ``False``, the parent never enabled
        web_fetch, so it has no researcher child.
    :returns: A parent :class:`AgentSpec` without the researcher in its tree.
    """
    panelist = AgentSpec(spec_version=1, name="panelist")
    builtins = [BuiltinToolConfig(name="web_fetch")] if web_fetch else []
    return AgentSpec(
        spec_version=1,
        name="concordia",
        llm=LLMConfig(model="openai/gpt-5.4"),
        executor=ExecutorSpec(max_iterations=40),
        tools=ToolsConfig(builtins=builtins),
        sub_agents=[panelist],
    )


def test_web_researcher_resolves_to_lean_researcher_on_bundle_reparse() -> None:
    """A resolve-miss for ``__web_researcher`` must rebuild the lean researcher.

    Fails before the fix (the resolver returns ``None``, so every swap site
    falls back to the parent spec and boots the child as a parent clone).
    """
    parent = _coordinator_parent(web_fetch=True)
    # Precondition: the re-parsed bundle does not carry the researcher...
    assert RESEARCHER_NAME not in [s.name for s in parent.sub_agents]
    # ...but it DOES declare web_fetch, the authored builtin that is the sole
    # reason the researcher exists and which is serialized into the bundle.
    assert "web_fetch" in [b.name for b in parent.tools.builtins]

    resolved = _find_spec_by_name(parent, RESEARCHER_NAME)

    assert resolved is not None, (
        "resolve-miss returned None; every swap site falls back to the parent "
        "spec, booting __web_researcher as a parent clone (runaway recursion "
        "via sys_session_send when the parent is a coordinator)."
    )
    assert resolved.name == RESEARCHER_NAME
    # The lean researcher, not the coordinator clone: capped iterations + one-shot.
    assert resolved.executor.max_iterations == 5, (
        f"expected the lean researcher (max_iterations=5), got "
        f"{resolved.executor.max_iterations} -- that is the parent's executor, "
        "i.e. the child booted as a parent clone."
    )
    assert resolved.interaction.conversational is False
    assert resolved.name != parent.name
    # Inherits the parent's LLM so panel grounding keeps working on the
    # parent's provider.
    assert resolved.llm is not None
    assert resolved.llm.model == "openai/gpt-5.4"


def test_web_researcher_not_synthesized_without_web_fetch_builtin() -> None:
    """Boundary regression: a parent that never enabled ``web_fetch`` must NOT
    get a synthesized ``__web_researcher``.

    ``__web_researcher`` only exists because ``WebFetchTool.__init__`` appends
    it, so a parent without the ``web_fetch`` builtin has no such child.
    Resolving the researcher name against that parent must fall through to
    normal resolution and return ``None`` rather than reconstruct a
    shell-capable child (``build_researcher_spec`` synthesizes an ``OSEnvSpec``)
    from a caller-controlled ``sub_agent_name``.
    """
    parent = _coordinator_parent(web_fetch=False)
    # Precondition: this parent genuinely lacks the web_fetch builtin.
    assert "web_fetch" not in [b.name for b in parent.tools.builtins]

    assert _find_spec_by_name(parent, RESEARCHER_NAME) is None


def test_declared_sub_agent_still_resolves_from_tree() -> None:
    """Bundle-declared sub-agents must still resolve by tree search.

    Guards against the researcher fallback shadowing real specs.
    """
    parent = _coordinator_parent()
    found = _find_spec_by_name(parent, "panelist")
    assert found is not None
    assert found.name == "panelist"


def test_missing_non_researcher_name_still_returns_none() -> None:
    """The fallback is scoped to ``__web_researcher`` only.

    Any other unknown name must still resolve to ``None`` so a genuine
    misconfiguration fails loud rather than silently synthesizing a spec.
    """
    parent = _coordinator_parent()
    assert _find_spec_by_name(parent, "does-not-exist") is None


def test_declared_web_researcher_is_returned_verbatim() -> None:
    """When the researcher IS present in the tree it is returned verbatim.

    Covers the in-process parent where ``WebFetchTool`` already appended the
    researcher: the tree search must win over reconstruction so there is no
    divergence between the appended spec and a freshly rebuilt one.
    """
    parent = _coordinator_parent()
    declared = build_researcher_spec(parent)
    parent.sub_agents.append(declared)
    found = _find_spec_by_name(parent, RESEARCHER_NAME)
    assert found is declared
