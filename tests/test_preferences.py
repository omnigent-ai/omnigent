"""Preference intent and scope boundaries, without transport or native adapters."""

from dataclasses import replace

import pytest

from omnigent.preferences import (
    PluginIdentity,
    PreferenceContext,
    PreferenceIdentity,
    PreferenceKey,
    PreferenceKind,
    PreferenceLayer,
    PreferenceScope,
    preferences_from_config,
    resolve_preferences,
)

IDENTITY = PreferenceIdentity("authority-a", "user-a", "workspace-a")
CONTEXT = PreferenceContext(IDENTITY, "laptop")
MODEL = PreferenceKey(PreferenceKind.MODEL, "model")
HARNESS = PreferenceKey(PreferenceKind.HARNESS, "default")
ARGS = PreferenceKey(PreferenceKind.HARNESS_ARGS, "codex-native")
PLUGIN = PreferenceKey(PreferenceKind.PLUGIN, PluginIdentity("claude", "marketplace-a", "tools"))
SCOPES = (
    PreferenceScope.BUILTIN,
    PreferenceScope.MANAGED,
    PreferenceScope.SHARED,
    PreferenceScope.MACHINE,
    PreferenceScope.PROJECT,
    PreferenceScope.SESSION,
)


@pytest.mark.parametrize(
    "key,lower,higher",
    [
        (MODEL, "model-a", "model-b"),
        (HARNESS, "codex-native", "pi-native"),
        (ARGS, ("--profile", "base"), ()),
        (PLUGIN, True, False),
    ],
)
@pytest.mark.parametrize("scope", SCOPES[1:])
def test_precedence_is_per_key_and_independent_of_input_order(key, lower, higher, scope):
    layers = [
        PreferenceLayer(
            candidate,
            IDENTITY,
            {key: higher if candidate == scope else lower},
            installation_id="laptop" if candidate == PreferenceScope.MACHINE else None,
        )
        for candidate in SCOPES[: SCOPES.index(scope) + 1]
    ]
    other_model = PreferenceKey(PreferenceKind.MODEL, "opencode_model")
    layers[0] = layers[0].with_value(other_model, "opencode-default")
    result = resolve_preferences(CONTEXT, list(reversed(layers)))
    assert result[key].value == higher
    assert result[key].source == scope
    assert result[other_model].value == "opencode-default"
    assert result[other_model].source == PreferenceScope.BUILTIN


def test_session_switch_and_machine_removal_do_not_change_shared_intent():
    shared = PreferenceLayer(PreferenceScope.SHARED, IDENTITY, {HARNESS: "pi-native"})
    machine = PreferenceLayer(PreferenceScope.MACHINE, IDENTITY, installation_id="laptop")
    machine = machine.with_value(HARNESS, "codex-native")
    session = PreferenceLayer(PreferenceScope.SESSION, IDENTITY, {HARNESS: "claude-native"})
    assert (
        resolve_preferences(CONTEXT, [shared, machine, session])[HARNESS].value == "claude-native"
    )
    assert resolve_preferences(CONTEXT, [shared, machine])[HARNESS].value == "codex-native"
    result = resolve_preferences(CONTEXT, [shared, machine.without(HARNESS)])
    assert result[HARNESS].value == "pi-native"
    assert result[HARNESS].source == PreferenceScope.SHARED
    assert shared.values[HARNESS] == "pi-native"
    assert machine.values[HARNESS] == "codex-native"


@pytest.mark.parametrize("field", ["authority_id", "user_id", "workspace_id"])
def test_foreign_layers_are_rejected_even_when_shadowed(field):
    foreign = replace(IDENTITY, **{field: "other"})
    with pytest.raises(ValueError, match="different identity"):
        resolve_preferences(
            CONTEXT,
            [
                PreferenceLayer(PreferenceScope.SHARED, foreign, {MODEL: "foreign"}),
                PreferenceLayer(PreferenceScope.SESSION, IDENTITY, {MODEL: "local"}),
            ],
        )


def test_shared_identity_is_portable_but_machine_overrides_are_not():
    shared = PreferenceLayer(PreferenceScope.SHARED, IDENTITY, {MODEL: "shared"})
    remote = replace(CONTEXT, installation_id="remote")
    assert resolve_preferences(remote, [shared])[MODEL].value == "shared"
    machine = PreferenceLayer(PreferenceScope.MACHINE, IDENTITY, {MODEL: "local"}, "laptop")
    with pytest.raises(ValueError, match="different installation"):
        resolve_preferences(remote, [shared, machine])


