"""Tests for the ``omnigent setup`` supervisor-harness offering (wizard.py).

The supervisor sub-step (:func:`omnigent.onboarding.wizard._prompt_supervisor`)
routes a CLI choice to :func:`_prompt_cli_supervisor_config` and EVERY other
choice to :func:`_prompt_openai_agents_config`, which hard-codes
``harness="openai-agents"``. So any harness offered as a supervisor that is
neither a CLI harness nor openai-agents would silently produce an
openai-agents spec. These tests pin that invariant — the reason antigravity
(Gemini-native, and with no supervisor credential flow yet) is not offered.
"""

from __future__ import annotations

from omnigent.onboarding import wizard


def test_antigravity_not_offered_as_supervisor() -> None:
    """Antigravity must not appear among the offerable supervisor harnesses.

    Offering it would route through ``_prompt_openai_agents_config`` and
    hand back an openai-agents spec for a user who picked Antigravity.
    """
    assert "antigravity" not in wizard._API_HARNESSES
    assert "antigravity" not in wizard._CLI_HARNESSES


def test_api_harness_descriptions_have_no_openai_gateway_clause_for_gemini() -> None:
    """No offered API harness advertises an OpenAI gateway it can't use.

    Specifically, the dropped antigravity entry described itself as offering
    "an OpenAI-compatible gateway", which is false for the Gemini-native SDK.
    """
    assert all(
        "antigravity" not in info["description"].lower() for info in wizard._API_HARNESSES.values()
    )


def test_every_offerable_api_supervisor_builds_its_own_harness() -> None:
    """Each ``_API_HARNESSES`` entry must not silently become openai-agents.

    The dispatch sends any non-CLI choice to ``_prompt_openai_agents_config``
    (``harness="openai-agents"``). So the only API harness safe to offer today
    is openai-agents itself; any other key would be mis-built. This guards
    against a future re-add of a non-openai harness without a real branch.
    """
    for harness in wizard._API_HARNESSES:
        assert harness == "openai-agents", (
            f"{harness!r} is offered as a supervisor but the dispatch would build it as "
            "'openai-agents'; add a dedicated config branch before offering it."
        )


def test_openai_agents_config_builder_is_pinned_to_openai_agents() -> None:
    """The non-CLI supervisor builder still hard-codes ``openai-agents``.

    This is the property that makes offering any OTHER harness here a bug —
    documenting why the dispatch's ``else`` branch is openai-agents-only.
    """
    import inspect

    src = inspect.getsource(wizard._prompt_openai_agents_config)
    assert 'harness="openai-agents"' in src
