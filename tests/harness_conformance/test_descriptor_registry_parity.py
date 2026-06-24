"""Conformance: scattered harness registries derive from descriptors.

These drift tests are the safety net for the unified
:class:`~omnigent.runtime.harness_descriptors.HarnessDescriptor` registry:
they assert every scattered view (runtime module map, spec allowlist,
aliases, native set, model-override support, CLI install metadata, native
web/wrapper agents) agrees with the single descriptor source of truth, so
adding or changing a harness can never silently desync the views.
"""

from __future__ import annotations

import importlib

import pytest

from omnigent.harness_aliases import (
    HARNESS_ALIASES,
    NATIVE_HARNESSES,
    canonicalize_harness,
    is_native_harness,
)
from omnigent.model_override import harness_supports_model_override
from omnigent.onboarding.harness_install import (
    _HARNESS_NAME_TO_KEY,
    required_cli_for_harness,
)
from omnigent.runtime.harness_descriptors import (
    HARNESS_DESCRIPTORS,
    all_harness_aliases,
    canonical_harness_ids,
    cli_backed_descriptors,
    native_harness_ids,
    native_ui_descriptors,
    runtime_module_map,
)
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.spec._omnigent_compat import OMNIGENT_HARNESS_ALIASES, OMNIGENT_HARNESSES

_DESCRIPTORS = list(HARNESS_DESCRIPTORS.values())


def test_descriptors_are_complete() -> None:
    """Every descriptor carries the required identity fields."""
    for harness_id, descriptor in HARNESS_DESCRIPTORS.items():
        assert descriptor.id == harness_id, "registry key must equal descriptor.id"
        assert descriptor.display_name
        assert descriptor.module
        assert descriptor.family


def test_descriptor_ids_are_canonical_not_aliases() -> None:
    """No descriptor id is itself a registered alias of another harness."""
    aliases = all_harness_aliases()
    for descriptor in _DESCRIPTORS:
        assert descriptor.id not in aliases, f"{descriptor.id} is both an id and an alias"


def test_runtime_registry_matches_descriptors() -> None:
    """``_HARNESS_MODULES`` equals the descriptor-derived module map."""
    assert runtime_module_map() == _HARNESS_MODULES


def test_runtime_modules_import_and_expose_create_app() -> None:
    """Every runtime-registered descriptor module exposes ``create_app``."""
    for descriptor in _DESCRIPTORS:
        if not descriptor.runtime_registered:
            continue
        module = importlib.import_module(descriptor.module)
        assert hasattr(module, "create_app"), f"{descriptor.module} missing create_app"


def test_spec_allowlist_matches_descriptors() -> None:
    """``OMNIGENT_HARNESSES`` equals the canonical descriptor id set."""
    assert canonical_harness_ids() == OMNIGENT_HARNESSES
    assert all_harness_aliases() == OMNIGENT_HARNESS_ALIASES


def test_aliases_canonicalize_to_descriptor_ids() -> None:
    """Every registered alias canonicalizes to a real descriptor id."""
    for alias, target in HARNESS_ALIASES.items():
        assert target in HARNESS_DESCRIPTORS
        assert canonicalize_harness(alias) == target


def test_native_set_matches_descriptors() -> None:
    """``NATIVE_HARNESSES`` and ``is_native_harness`` agree with descriptors."""
    assert native_harness_ids() == NATIVE_HARNESSES
    for descriptor in _DESCRIPTORS:
        assert is_native_harness(descriptor.id) == descriptor.is_native


def test_model_override_support_matches_descriptors() -> None:
    """``harness_supports_model_override`` agrees with each descriptor flag."""
    for descriptor in _DESCRIPTORS:
        assert harness_supports_model_override(descriptor.id) == descriptor.supports_model_override


def test_install_metadata_matches_descriptors() -> None:
    """CLI-backed descriptors map to a real install spec with a binary."""
    for descriptor in cli_backed_descriptors():
        assert descriptor.cli_binary
        assert descriptor.npm_package or descriptor.install_hint
        assert _HARNESS_NAME_TO_KEY.get(descriptor.id) == descriptor.install_family_key
        spec = required_cli_for_harness(descriptor.id)
        assert spec is not None, f"no install spec for {descriptor.id}"
        assert spec.binary == descriptor.cli_binary


def test_install_name_to_key_only_covers_cli_backed_descriptors() -> None:
    """Every ``_HARNESS_NAME_TO_KEY`` entry maps to a cli-backed descriptor."""
    cli_ids = {d.id for d in cli_backed_descriptors()}
    for harness_id in _HARNESS_NAME_TO_KEY:
        assert harness_id in cli_ids, f"{harness_id} in name-to-key but not cli-backed descriptor"


def test_native_ui_descriptors_have_wrapper_metadata() -> None:
    """Native-UI descriptors carry wrapper + terminal-first web metadata."""
    for descriptor in native_ui_descriptors():
        assert descriptor.wrapper_agent_name
        assert descriptor.wrapper_label
        assert descriptor.terminal_name
        assert descriptor.web_icon_kind
        assert descriptor.terminal_first


def test_native_ui_descriptors_match_native_coding_agents() -> None:
    """The Python native_coding_agents registry matches native-UI descriptors."""
    from omnigent.native_coding_agents import NATIVE_CODING_AGENTS

    by_harness = {agent.harness: agent for agent in NATIVE_CODING_AGENTS}
    for descriptor in native_ui_descriptors():
        agent = by_harness.get(descriptor.id)
        assert agent is not None, f"no native_coding_agents entry for {descriptor.id}"
        assert agent.agent_name == descriptor.wrapper_agent_name
        assert agent.wrapper_label == descriptor.wrapper_label
        assert agent.terminal_name == descriptor.terminal_name


def test_opencode_descriptor_is_registered() -> None:
    """The opencode-native descriptor exists with the expected shape."""
    descriptor = HARNESS_DESCRIPTORS.get("opencode-native")
    assert descriptor is not None
    assert descriptor.family == "native-server"
    assert descriptor.transport_kind == "http-sse"
    assert descriptor.cli_binary == "opencode"
    assert descriptor.npm_package == "opencode-ai"
    assert descriptor.supports_model_override is True
    assert descriptor.supports_terminal_takeover is True


@pytest.mark.parametrize("alias", ["native-opencode", "opencode"])
def test_opencode_aliases_resolve(alias: str) -> None:
    """OpenCode aliases canonicalize to the opencode-native id."""
    assert canonicalize_harness(alias) == "opencode-native"


def test_vendored_openapi_schemas_exist_and_parse() -> None:
    """A descriptor naming an ``openapi_schema`` points at a real, valid file.

    The native-server client is hand-shaped from the pinned vendor OpenAPI,
    so the fixture must actually ship in-tree (resolved relative to the
    ``omnigent`` package root) — otherwise the descriptor reference is a dead
    string and the client can silently drift from the documented contract.
    """
    import json
    from pathlib import Path

    import omnigent

    pkg_root = Path(omnigent.__file__).resolve().parent
    for descriptor in _DESCRIPTORS:
        if descriptor.openapi_schema is None:
            continue
        schema_path = pkg_root / descriptor.openapi_schema
        assert schema_path.is_file(), (
            f"{descriptor.id} declares openapi_schema={descriptor.openapi_schema!r} "
            f"but {schema_path} does not exist"
        )
        with schema_path.open() as fh:
            doc = json.load(fh)
        assert doc.get("openapi"), f"{schema_path} is not a valid OpenAPI document"
        assert doc.get("paths"), f"{schema_path} declares no paths"