def test_duplicate_scope_is_rejected_instead_of_last_writer_wins():
    layer = PreferenceLayer(PreferenceScope.SHARED, IDENTITY, {MODEL: "first"})
    with pytest.raises(ValueError, match="duplicate preference scope"):
        resolve_preferences(CONTEXT, [layer, layer.with_value(MODEL, "second")])


def test_snapshot_and_edits_do_not_alias_input_maps():
    values = {MODEL: "original"}
    layer = PreferenceLayer(PreferenceScope.SHARED, IDENTITY, values)
    values[MODEL] = "external-change"
    assert layer.values[MODEL] == "original"
    with pytest.raises(TypeError):
        layer.values[MODEL] = "mutated"
    assert layer.with_value(MODEL, "new").values[MODEL] == "new"
    assert layer.without(MODEL).values == {}
    assert layer.values[MODEL] == "original"


def test_plugin_keys_distinguish_ecosystem_source_and_name():
    plugin = PLUGIN.name
    keys = [PLUGIN] + [
        PreferenceKey(PreferenceKind.PLUGIN, replace(plugin, **{field: "other"}))
        for field in ("ecosystem", "source", "name")
    ]
    layer = PreferenceLayer(PreferenceScope.SHARED, IDENTITY, dict.fromkeys(keys, False))
    assert len(resolve_preferences(CONTEXT, [layer])) == 4
    assert resolve_preferences(CONTEXT, [layer.without(PLUGIN)])[keys[1]].value is False


@pytest.mark.parametrize(
    "key,value",
    [
        (MODEL, False),
        (MODEL, ""),
        (HARNESS, " "),
        (PLUGIN, "false"),
        (PLUGIN, 0),
        (ARGS, ["--profile", "x"]),
        (ARGS, (1,)),
        (ARGS, "--profile x"),
    ],
)
def test_invalid_values_fail_at_the_layer_boundary(key, value):
    with pytest.raises(ValueError, match="invalid value"):
        PreferenceLayer(PreferenceScope.SHARED, IDENTITY, {key: value})


@pytest.mark.parametrize(
    "kind,name",
    [
        (PreferenceKind.MODEL, "token"),
        (PreferenceKind.HARNESS, "command"),
        (PreferenceKind.PLUGIN, "ambiguous-plugin"),
        (PreferenceKind.HARNESS_ARGS, ""),
        (PreferenceKind.HARNESS_ARGS, "default"),
    ],
)
def test_invalid_keys_are_rejected(kind, name):
    with pytest.raises(ValueError):
        PreferenceKey(kind, name)


@pytest.mark.parametrize(
    "scope,installation",
    [
        (PreferenceScope.MACHINE, None),
        (PreferenceScope.MACHINE, ""),
        (PreferenceScope.SHARED, "laptop"),
    ],
)
def test_installation_identity_is_required_only_for_machine_scope(scope, installation):
    with pytest.raises(ValueError):
        PreferenceLayer(scope, IDENTITY, installation_id=installation)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PreferenceIdentity("", "user", "workspace"),
        lambda: PreferenceIdentity("authority", " ", "workspace"),
        lambda: PreferenceIdentity("authority", "user", ""),
        lambda: PreferenceContext(IDENTITY, ""),
        lambda: PluginIdentity("claude", "", "tools"),
    ],
)
def test_missing_identifiers_are_rejected(factory):
    with pytest.raises(ValueError, match="non-empty"):
        factory()


def test_legacy_extraction_reuses_aliases_and_retains_order_without_local_only_fields():
    config = {
        "model": "model-a",
        "opencode_model": "model-b",
        "auth": {"api_key": "local-only"},
        "harness": {
            "default": "acp:custom-agent",
            "codex-native": {
                "command": "/local/wrapper",
                "args": ["--profile", "a", "--profile", "b", ""],
            },
        },
    }
    assert preferences_from_config(config) == {
        MODEL: "model-a",
        PreferenceKey(PreferenceKind.MODEL, "opencode_model"): "model-b",
        HARNESS: "acp:custom-agent",
        ARGS: ("--profile", "a", "--profile", "b", ""),
    }
    assert PreferenceKey(PreferenceKind.HARNESS_ARGS, "claude") == PreferenceKey(
        PreferenceKind.HARNESS_ARGS, "claude-sdk"
    )
    assert preferences_from_config({"harness": {"claude": {"args": []}}}) == {
        PreferenceKey(PreferenceKind.HARNESS_ARGS, "claude-sdk"): ()
    }


def test_legacy_extraction_keeps_scalar_harness_and_skips_malformed_values():
    assert preferences_from_config({"harness": "pi-native", "model": False}) == {
        HARNESS: "pi-native"
    }
    assert preferences_from_config({"harness": {"codex-native": {"args": "--bad"}}}) == {}
