"""Inference policy resolution, immutable snapshots, and session isolation."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inference_config import (
    binding_for_harness,
    inference_config_scope,
    inference_revision,
    load_runtime_inference_config,
    parse_inference_config,
    resolve_bound_model,
    resolve_bound_provider,
    snapshot_runtime_config,
    validate_bound_agent_model,
)
from omnigent.spec.types import ApiKeyAuth, DatabricksAuth, ProviderAuth


@pytest.fixture(autouse=True)
def isolated_runtime_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_INFERENCE_CONFIG", raising=False)
    monkeypatch.setattr("omnigent.onboarding.provider_config._load_config", dict)


def _config(**bindings: dict[str, Any]) -> dict[str, Any]:
    return {
        "providers": {
            name: {
                "kind": "gateway",
                "openai": {
                    "base_url": f"https://{name}.example/v1",
                    "api_key_ref": f"env:INFERENCE_TEST_{name.upper()}_KEY",
                    "wire_api": "responses",
                    "models": {"default": "model-a", "fast": "model-b"},
                },
            }
            for name in ("bifrost", "unity")
        },
        "inference": {"harnesses": bindings},
    }


def test_native_and_sdk_bindings_do_not_share_provider_defaults() -> None:
    config = _config(
        **{
            "codex-native": {"provider": "unity"},
            "codex": {"provider": "bifrost"},
        }
    )
    for harness, expected in (("codex-native", "unity"), ("codex", "bifrost")):
        binding = binding_for_harness(config, harness)
        assert binding is not None
        assert binding.provider == expected
    assert binding_for_harness(config, "claude-native") is None


def test_native_alias_resolves_without_collapsing_to_sdk() -> None:
    config = _config(**{"native-codex": {"provider": "unity"}})
    assert binding_for_harness(config, "codex-native") == binding_for_harness(
        config, "native-codex"
    )
    assert binding_for_harness(config, "codex-native") is not None
    assert binding_for_harness(config, "codex") is None


def test_exact_acp_binding_precedes_only_explicit_generic_fallback() -> None:
    config = _config(
        **{
            "acp": {"provider": "bifrost"},
            "acp:custom": {"provider": "unity"},
            "jcode": {"provider": "unity"},
        }
    )
    for harness, expected in (
        ("acp:custom", "unity"),
        ("acp:other", "bifrost"),
        ("acp", "bifrost"),
        ("jcode", "unity"),
    ):
        binding = binding_for_harness(config, harness)
        assert binding is not None
        assert binding.provider == expected
    del config["inference"]["harnesses"]["acp"]
    assert binding_for_harness(config, "acp:other") is None
    assert binding_for_harness(config, "pi-native") is None


@pytest.mark.parametrize("config", [{}, {"inference": None}, {"inference": {}}])
def test_missing_inference_preserves_legacy_model_selection(config: dict[str, Any]) -> None:
    assert parse_inference_config(config) == {}
    assert resolve_bound_provider(config, "codex", ProviderAuth(name="legacy")) is None
    assert resolve_bound_model(config, "codex", "private/model-v1") == "private/model-v1"
    assert resolve_bound_model(config, "codex", None) is None


def test_missing_allowlist_leaves_models_unrestricted() -> None:
    config = _config(codex={"provider": "bifrost"})
    binding = binding_for_harness(config, "codex")
    assert binding is not None
    assert binding.model_allowlist is None
    assert resolve_bound_model(config, "codex", "private/model-v1") == "private/model-v1"
    assert resolve_bound_model(config, "codex", None) == "model-a"


def test_empty_allowlist_disables_provider_and_all_model_choices() -> None:
    config = _config(codex={"provider": "bifrost", "model_allowlist": []})
    binding = binding_for_harness(config, "codex")
    assert binding is not None
    assert binding.model_allowlist == ()
    with pytest.raises(OmnigentError, match="no allowed models") as exc:
        resolve_bound_provider(config, "codex")
    assert exc.value.code == ErrorCode.INVALID_INPUT
    for model in (None, "model-a", "private/model-v1"):
        with pytest.raises(OmnigentError):
            resolve_bound_model(config, "codex", model)


def test_singleton_allowlist_remains_restricted_and_keeps_literal_id() -> None:
    model = "private.catalog/model-v1"
    config = _config(
        codex={"provider": "bifrost", "default_model": model, "model_allowlist": [model]}
    )
    assert resolve_bound_model(config, "codex", None) == model
    assert resolve_bound_model(config, "codex", model) == model
    with pytest.raises(OmnigentError, match="configured model list"):
        resolve_bound_model(config, "codex", "model-a")
    provider = resolve_bound_provider(config, "codex")
    assert provider is not None
    assert set(provider.families["openai"].models.values()) == {model}


def test_allowlist_retains_operator_order_without_duplicate_models() -> None:
    config = _config(
        codex={"provider": "bifrost", "model_allowlist": ["model-b", "model-a", "model-b"]}
    )
    binding = binding_for_harness(config, "codex")
    assert binding is not None
    assert binding.model_allowlist == ("model-b", "model-a")
    assert config["inference"]["harnesses"]["codex"]["model_allowlist"] == [
        "model-b",
        "model-a",
        "model-b",
    ]


def test_allowlist_preserves_compatible_provider_default() -> None:
    config = _config(codex={"provider": "bifrost", "model_allowlist": ["model-b", "model-a"]})
    assert resolve_bound_model(config, "codex", None) == "model-a"


@pytest.mark.parametrize(
    ("harness", "expected"), [("claude-native", "anthropic-default"), ("codex", "model-a")]
)
def test_provider_default_uses_harness_family(harness: str, expected: str) -> None:
    config = _config(**{harness: {"provider": "bifrost"}})
    config["providers"]["bifrost"]["anthropic"] = {
        "base_url": "https://bifrost.example/anthropic",
        "api_key_ref": "env:INFERENCE_TEST_BIFROST_KEY",
        "models": {"default": "anthropic-default"},
    }
    assert resolve_bound_model(config, harness, None) == expected


def test_bound_models_do_not_mutate_shared_provider_or_other_harness() -> None:
    config = _config(
        codex={"provider": "bifrost", "default_model": "model-b", "model_allowlist": ["model-b"]},
        **{"codex-native": {"provider": "bifrost"}},
    )
    before = copy.deepcopy(config)
    selected = resolve_bound_provider(config, "codex")
    unfiltered = resolve_bound_provider(config, "codex-native")
    assert selected is not None and unfiltered is not None
    assert selected.families["openai"].default_model == "model-b"
    assert unfiltered.families["openai"].default_model == "model-a"
    assert set(selected.families["openai"].models.values()) == {"model-b"}
    assert set(unfiltered.families["openai"].models.values()) == {"model-a", "model-b"}
    assert config == before


@pytest.mark.parametrize(
    "inference",
    [
        [],
        "codex",
        {"unknown": {}},
        {"harnesses": []},
        {"harnesses": {"": {"provider": "bifrost"}}},
        {"harnesses": {" ": {"provider": "bifrost"}}},
        {"harnesses": {1: {"provider": "bifrost"}}},
        {"harnesses": {"codex": "bifrost"}},
    ],
)
def test_rejects_invalid_configuration_shape(inference: object) -> None:
    config = _config()
    config["inference"] = inference
    with pytest.raises(ValueError):
        parse_inference_config(config)


@pytest.mark.parametrize(
    "binding",
    [
        {},
        {"provider": "missing"},
        {"provider": 1},
        {"provider": "bifrost", "unknown": True},
        {"provider": "bifrost", "default_model": ""},
        {"provider": "bifrost", "default_model": "  "},
        {"provider": "bifrost", "default_model": 1},
        {"provider": "bifrost", "model_allowlist": "model-a"},
        {"provider": "bifrost", "model_allowlist": [""]},
        {"provider": "bifrost", "model_allowlist": ["  "]},
        {"provider": "bifrost", "model_allowlist": [1]},
        {"provider": "bifrost", "default_model": "model-a", "model_allowlist": []},
        {"provider": "bifrost", "default_model": "model-a", "model_allowlist": ["model-b"]},
    ],
)
def test_rejects_invalid_binding_and_defaults(binding: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_inference_config(_config(codex=binding))


def test_rejects_conflicting_alias_bindings() -> None:
    config = _config(
        **{
            "native-codex": {"provider": "bifrost"},
            "codex-native": {"provider": "unity"},
        }
    )
    with pytest.raises(ValueError, match="Duplicate inference binding"):
        parse_inference_config(config)


def test_rejects_nonmapping_providers() -> None:
    config = _config(codex={"provider": "bifrost"})
    config["providers"] = []
    with pytest.raises(ValueError, match="providers must be a mapping"):
        parse_inference_config(config)


def test_unavailable_bound_provider_does_not_fall_back() -> None:
    config = _config(codex={"provider": "future-cli"})
    config["providers"]["future-cli"] = {"kind": "cli-config", "cli": "future"}
    with pytest.raises(OmnigentError, match="unavailable"):
        resolve_bound_provider(config, "codex")


@pytest.mark.parametrize(
    "auth",
    [
        ProviderAuth(name="unity"),
        ApiKeyAuth(api_key="synthetic-test-token", base_url="https://other.example/v1"),
        DatabricksAuth(profile="synthetic-profile"),
        {"type": "provider", "name": "bifrost"},
    ],
)
def test_authoritative_binding_rejects_conflicting_auth(auth: object) -> None:
    config = _config(codex={"provider": "bifrost"})
    with pytest.raises(OmnigentError, match="authentication conflicts") as exc:
        resolve_bound_provider(config, "codex", auth)
    assert exc.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("auth", [None, ProviderAuth(name="bifrost")])
def test_matching_provider_auth_preserves_authoritative_binding(auth: object) -> None:
    config = _config(codex={"provider": "bifrost"})
    provider = resolve_bound_provider(config, "codex", auth)
    assert provider is not None
    assert provider.name == "bifrost"
    assert provider.families["openai"].api_key_ref == "env:INFERENCE_TEST_BIFROST_KEY"


def test_nested_runtime_scopes_restore_configuration_after_exception() -> None:
    base = _config(codex={"provider": "bifrost"})
    outer = _config(codex={"provider": "unity"})
    with inference_config_scope(outer):
        assert load_runtime_inference_config(base)["inference"] == outer["inference"]
        with pytest.raises(RuntimeError), inference_config_scope({}):
            assert load_runtime_inference_config(base)["providers"] == {}
            raise RuntimeError("simulated cancelled operation")
        assert load_runtime_inference_config(base)["inference"] == outer["inference"]
    assert load_runtime_inference_config(base) == base


async def test_runtime_configuration_is_isolated_across_concurrent_tasks() -> None:
    started = {name: asyncio.Event() for name in ("bifrost", "unity")}

    async def resolve(name: str, other: str) -> str:
        with inference_config_scope(_config(codex={"provider": name})):
            started[name].set()
            await started[other].wait()
            binding = binding_for_harness(load_runtime_inference_config(), "codex")
            assert binding is not None
            return binding.provider

    result = await asyncio.gather(resolve("bifrost", "unity"), resolve("unity", "bifrost"))
    assert result == ["bifrost", "unity"]
    assert load_runtime_inference_config() == {}


def test_saved_file_overlays_only_inference_and_preserves_ambient_host_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = _config(codex={"provider": "unity"})
    saved["unrelated"] = "saved-value"
    path = tmp_path / "session-inference.json"
    path.write_text(json.dumps(saved), encoding="utf-8")
    monkeypatch.setenv("OMNIGENT_INFERENCE_CONFIG", str(path))
    base = _config(codex={"provider": "bifrost"})
    base["unrelated"] = "ambient-value"
    loaded = load_runtime_inference_config(base)
    assert loaded["providers"] == saved["providers"]
    assert loaded["inference"] == saved["inference"]
    assert loaded["unrelated"] == "ambient-value"
    assert base["inference"]["harnesses"]["codex"]["provider"] == "bifrost"


def test_empty_snapshot_clears_ambient_routing_instead_of_inheriting_new_settings() -> None:
    base = _config(codex={"provider": "bifrost"})
    with inference_config_scope({}):
        assert load_runtime_inference_config(base) == {"providers": {}, "inference": {}}


def test_scoped_config_takes_precedence_over_saved_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_INFERENCE_CONFIG", str(tmp_path / "missing.json"))
    snapshot = _config(codex={"provider": "unity"})
    with inference_config_scope(snapshot):
        assert load_runtime_inference_config() == snapshot


@pytest.mark.parametrize("contents", [None, "{", "[]", "null", '"not-a-config"'])
def test_invalid_saved_file_fails_closed(
    contents: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "invalid-session.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    monkeypatch.setenv("OMNIGENT_INFERENCE_CONFIG", str(path))
    with pytest.raises(OmnigentError, match="saved inference configuration") as exc:
        load_runtime_inference_config(_config(codex={"provider": "bifrost"}))
    assert exc.value.code == ErrorCode.INVALID_INPUT


def test_runtime_overlay_and_snapshot_return_independent_copies() -> None:
    runtime = _config(codex={"provider": "unity", "model_allowlist": ["model-a"]})
    snapshot = {
        "runtime_config": runtime,
        "model_discovery": {"unity": {"api_key_ref": "env:CATALOG_ONLY_KEY"}},
    }
    copied = snapshot_runtime_config(snapshot)
    assert copied is not None
    assert copied == runtime
    assert "model_discovery" not in copied
    with inference_config_scope(copied):
        loaded = load_runtime_inference_config()
        loaded["providers"].clear()
        assert load_runtime_inference_config()["providers"] == runtime["providers"]
    copied["providers"].clear()
    assert runtime["providers"]
    assert snapshot_runtime_config(None) is None


def test_revision_is_stable_across_mapping_order_and_unrelated_settings() -> None:
    config = _config(codex={"provider": "bifrost", "model_allowlist": ["model-b", "model-a"]})
    discovery = {"bifrost": {"base_url": "https://catalog.example/v1", "timeout_s": 5}}
    reordered = {key: config[key] for key in reversed(config)}
    reordered["unrelated"] = {"host": "different"}
    reordered_discovery = {"bifrost": dict(reversed(list(discovery["bifrost"].items())))}
    assert inference_revision(config, discovery) == inference_revision(
        reordered, reordered_discovery
    )


@pytest.mark.parametrize(
    ("section", "path", "replacement"),
    [
        ("config", ("providers", "bifrost", "openai", "base_url"), "https://changed.example/v1"),
        ("config", ("providers", "bifrost", "openai", "api_key_ref"), "env:OTHER_KEY"),
        ("config", ("providers", "bifrost", "openai", "wire_api"), "chat"),
        ("config", ("providers", "bifrost", "openai", "models", "default"), "model-b"),
        ("config", ("inference", "harnesses", "codex", "provider"), "unity"),
        ("config", ("inference", "harnesses", "codex", "default_model"), "model-b"),
        ("config", ("inference", "harnesses", "codex", "model_allowlist"), ["model-b", "model-a"]),
        ("discovery", ("bifrost", "base_url"), "https://other-catalog.example/v1"),
        ("discovery", ("bifrost", "api_key_ref"), "env:OTHER_CATALOG_KEY"),
    ],
)
def test_revision_changes_with_routing_or_discovery_inputs(
    section: str, path: tuple[str, ...], replacement: object
) -> None:
    config = _config(
        codex={
            "provider": "bifrost",
            "default_model": "model-a",
            "model_allowlist": ["model-a", "model-b"],
        }
    )
    discovery = {
        "bifrost": {"base_url": "https://catalog.example/v1", "api_key_ref": "env:CATALOG_KEY"}
    }
    before = inference_revision(config, discovery)
    target = config if section == "config" else discovery
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    assert inference_revision(config, discovery) != before


@pytest.mark.parametrize("harness", ["claude-native", "native-claude", "acp:custom", "acp:other"])
@pytest.mark.parametrize("pin", [None, "model-a", "obsolete-model"])
def test_agent_pin_policy(harness, pin):
    binding = {"provider": "bifrost", "model_allowlist": ["model-a"]}
    config = _config(**{"claude-native": binding, "acp": binding, "acp:custom": binding})
    if pin == "obsolete-model":
        with pytest.raises(OmnigentError, match=r"Remove executor\.model") as exc:
            validate_bound_agent_model(config, harness, pin)
        assert exc.value.code == ErrorCode.INVALID_INPUT
        assert pin in str(exc.value)
    else:
        validate_bound_agent_model(config, harness, pin)


def test_agent_pin_policy_only_restricts_explicit_allowlists():
    validate_bound_agent_model({}, "codex", "obsolete-model")
    validate_bound_agent_model(_config(codex={"provider": "bifrost"}), "codex", "obsolete-model")
    config = _config(codex={"provider": "bifrost", "model_allowlist": []})
    validate_bound_agent_model(config, "pi", "obsolete-model")
    validate_bound_agent_model(config, "codex", None)
    with pytest.raises(OmnigentError, match="obsolete-model"):
        validate_bound_agent_model(config, "codex", "obsolete-model")


def test_agent_pin_uses_exact_acp_policy_before_generic_fallback():
    config = _config(
        **{
            "acp": {"provider": "bifrost", "model_allowlist": ["generic"]},
            "acp:custom": {"provider": "bifrost", "model_allowlist": ["custom"]},
        }
    )
    validate_bound_agent_model(config, "acp:custom", "custom")
    validate_bound_agent_model(config, "acp:other", "generic")
    with pytest.raises(OmnigentError):
        validate_bound_agent_model(config, "acp:custom", "generic")
