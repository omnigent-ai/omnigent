from __future__ import annotations

import pytest

from omnigent.models.claude_model_vocabulary import (
    claude_model_alias,
    claude_model_command_arg,
    model_vocabulary_env,
    normalized_model_id,
    picker_command_values,
    picker_value_for_model,
    prefix_folded_model_id,
    served_alias_pins,
    served_canonical_overrides,
)

# A ucode-style launch pinning: each family alias mapped to a gateway id,
# plus the extra picker slot holding the newer sonnet generation.
_PINNED_ENV = {
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-4-8",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "databricks-claude-sonnet-4-6",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "databricks-claude-haiku-4-5",
    "ANTHROPIC_CUSTOM_MODEL_OPTION": "databricks-claude-sonnet-5",
}


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("databricks-claude-opus-4-8", "opus"),
        ("databricks-claude-sonnet-4-6", "sonnet"),
        ("databricks-claude-haiku-4-5", "haiku"),
        # Pinned to the custom slot, and the Agent tool enum has no slot for
        # it: "sonnet" resolves to the pinned 4-6, so stepping down would run
        # a model nobody asked for.
        ("databricks-claude-sonnet-5", None),
        ("claude-opus-4-8[1m]", "opus"),
        ("sonnet", "sonnet"),
        ("databricks-gpt-5-5", None),
    ],
)
def test_alias_translation_under_a_pinned_launch(model: str, expected: str | None) -> None:
    assert claude_model_alias(model, _PINNED_ENV) == expected


def test_alias_from_the_id_when_nothing_is_pinned() -> None:
    """A direct Anthropic login accepts the family alias as-is."""
    assert claude_model_alias("databricks-claude-sonnet-5", {}) == "sonnet"
    assert claude_model_alias("mystery-model", {}) is None


def test_unpinned_family_is_untranslatable() -> None:
    """An unpinned alias resolves to a vendor id the gateway rejects."""
    env = {"ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-4-8"}
    assert claude_model_alias("databricks-claude-sonnet-5", env) is None
    assert claude_model_alias("databricks-claude-opus-4-8", env) == "opus"


def test_alias_requires_the_pin_to_match_the_routed_id() -> None:
    env = {"ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-5"}
    assert claude_model_alias("databricks-claude-opus-4-8", env) is None
    assert claude_model_alias("databricks-claude-opus-5", env) == "opus"


# A launch whose family pins drifted a generation ahead of the routed id, with
# and without the extra picker slot holding that exact id.
_DRIFTED_ENV = {
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-5",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "databricks-claude-sonnet-5",
}
_DRIFTED_WITH_SLOT = {
    **_DRIFTED_ENV,
    "ANTHROPIC_CUSTOM_MODEL_OPTION": "databricks-claude-opus-4-8",
}


