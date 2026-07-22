"""Guard tests for the declarative harness capability model.

Capabilities are declared per harness in ``_BUILTIN_CONTRIBUTION.capabilities``.
These tests assert the declarations stay complete (every built-in harness is
covered) and that the two *derivable* axes (model_family, subagents) do not
contradict the code that actually enforces them, so the table cannot silently
drift.
"""

from __future__ import annotations

from omnigent import harness_plugins as hp
from omnigent.harness_availability import CODEX_CANONICAL_HARNESSES
from omnigent.harness_capabilities import (
    AuthModel,
    EffortFamily,
    Elicitation,
    HarnessCapabilities,
    IntegrationMode,
    ModelFamily,
    Resume,
)
from omnigent.harness_plugins import (
    HarnessContribution,
    harness_capabilities,
    harness_catalog,
    harness_setup_steps_by_spelling,
    native_agents,
    valid_harnesses,
)
from omnigent.model_override import (
    _ANTIGRAVITY_FAMILY_HARNESSES,
    _CLAUDE_FAMILY_HARNESSES,
)

_NATIVE_MODES = frozenset({IntegrationMode.NATIVE_TUI, IntegrationMode.NATIVE_SERVER})


def test_every_builtin_harness_declares_capabilities() -> None:
    caps = harness_capabilities()
    missing = sorted(valid_harnesses() - set(caps))
    assert not missing, f"harnesses missing a capability declaration: {missing}"


def test_capability_keys_are_valid_harnesses() -> None:
    # No stray capability entry for a non-existent harness id.
    stray = sorted(set(harness_capabilities()) - valid_harnesses())
    assert not stray, f"capability entries for unknown harnesses: {stray}"


def test_native_agents_have_native_integration_mode() -> None:
    caps = harness_capabilities()
    native_harness_ids = {agent.harness for agent in native_agents()}
    for harness in native_harness_ids:
        assert caps[harness].integration_mode in _NATIVE_MODES, harness


def test_model_family_matches_model_override_sets() -> None:
    # model_family is derivable from model_override's family frozensets, so the
    # declaration must not contradict the code that enforces routing.
    for harness, capability in harness_capabilities().items():
        family = capability.model_family
        if harness in _CLAUDE_FAMILY_HARNESSES:
            assert family is ModelFamily.CLAUDE, harness
        elif harness in CODEX_CANONICAL_HARNESSES:
            assert family is ModelFamily.GPT, harness
        elif harness in _ANTIGRAVITY_FAMILY_HARNESSES:
            assert family is ModelFamily.GEMINI, harness
        else:
            assert family is ModelFamily.MULTI, harness


def test_subagents_matches_native_wrapper_label() -> None:
    # subagents is derivable: only native agents with a subagent_wrapper_label
    # can spawn Omnigent native sub-agents.
    subagent_capable = {agent.harness for agent in native_agents() if agent.subagent_wrapper_label}
    for harness, capability in harness_capabilities().items():
        expected = harness in subagent_capable
        assert capability.subagents == expected, harness


def test_native_harnesses_resume_warm() -> None:
    caps = harness_capabilities()
    native_harness_ids = {agent.harness for agent in native_agents()}
    for harness in native_harness_ids:
        assert caps[harness].resume is Resume.WARM_REATTACH, harness


def test_p0_bench_harnesses_declare_interrupt_and_streaming() -> None:
    # The harness bench live-probes these four and declares them SUPPORTED for
    # interrupt + streaming; the capability declaration must match so the bench
    # can derive its expected matrix from capabilities without contradiction.
    caps = harness_capabilities()
    for harness in ("claude-sdk", "codex", "pi", "openai-agents"):
        assert caps[harness].interrupt is True, harness
        assert caps[harness].streaming is True, harness


def test_optional_bench_capabilities_default_to_unknown() -> None:
    capability = HarnessCapabilities(
        IntegrationMode.SDK_IN_PROCESS,
        Elicitation.NONE,
        Resume.COLD_ONLY,
        EffortFamily.NONE,
        ModelFamily.MULTI,
        AuthModel.OWN_AUTH,
        subagents=False,
        interrupt=True,
        streaming=True,
    )

    assert capability.steering is None
    assert capability.live_queue is None
    assert capability.images is None
    assert capability.compaction is None
    assert capability.as_dict() == {
        "integration_mode": "sdk-in-process",
        "elicitation": "none",
        "resume": "cold-only",
        "effort": "none",
        "model_family": "multi",
        "auth": "own-auth",
        "subagents": False,
        "interrupt": True,
        "streaming": True,
        "steering": None,
        "live_queue": None,
        "images": None,
        "compaction": None,
    }


def test_community_capabilities_cannot_override_builtin() -> None:
    # `capabilities` is part of the collision-key set, so a community plugin that
    # declares capabilities for a built-in harness id is rejected rather than
    # silently overriding the built-in declaration (last-wins in _merge_dict).
    evil = HarnessContribution(
        name="omnigent-evil",
        capabilities={
            "claude-sdk": HarnessCapabilities(
                IntegrationMode.SDK_IN_PROCESS,
                Elicitation.NONE,
                Resume.COLD_ONLY,
                EffortFamily.NONE,
                ModelFamily.MULTI,
                AuthModel.OWN_AUTH,
                subagents=False,
                interrupt=False,
                streaming=False,
            )
        },
    )
    error = hp._validate_community_contribution(
        evil, entry_point_name="evil", existing=(hp._BUILTIN_CONTRIBUTION,)
    )
    assert error is not None
    assert "claude-sdk" in error


def test_catalog_rows_include_capabilities() -> None:
    rows = harness_catalog()
    caps = harness_capabilities()
    for row in rows:
        if row["id"] in caps:
            assert "capabilities" in row, row["id"]
            # JSON-serializable: values are primitives, not enums.
            for value in row["capabilities"].values():
                assert value is None or isinstance(value, (str, bool))


def test_catalog_rows_carry_setup_steps() -> None:
    """Every row exposes an ordered, JSON-serializable setup checklist."""
    rows = {row["id"]: row for row in harness_catalog()}
    for row in rows.values():
        assert "setup_steps" in row, row["id"]
        assert len(row["setup_steps"]) >= 1
        for step in row["setup_steps"]:
            assert step["kind"] in ("install", "auth")
            assert step["action"] in ("install", "command", "setup")
            # JSON-serializable primitives only.
            for value in step.values():
                assert value is None or isinstance(value, str)
    # Codex is a first-class harness: install (one-click) then a login command.
    codex = rows["codex"]["setup_steps"]
    assert [s["action"] for s in codex] == ["install", "command"]
    assert codex[1]["command"] == "codex login"


def test_setup_steps_by_spelling_covers_native_and_installable_ids() -> None:
    """The by-spelling map resolves the ids a session declares, not just picker
    rows — native wrappers and installable non-picker ids included."""
    by_spelling = harness_setup_steps_by_spelling()
    # Native wrappers (what a session actually declares) resolve to steps...
    for native in ("codex-native", "claude-native", "opencode-native", "qwen-native"):
        assert native in by_spelling, native
        assert len(by_spelling[native]) >= 1
    # ...and match their bare-id counterpart's steps (same ui_setup_steps source).
    assert by_spelling["codex-native"] == by_spelling["codex"]
    # Installable ids that are NOT picker rows still resolve.
    assert "opencode" in by_spelling
    assert "qwen" in by_spelling
