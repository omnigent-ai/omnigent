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
    ForkHistory,
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
    # New axes default to their unset value: fork_history=none, shell tool None.
    assert capability.fork_history is ForkHistory.NONE
    assert capability.shell_tool_name is None
    assert capability.shell_tool_prompt is None
    # The parity axes default to "unknown" rather than to a guess, so an
    # undeclared feature is distinguishable from one checked and found absent.
    unknown_feature = {
        "support": "unknown",
        "mode": None,
        "limitations": None,
        "last_verified": None,
    }
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
        "fork_history": "none",
        "shell_tool_name": None,
        "shell_tool_prompt": None,
        "skills": unknown_feature,
        "hooks": unknown_feature,
        "mcp": unknown_feature,
        "plan_mode": unknown_feature,
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
            # JSON-serializable: primitives, or a flat dict of primitives for a
            # structured axis. Nothing may still be an enum by this point.
            for value in row["capabilities"].values():
                if isinstance(value, dict):
                    for nested in value.values():
                        assert nested is None or isinstance(nested, str)
                    continue
                assert value is None or isinstance(value, (str, bool))


def test_catalog_includes_hermes() -> None:
    """Hermes must appear in the web picker catalog (regression: it was a
    valid harness with capabilities but had no ``harness_labels`` entry, so
    ``harness_catalog`` — which iterates labels — dropped it)."""
    rows = harness_catalog()
    hermes = next((row for row in rows if row["id"] == "hermes"), None)
    assert hermes is not None, "hermes missing from harness_catalog()"
    assert hermes["label"] == "Hermes"
    # The catalog only lists valid harnesses and hermes declares capabilities,
    # so the row must carry the feature matrix like its subprocess peers.
    assert "capabilities" in hermes


def test_hermes_picker_row_has_spawn_env_plumbing() -> None:
    """A picker row is only honest if the session's choices reach the harness.

    Hermes' model env key is what both threads ``/model`` into the spawn env and
    (via ``_SDK_MODEL_OVERRIDE_HARNESSES``) makes the server accept the override
    instead of rejecting it up front."""
    from omnigent.harness_plugins import model_env_keys
    from omnigent.model_override import harness_supports_model_override

    assert model_env_keys()["hermes"] == "HARNESS_HERMES_MODEL"
    assert harness_supports_model_override("hermes")


def test_catalog_rows_carry_setup_steps() -> None:
    """Every row exposes an ordered, JSON-serializable setup checklist."""
    rows = {row["id"]: row for row in harness_catalog()}
    for row in rows.values():
        assert "setup_steps" in row, row["id"]
        assert len(row["setup_steps"]) >= 1
        for step in row["setup_steps"]:
            assert step["kind"] in ("install", "auth")
            assert step["action"] in ("install", "command", "setup", "auth")
            # JSON-serializable primitives only.
            for value in step.values():
                assert value is None or isinstance(value, str)
    # Codex is a first-class harness: install (one-click) then a UI-authable
    # auth step. The step opens the credential form (action "auth"); its
    # subscription option still carries the `codex login` command.
    codex = rows["codex"]["setup_steps"]
    assert [s["action"] for s in codex] == ["install", "auth"]
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


def test_fork_history_axis_matches_canonical_declarations() -> None:
    """fork_history is the source of truth for the server's fork-history gating.

    The two frozensets in ``_sessions/common`` are DERIVED from this axis; the
    canonical harness id is in the rebuild set iff it declares REBUILD, the
    preamble set iff PREAMBLE. (The sets also carry reversed ``native-<x>``
    spellings — asserted separately below.)
    """
    from omnigent.server.routes._sessions.common import (
        _CURSOR_FORK_HISTORY_HARNESSES,
        _FORK_HISTORY_NATIVE_HARNESSES,
    )

    for harness, capability in harness_capabilities().items():
        fh = capability.fork_history
        assert (harness in _FORK_HISTORY_NATIVE_HARNESSES) == (fh is ForkHistory.REBUILD), harness
        assert (harness in _CURSOR_FORK_HISTORY_HARNESSES) == (fh is ForkHistory.PREAMBLE), harness


def test_fork_history_derivation_preserves_prior_membership() -> None:
    """The derived sets are a superset of the pre-1.8 hand-maintained literals.

    Behavior-preservation pin: the exact pre-derivation membership (canonical ids
    PLUS the reversed ``native-<x>`` spellings the literals carried) must still be
    present, or a fork silently loses history. The derived set may add extra
    reversed spellings that canonicalize into it (harmless at the read site).
    """
    from omnigent.server.routes._sessions.common import (
        _CURSOR_FORK_HISTORY_HARNESSES,
        _FORK_HISTORY_NATIVE_HARNESSES,
    )

    prior_rebuild = frozenset(
        {
            "claude-native",
            "native-claude",
            "codex-native",
            "native-codex",
            "hermes-native",
            "native-hermes",
            "pi-native",
            "qwen-native",
        }
    )
    prior_preamble = frozenset(
        {"cursor-native", "native-cursor", "opencode-native", "native-opencode"}
    )
    assert prior_rebuild <= _FORK_HISTORY_NATIVE_HARNESSES
    assert prior_preamble <= _CURSOR_FORK_HISTORY_HARNESSES