@pytest.mark.parametrize(
    ("model", "env", "expected"),
    [
        # Drifted pins and no custom slot: nothing spells the routed id.
        ("databricks-claude-opus-4-8", _DRIFTED_ENV, None),
        # The slot holds it exactly, so that is the ``/model`` arg.
        ("databricks-claude-opus-4-8", _DRIFTED_WITH_SLOT, "databricks-claude-opus-4-8"),
        # ``/model`` compares the custom slot byte-exactly, so the arg is the
        # env's own spelling even when the routed id differs in prefix or case.
        ("system.ai.claude-opus-4-8", _DRIFTED_WITH_SLOT, "databricks-claude-opus-4-8"),
        # A pinned launch prefers the custom slot's exact id over stepping down
        # to the family alias.
        ("databricks-claude-sonnet-5", _PINNED_ENV, "databricks-claude-sonnet-5"),
        # A family pin that already matches resolves to the bare alias.
        ("databricks-claude-sonnet-4-6", _PINNED_ENV, "sonnet"),
        # Not a Claude id at all.
        ("databricks-gpt-5-5", _PINNED_ENV, None),
        # Empty / whitespace-only input is never a spellable model.
        ("", _PINNED_ENV, None),
        ("   ", _PINNED_ENV, None),
        # UNPINNED (canonical endpoint): a full Anthropic id names an exact
        # generation and ``/model`` accepts it verbatim — stepping down to
        # the family alias would land on claude's CURRENT generation (picking
        # Opus 4.8 (1M) used to type ``/model opus`` and run Opus 5).
        ("claude-opus-4-8[1m]", {}, "claude-opus-4-8[1m]"),
        ("claude-sonnet-4-6", {}, "claude-sonnet-4-6"),
        # Bare family aliases stay aliases on an unpinned env.
        ("opus", {}, "opus"),
        ("sonnet[1m]", {}, "sonnet[1m]"),
        # A gateway spelling still cannot be typed on an unpinned session.
        ("databricks-claude-opus-4-8", {}, "opus"),
        # With pins, a full id no alias resolves to stays unspeakable —
        # the caller fails loud instead of switching to something else.
        ("claude-fable-5", _PINNED_ENV, None),
    ],
)
def test_command_arg_spells_the_routed_model_or_nothing(
    model: str, env: dict[str, str], expected: str | None
) -> None:
    assert claude_model_command_arg(model, env) == expected


def test_model_vocabulary_env_rebuilds_the_pinning_from_picker_rows() -> None:
    env = model_vocabulary_env(
        [
            {"id": "opus", "model": "databricks-claude-opus-5"},
            {"id": "sonnet", "model": "databricks-claude-sonnet-5"},
            {"id": "sonnet_5", "model": "databricks-claude-opus-4-8"},
            {"id": "haiku"},
            "not-a-row",
        ]
    )
    assert env == {
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-5",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "databricks-claude-sonnet-5",
        "ANTHROPIC_CUSTOM_MODEL_OPTION": "databricks-claude-opus-4-8",
    }
    assert claude_model_command_arg("databricks-claude-opus-4-8", env) == (
        "databricks-claude-opus-4-8"
    )
    assert model_vocabulary_env([]) == {}


def test_model_vocabulary_env_ignores_rows_that_pin_nothing() -> None:
    """A direct Claude login's curated rows restate their own key."""
    assert (
        model_vocabulary_env(
            [
                {"id": "opus", "model": "opus", "displayName": "Opus"},
                {"id": "sonnet_5", "model": "sonnet_5", "displayName": "Sonnet 5"},
            ]
        )
        == {}
    )


def test_normalized_model_id_strips_prefix_and_context_suffix() -> None:
    assert normalized_model_id("databricks-claude-sonnet-5") == "claude-sonnet-5"
    assert normalized_model_id("system.ai.claude-sonnet-5") == "claude-sonnet-5"
    assert normalized_model_id("Claude-Opus-4-8[1M]") == "claude-opus-4-8"


def test_prefix_fold_strips_the_namespace_but_keeps_the_context_marker() -> None:
    """The ``[1m]`` marker denotes a distinct request, so only the prefix folds."""
    assert prefix_folded_model_id("system.ai.claude-opus-4-8[1m]") == "claude-opus-4-8[1m]"
    assert prefix_folded_model_id("databricks-claude-sonnet-5") == "claude-sonnet-5"
    assert prefix_folded_model_id("Claude-Opus-4-8[1M]") == "claude-opus-4-8[1m]"
    assert prefix_folded_model_id("claude-haiku-4-5") == "claude-haiku-4-5"


def test_catalog_prefixes_match_the_routing_defaults() -> None:
    """This module duplicates the prefix list to stay stdlib-only; keep it equal."""
    from omnigent.models.claude_model_vocabulary import _CATALOG_PREFIXES
    from omnigent.server.smart_routing import MODEL_ID_PREFIXES

    assert _CATALOG_PREFIXES == MODEL_ID_PREFIXES


