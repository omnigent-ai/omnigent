"""Completeness + consistency guard for the harness registry (Phase 0).

The registry is a typed *view* derived from the existing harness constants, so
these tests do not check hand-copied data; they assert the view stays complete
against every source it derives from. If a future harness lands in one of those
constants (``_HARNESS_MODULES``, ``NATIVE_CODING_AGENTS``, ``OMNIGENT_HARNESSES``,
the alias maps, ``_HARNESS_NAME_TO_KEY``) without the others, one of these fails
loudly — which is the drift the registry exists to prevent.
"""

from __future__ import annotations

from omnigent.harness_aliases import HARNESS_ALIASES, NATIVE_HARNESSES, is_native_harness
from omnigent.harnesses import REGISTRY, all_descriptors, get, render_matrix
from omnigent.harnesses.capabilities import IntegrationMode, ModelFamily, Resume
from omnigent.model_override import (
    _ANTIGRAVITY_FAMILY_HARNESSES,
    _CLAUDE_FAMILY_HARNESSES,
    _CODEX_FAMILY_HARNESSES,
    harness_supports_model_override,
)
from omnigent.native_coding_agents import NATIVE_CODING_AGENTS
from omnigent.onboarding.harness_install import _HARNESS_NAME_TO_KEY
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.spec._omnigent_compat import OMNIGENT_HARNESS_ALIASES, OMNIGENT_HARNESSES

_NATIVE_MODES = frozenset({IntegrationMode.NATIVE_TUI, IntegrationMode.NATIVE_SERVER})

# The one canonical harness that is intentionally resolved through an alternate
# path and has no ``_HARNESS_MODULES`` entry. Documented here so that a NEW
# harness missing its module registration fails ``test_every_canonical_has_a_module``
# instead of silently joining this exception.
_MODULELESS_HARNESSES = frozenset({"open-responses"})


def test_registry_names_are_exactly_the_spec_allowlist() -> None:
    assert set(REGISTRY) == set(OMNIGENT_HARNESSES)


def test_every_alias_resolves() -> None:
    # Both the spec-level and harness-level alias maps must resolve to a
    # descriptor, and to the same canonical the alias map declares.
    for alias, canonical in HARNESS_ALIASES.items():
        descriptor = get(alias)
        assert descriptor is not None, f"alias {alias!r} did not resolve"
        assert descriptor.name == canonical
    for alias in OMNIGENT_HARNESS_ALIASES:
        assert get(alias) is not None, f"spec alias {alias!r} did not resolve"


def test_alias_lists_never_contain_the_canonical_name() -> None:
    for descriptor in all_descriptors():
        assert descriptor.name not in descriptor.aliases


def test_every_harness_module_key_matches() -> None:
    # Every _HARNESS_MODULES key (canonical or alias) resolves to a descriptor
    # whose module path is the same value the registry would spawn.
    for name, module_path in _HARNESS_MODULES.items():
        descriptor = get(name)
        assert descriptor is not None, f"{name!r} in _HARNESS_MODULES but not the registry"
        assert descriptor.harness_module == module_path


def test_every_canonical_has_a_module() -> None:
    for descriptor in all_descriptors():
        if descriptor.name in _MODULELESS_HARNESSES:
            assert descriptor.harness_module is None
        else:
            assert descriptor.harness_module is not None, (
                f"{descriptor.name!r} has no _HARNESS_MODULES entry; add one or "
                f"add it to _MODULELESS_HARNESSES with a reason"
            )


def test_native_membership_matches_harness_aliases() -> None:
    # Every canonical/alias spelling the legacy predicate calls native must be
    # a native descriptor in the registry, and vice versa.
    for spelling in NATIVE_HARNESSES:
        descriptor = get(spelling)
        assert descriptor is not None, f"native spelling {spelling!r} did not resolve"
        assert descriptor.is_native, f"{spelling!r} resolved to a non-native descriptor"
    for descriptor in all_descriptors():
        if descriptor.is_native:
            assert is_native_harness(descriptor.name)


def test_native_metadata_is_the_same_object() -> None:
    # The registry reuses NativeCodingAgent objects rather than copying them.
    for agent in NATIVE_CODING_AGENTS:
        descriptor = get(agent.harness)
        assert descriptor is not None
        assert descriptor.native is agent


def test_install_family_key_matches() -> None:
    for name, family_key in _HARNESS_NAME_TO_KEY.items():
        descriptor = get(name)
        assert descriptor is not None, f"{name!r} in _HARNESS_NAME_TO_KEY but not the registry"
        assert descriptor.install_family_key == family_key


def test_model_override_matches_predicate() -> None:
    for descriptor in all_descriptors():
        assert descriptor.supports_model_override == harness_supports_model_override(
            descriptor.name
        )


def test_get_is_alias_insensitive() -> None:
    # A representative reversed native spelling and an SDK alias both resolve.
    assert get("native-kiro") is get("kiro-native")
    assert get("claude") is get("claude-sdk")
    assert get("native-antigravity") is get("antigravity-native")
    assert get(None) is None
    assert get("definitely-not-a-harness") is None


# ── Capability declarations (PR 2) ────────────────────────────────────────


def test_every_descriptor_declares_capabilities() -> None:
    for descriptor in all_descriptors():
        assert descriptor.capabilities is not None


def test_integration_mode_agrees_with_native_flag() -> None:
    # A native integration mode iff the descriptor carries native UI metadata.
    for descriptor in all_descriptors():
        is_native_mode = descriptor.capabilities.integration_mode in _NATIVE_MODES
        assert is_native_mode == descriptor.is_native, descriptor.name


def test_model_family_matches_model_override_sets() -> None:
    # model_family is derivable from model_override's family frozensets, so the
    # declaration must not contradict the code that actually enforces routing.
    for descriptor in all_descriptors():
        name = descriptor.name
        family = descriptor.capabilities.model_family
        if name in _CLAUDE_FAMILY_HARNESSES:
            assert family is ModelFamily.CLAUDE, name
        elif name in _CODEX_FAMILY_HARNESSES:
            assert family is ModelFamily.GPT, name
        elif name in _ANTIGRAVITY_FAMILY_HARNESSES:
            assert family is ModelFamily.GEMINI, name
        else:
            assert family is ModelFamily.MULTI, name


def test_subagents_matches_native_wrapper_label() -> None:
    # subagents is derivable: only native agents with a subagent_wrapper_label
    # can spawn Omnigent native sub-agents.
    subagent_capable = {
        agent.harness for agent in NATIVE_CODING_AGENTS if agent.subagent_wrapper_label
    }
    for descriptor in all_descriptors():
        expected = descriptor.name in subagent_capable
        assert descriptor.capabilities.subagents == expected, descriptor.name


def test_native_harnesses_resume_warm() -> None:
    # Every native harness reattaches to a live vendor session/terminal.
    for descriptor in all_descriptors():
        if descriptor.is_native:
            assert descriptor.capabilities.resume is Resume.WARM_REATTACH, descriptor.name


def test_matrix_renders_every_harness() -> None:
    table = render_matrix()
    for descriptor in all_descriptors():
        assert descriptor.name in table
    # Header + separator + one row per harness.
    assert len(table.splitlines()) == len(REGISTRY) + 2
