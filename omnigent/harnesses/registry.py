"""The harness registry — a typed view over the existing harness constants.

See :mod:`omnigent.harnesses` for the phased plan. In Phase 0 every field is
*derived* from an existing source of truth, so the registry cannot drift from
those constants; the accompanying test
(``tests/test_harness_registry.py``) asserts the derivation stays complete.
"""

from __future__ import annotations

from omnigent.harness_aliases import HARNESS_ALIASES
from omnigent.harnesses.capabilities import capabilities_for
from omnigent.harnesses.types import HarnessDescriptor
from omnigent.model_override import harness_supports_model_override
from omnigent.native_coding_agents import NativeCodingAgent, native_coding_agent_for_harness
from omnigent.onboarding.harness_install import _HARNESS_NAME_TO_KEY, harness_install_spec
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES

_NATIVE_SUFFIX = "-native"

# Cosmetic display names for the SDK harnesses that have neither native-UI
# metadata nor an install spec to borrow a label from. Purely presentational;
# not asserted by the completeness test.
_DISPLAY_OVERRIDES: dict[str, str] = {
    "claude-sdk": "Claude",
    "codex": "Codex",
    "openai-agents": "OpenAI Agents",
    "antigravity": "Antigravity",
    "cursor": "Cursor",
    "copilot": "GitHub Copilot",
    "open-responses": "Open Responses",
}


def _reversed_native_alias(name: str) -> str | None:
    """Return the ``native-<x>`` spelling for an ``<x>-native`` name, else None.

    The spec allowlist accepts reversed spellings (``native-antigravity``) that
    ``HARNESS_ALIASES`` does not always carry, so the registry derives them from
    the canonical native name to become the single complete resolver.
    """
    if name.endswith(_NATIVE_SUFFIX):
        return "native-" + name[: -len(_NATIVE_SUFFIX)]
    return None


def _build_alias_map(names: frozenset[str]) -> dict[str, str]:
    """Build alias -> canonical over both explicit aliases and reversed natives."""
    alias_to_canonical: dict[str, str] = dict(HARNESS_ALIASES)
    for name in names:
        reversed_alias = _reversed_native_alias(name)
        if reversed_alias is not None:
            # setdefault: an explicit HARNESS_ALIASES entry wins if one exists.
            alias_to_canonical.setdefault(reversed_alias, name)
    return alias_to_canonical


_ALIAS_TO_CANONICAL: dict[str, str] = _build_alias_map(OMNIGENT_HARNESSES)


def _aliases_for(name: str) -> tuple[str, ...]:
    """Return the sorted aliases that resolve to *name*."""
    return tuple(sorted(a for a, canonical in _ALIAS_TO_CANONICAL.items() if canonical == name))


def _display_name_for(
    name: str, native: NativeCodingAgent | None, install_family_key: str | None
) -> str:
    """Pick a human label: native meta, then install spec, then override, then name."""
    if native is not None:
        return native.display_name
    if install_family_key is not None:
        spec = harness_install_spec(install_family_key)
        if spec is not None:
            return spec.display
    return _DISPLAY_OVERRIDES.get(name, name)


def _build_registry() -> dict[str, HarnessDescriptor]:
    registry: dict[str, HarnessDescriptor] = {}
    for name in sorted(OMNIGENT_HARNESSES):
        native = native_coding_agent_for_harness(name)
        install_family_key = _HARNESS_NAME_TO_KEY.get(name)
        capabilities = capabilities_for(name)
        if capabilities is None:
            raise ValueError(
                f"harness {name!r} is in OMNIGENT_HARNESSES but has no entry in "
                f"omnigent.harnesses.capabilities._CAPABILITIES — declare its "
                f"capabilities there"
            )
        registry[name] = HarnessDescriptor(
            name=name,
            aliases=_aliases_for(name),
            display_name=_display_name_for(name, native, install_family_key),
            harness_module=_HARNESS_MODULES.get(name),
            native=native,
            install_family_key=install_family_key,
            supports_model_override=harness_supports_model_override(name),
            capabilities=capabilities,
        )
    return registry


REGISTRY: dict[str, HarnessDescriptor] = _build_registry()


def get(name_or_alias: str | None) -> HarnessDescriptor | None:
    """Return the descriptor for a canonical name or any accepted alias.

    :param name_or_alias: A harness spelling, e.g. ``"kiro-native"``,
        ``"native-kiro"``, ``"claude"``, or ``None``.
    :returns: The matching :class:`HarnessDescriptor`, or ``None`` if the
        spelling is not a known harness.
    """
    if not name_or_alias:
        return None
    canonical = _ALIAS_TO_CANONICAL.get(name_or_alias, name_or_alias)
    return REGISTRY.get(canonical)


def all_descriptors() -> tuple[HarnessDescriptor, ...]:
    """Return every descriptor, ordered by canonical name."""
    return tuple(REGISTRY.values())