@pytest.mark.parametrize(
    ("candidate", "env"),
    [
        ("sonnet[1m]", {}),
        ("sonnet[1m]", _PINNED_ENV),
        ("opus[1m]", _PINNED_ENV),
        ("Fable[1M]", {}),
    ],
)
def test_bracket_family_aliases_are_their_own_model_arguments(
    candidate: str,
    env: dict[str, str],
) -> None:
    """``sonnet[1m]`` is a settable alias the harness enumerates itself.

    It must pass through verbatim on pinned and bare envs alike: stepping
    down to ``sonnet`` silently drops the context marker, and ``None``
    blocks a switch the pane would accept.
    """
    assert claude_model_alias(candidate, env) == candidate.lower()
    assert claude_model_command_arg(candidate, env) == candidate.lower()


def test_bracket_marker_on_a_non_alias_is_not_an_argument() -> None:
    """The bracket pass-through covers only Claude's own family aliases."""
    assert claude_model_alias("gpt-5.5[1m]", {}) is None
    # A dangling bracket is not a marker; on a pinned env nothing else
    # claims it either (the bare-login segment fallback is separate).
    assert claude_model_alias("sonnet[", _PINNED_ENV) is None


def test_served_alias_pins_pick_the_newest_served_id_per_family() -> None:
    served = [
        "databricks-claude-opus-4-7",
        "databricks-claude-opus-4-8",
        "anthropic/claude-sonnet-5",
        "gw-claude-haiku-4-5",
        "databricks-gpt-5-6",
        "claude-opus-5[1m]",
    ]
    assert served_alias_pins(served) == {
        # ``claude-opus-5`` outranks ``4-8``; the [1m] marker is not a
        # different model, so the spelling the gateway listed is kept.
        "opus": "claude-opus-5[1m]",
        "sonnet": "anthropic/claude-sonnet-5",
        "haiku": "gw-claude-haiku-4-5",
    }


def test_served_alias_pins_ignore_ids_of_no_claude_family() -> None:
    assert served_alias_pins(["databricks-gpt-5-6", "gemini-3-pro", ""]) == {}


def test_served_canonical_overrides_map_canonical_ids_to_gateway_spellings() -> None:
    """Every served generation becomes reachable by its canonical spelling.

    Claude Code names a model itself when its refusal-fallback re-issues a
    flagged turn, using a canonical id from a route table internal to the CLI.
    The map has to cover whichever id that is, so it covers all of them.
    """
    assert served_canonical_overrides(
        [
            "databricks-claude-opus-4-8",
            "databricks-claude-opus-5",
            "anthropic/claude-sonnet-5",
            "gw-claude-haiku-4-5",
            "system.ai.claude-fable-5-1",
        ]
    ) == {
        "claude-opus-4-8": "databricks-claude-opus-4-8",
        "claude-opus-5": "databricks-claude-opus-5",
        "claude-sonnet-5": "anthropic/claude-sonnet-5",
        "claude-haiku-4-5": "gw-claude-haiku-4-5",
        "claude-fable-5-1": "system.ai.claude-fable-5-1",
    }


def test_served_canonical_overrides_need_no_knowledge_of_a_generation() -> None:
    """A model the vocabulary has never heard of still maps.

    The rewrite is derived from the served spelling alone, so a future
    generation — or a family with no alias of its own, like Mythos — is
    covered without touching this module.
    """
    assert served_canonical_overrides(
        ["databricks-claude-opus-6", "databricks-claude-mythos-5"]
    ) == {
        "claude-opus-6": "databricks-claude-opus-6",
        "claude-mythos-5": "databricks-claude-mythos-5",
    }


@pytest.mark.parametrize(
    "served",
    [
        pytest.param(["claude-opus-4-8"], id="already-canonical"),
        pytest.param(["claude-opus-4-8[1m]"], id="context-marker-is-not-a-spelling"),
        pytest.param(["databricks-gpt-5-6", "gemini-3-pro", ""], id="no-claude-model"),
        pytest.param([], id="empty-listing"),
    ],
)
def test_served_canonical_overrides_stay_empty_when_there_is_nothing_to_rewrite(
    served: list[str],
) -> None:
    """No entry unless the gateway's spelling actually differs.

    An empty map leaves Claude Code exactly as it behaves today, which is the
    right outcome when the listing is unhelpful or unreachable.
    """
    assert served_canonical_overrides(served) == {}