def test_reversed_native_spellings_classify_fork_history() -> None:
    """Reversed ``native-<x>`` spellings that don't canonicalize are still gated.

    ``native-claude`` / ``native-codex`` / ``native-cursor`` are valid harness
    ids that ``canonicalize_harness`` passes through unchanged (they are NOT
    registered aliases). The read sites match on the canonicalized id, so the
    derived sets must contain these literal spellings — this guards the
    regression where an identically-behaving reversed-spelling agent silently
    loses fork history. Mirrors test_fork_reversed_native_spelling_carry_gating.
    """
    from omnigent.harness_aliases import canonicalize_harness
    from omnigent.server.routes._sessions.common import (
        _CURSOR_FORK_HISTORY_HARNESSES,
        _FORK_HISTORY_NATIVE_HARNESSES,
    )

    for spelling in ("native-claude", "native-codex"):
        assert canonicalize_harness(spelling) in _FORK_HISTORY_NATIVE_HARNESSES, spelling
    assert canonicalize_harness("native-cursor") in _CURSOR_FORK_HISTORY_HARNESSES


def test_native_tui_harnesses_declare_shell_tool_provocation() -> None:
    """Native-TUI harnesses the bench tool-probes declare a shell-tool prompt.

    The bench derives its provocation from these fields (was a hardcoded table);
    a prompt must carry the ``omnigent-bench-ok`` placeholder the probe
    token-swaps. cursor-native is intentionally probe-skipped (no shell tool),
    matching its prior absence from the table.
    """
    caps = harness_capabilities()
    probe_skipped = {"cursor-native"}
    for harness, capability in caps.items():
        if capability.integration_mode is not IntegrationMode.NATIVE_TUI:
            continue
        if harness in probe_skipped:
            assert capability.shell_tool_name is None, harness
            continue
        assert capability.shell_tool_name, harness
        assert capability.shell_tool_prompt, harness
        assert "omnigent-bench-ok" in capability.shell_tool_prompt, harness


# ── skills / hooks / MCP / plan-mode axes ──────────────────


def test_every_harness_declares_the_four_parity_axes() -> None:
    """The axes exist on every harness, even when the answer is "unknown".

    An undeclared axis has to read as ``unknown`` rather than as absent, so a
    consumer can tell "nobody has checked" apart from "checked, and no".
    """
    from omnigent.harness_capabilities import Support

    for harness, capability in harness_capabilities().items():
        for axis in ("skills", "hooks", "mcp", "plan_mode"):
            support = getattr(capability, axis).support
            assert isinstance(support, Support), f"{harness}.{axis} is not a Support"


def test_skills_declaration_matches_the_relay_that_enforces_it() -> None:
    """The skills axis must not drift from the tool surface behind it.

    A native harness ignores ``request.tools`` and sees only the relay set in
    ``runner/tool_dispatch.py``. If someone adds the skill tools to that relay,
    this test fails and the declaration has to move with it — which is the
    point, because the alternative is a published parity matrix that quietly
    stops being true.
    """
    from omnigent.harness_capabilities import IntegrationMode, Support
    from omnigent.runner.tool_dispatch import (
        _NATIVE_RELAY_BUILTIN_TOOLS,
        _SKILL_TOOLS,
    )

    relay_carries_skills = bool(_SKILL_TOOLS & _NATIVE_RELAY_BUILTIN_TOOLS)
    expected_for_native = Support.SHIMMED if relay_carries_skills else Support.UNAVAILABLE

    for harness, capability in harness_capabilities().items():
        relay_only = capability.integration_mode in (
            IntegrationMode.NATIVE_TUI,
            IntegrationMode.NATIVE_SERVER,
        )
        expected = expected_for_native if relay_only else Support.SHIMMED
        assert capability.skills.support is expected, (
            f"{harness} declares skills={capability.skills.support.value} "
            f"but its tool surface says {expected.value}"
        )


def test_an_unavailable_axis_says_why() -> None:
    """An `unavailable` cell without a reason is not an honest grading.

    "It does not work" is a bug report. "It does not work because the relay
    does not carry these tools" is something a contributor can act on.
    """
    from omnigent.harness_capabilities import Support

    for harness, capability in harness_capabilities().items():
        for axis in ("skills", "hooks", "mcp", "plan_mode"):
            feature = getattr(capability, axis)
            if feature.support is Support.UNAVAILABLE:
                assert feature.limitations, f"{harness}.{axis} is unavailable with no reason"


def test_the_axes_reach_the_catalog() -> None:
    """``GET /v1/harnesses`` has to carry them, or nothing can consume them."""
    catalog = harness_catalog()
    row = next(entry for entry in catalog if entry.get("capabilities"))
    capabilities = row["capabilities"]

    for axis in ("skills", "hooks", "mcp", "plan_mode"):
        assert axis in capabilities, f"{axis} missing from the catalog"
        assert set(capabilities[axis]) == {"support", "mode", "limitations", "last_verified"}