def test_served_canonical_overrides_keep_the_first_of_two_equal_spellings() -> None:
    """Two served ids sharing a canonical form resolve deterministically."""
    assert served_canonical_overrides(["databricks-claude-opus-4-8", "gw-claude-opus-4-8"]) == {
        "claude-opus-4-8": "databricks-claude-opus-4-8"
    }


# A gateway that manages the picker names every row by the id it serves,
# family models and third-party models alike — the shape Claude Code's
# initialize response reports for a workspace-managed Claude Code.
_MANAGED_PICKER_VALUES = (
    "system.ai.claude-opus-4-8[1m]",
    "system.ai.claude-sonnet-4-6[1m]",
    "system.ai.claude-haiku-4-5",
    "system.ai.glm-5-3",
)


def test_picker_command_values_reads_the_rows_as_the_vocabulary() -> None:
    assert picker_command_values(
        [
            {"id": "default", "model": "system.ai.claude-opus-4-8[1m]"},
            {"id": "system.ai.glm-5-3", "model": "system.ai.glm-5-3"},
            {"id": "system.ai.glm-5-3", "model": "system.ai.glm-5-3"},
            {"id": "  opus  ", "model": "system.ai.claude-opus-4-8[1m]"},
            {"id": ""},
            {"model": "system.ai.kimi-k3"},
            "not-a-row",
        ]
    ) == ["system.ai.glm-5-3", "opus"]
    assert picker_command_values([]) == []


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # The reported bug: a managed model of no Claude family. Every alias
        # is pinned elsewhere, so only the picker's own row spells it.
        ("system.ai.glm-5-3", "system.ai.glm-5-3"),
        # Catalog prefix and case fold; the row's spelling is what is typed.
        ("databricks-glm-5-3", "system.ai.glm-5-3"),
        ("SYSTEM.AI.GLM-5-3", "system.ai.glm-5-3"),
        # A row carrying the 1M marker is that model's only spelling here,
        # so a routed id without the marker still reaches it.
        ("system.ai.claude-opus-4-8[1m]", "system.ai.claude-opus-4-8[1m]"),
        ("databricks-claude-opus-4-8", "system.ai.claude-opus-4-8[1m]"),
        # Nothing in the picker serves it: the caller must fail loud.
        ("system.ai.kimi-k3", None),
    ],
)
def test_picker_value_for_model_speaks_the_rows_the_cli_listed(
    model: str, expected: str | None
) -> None:
    assert picker_value_for_model(model, _MANAGED_PICKER_VALUES) == expected


def test_command_arg_prefers_the_picker_over_an_unrelated_pin() -> None:
    """A managed row is speakable even when every alias is pinned elsewhere.

    Without the picker the pane's vocabulary is just the pinning, so a
    switch to a model of no Claude family answered "no spelling" and left
    the session on its old model.
    """
    assert claude_model_command_arg("system.ai.glm-5-3", _PINNED_ENV) is None
    assert (
        claude_model_command_arg(
            "system.ai.glm-5-3", _PINNED_ENV, picker_values=_MANAGED_PICKER_VALUES
        )
        == "system.ai.glm-5-3"
    )
    # A pin the picker does not list still resolves through the alias.
    assert (
        claude_model_command_arg(
            "databricks-claude-sonnet-4-6", _PINNED_ENV, picker_values=("system.ai.glm-5-3",)
        )
        == "sonnet"
    )


def test_command_arg_keeps_failing_loud_when_the_picker_is_unknown() -> None:
    """No picker rows recorded yet leaves today's pinning-only answer."""
    assert claude_model_command_arg("system.ai.glm-5-3", _PINNED_ENV, picker_values=()) is None
