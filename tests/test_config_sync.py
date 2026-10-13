"""Which user settings travel between a user's hosts, and how they merge."""

from __future__ import annotations

import json

import pytest

from omnigent.config_sync import (
    MAX_SYNCED_SETTINGS_BYTES,
    apply_synced_settings,
    synced_settings,
    validate_synced_settings,
)

_LOCAL_CONFIG = {
    "model": "gpt-5",
    "opencode_model": "openai/gpt-5",
    "auto_open_conversation": False,
    "default_agent": "/home/me/agents/dev.yaml",
    "server": "https://omnigent.example.test",
    "providers": {"work": {"kind": "key", "openai": {"api_key": "SECRET"}}},
    "host": {"host_id": "abc", "name": "laptop"},
    "github": {"accounts": {"/home/me/repo": "me"}},
    "harness": {
        "default": "codex",
        "codex-native": {"command": "/opt/codex", "args": ["--profile", "work"]},
        "claude": {"args": ["--verbose"]},
    },
}


def test_synced_settings_keeps_only_portable_keys() -> None:
    assert synced_settings(_LOCAL_CONFIG) == {
        "model": "gpt-5",
        "opencode_model": "openai/gpt-5",
        "auto_open_conversation": False,
        "harness": {
            "default": "codex",
            "codex-native": {"args": ["--profile", "work"]},
            "claude-sdk": {"args": ["--verbose"]},
        },
    }


@pytest.mark.parametrize(
    "harness,expected",
    [
        ("claude-sdk", {"harness": {"default": "claude-sdk"}}),
        ({"codex-native": {"command": "/opt/codex"}}, {}),
        ({"codex-native": {"args": "--not-a-list"}}, {}),
        ({"codex-native": {"args": ["ok", 7]}}, {}),
        ({"default": 3, "codex-native": None}, {}),
        (["codex"], {}),
    ],
)
def test_synced_settings_tolerates_legacy_and_malformed_harness(
    harness: object, expected: dict[str, object]
) -> None:
    assert synced_settings({"harness": harness, "model": ""}) == expected


def test_apply_replaces_portable_keys_and_preserves_the_rest() -> None:
    merged = apply_synced_settings(
        _LOCAL_CONFIG,
        {
            "model": "claude-opus",
            "harness": {"default": "claude-sdk", "codex-native": {"args": ["--profile", "home"]}},
        },
    )
    assert merged == {
        "model": "claude-opus",
        "default_agent": "/home/me/agents/dev.yaml",
        "server": "https://omnigent.example.test",
        "providers": _LOCAL_CONFIG["providers"],
        "host": _LOCAL_CONFIG["host"],
        "github": _LOCAL_CONFIG["github"],
        "harness": {
            "default": "claude-sdk",
            "codex-native": {"command": "/opt/codex", "args": ["--profile", "home"]},
        },
    }
    assert synced_settings(merged) == {
        "model": "claude-opus",
        "harness": {"default": "claude-sdk", "codex-native": {"args": ["--profile", "home"]}},
    }


def test_apply_empty_settings_removes_portable_keys_only() -> None:
    merged = apply_synced_settings(_LOCAL_CONFIG, {})
    assert "model" not in merged
    assert "opencode_model" not in merged
    assert "auto_open_conversation" not in merged
    assert merged["harness"] == {"codex-native": {"command": "/opt/codex"}}
    assert merged["providers"] == _LOCAL_CONFIG["providers"]
    assert apply_synced_settings({"harness": "codex"}, {}) == {}


@pytest.mark.parametrize(
    "local_id,synced_id",
    [("claude", "claude-sdk"), ("claude-sdk", "claude")],
    ids=["local-alias", "synced-alias"],
)
def test_apply_merges_alias_and_canonical_harness_entries(local_id: str, synced_id: str) -> None:
    merged = apply_synced_settings(
        {"harness": {local_id: {"command": "/opt/claude", "args": ["old"]}}},
        {"harness": {synced_id: {"args": ["new"]}}},
    )
    assert merged == {"harness": {"claude-sdk": {"command": "/opt/claude", "args": ["new"]}}}


def test_validate_accepts_extracted_settings() -> None:
    settings = synced_settings(_LOCAL_CONFIG)
    assert validate_synced_settings(settings) == settings
    assert validate_synced_settings({}) == {}


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"providers": {}},
        {"host": {"host_id": "abc"}},
        {"default_agent": "/path"},
        {"model": ""},
        {"model": 5},
        {"auto_open_conversation": "yes"},
        {"harness": "codex"},
        {"harness": {"default": ""}},
        {"harness": {"codex-native": {"command": "/opt/codex"}}},
        {"harness": {"codex-native": {"args": ["ok", 1]}}},
        {"harness": {"codex-native": {}}},
        {"harness": {"": {"args": []}}},
        {"model": "m" * (MAX_SYNCED_SETTINGS_BYTES + 1)},
    ],
)
def test_validate_rejects_non_portable_or_malformed_settings(value: object) -> None:
    with pytest.raises(ValueError):
        validate_synced_settings(value)


def test_validate_canonicalizes_and_merges_alias_harness_ids() -> None:
    assert validate_synced_settings(
        {"harness": {"default": "codex", "claude": {"args": ["a"]}, "claude-sdk": {"args": ["b"]}}}
    ) == {"harness": {"claude-sdk": {"args": ["b"]}, "default": "codex"}}


def test_validate_measures_the_size_cap_in_bytes() -> None:
    with pytest.raises(ValueError, match="exceed"):
        validate_synced_settings({"model": "é" * (MAX_SYNCED_SETTINGS_BYTES // 2 + 1)})


def test_validate_size_cap_is_an_exact_byte_boundary() -> None:
    padding = MAX_SYNCED_SETTINGS_BYTES - len(json.dumps({"model": ""}).encode("utf-8"))
    assert validate_synced_settings({"model": "x" * padding}) == {"model": "x" * padding}
    with pytest.raises(ValueError, match="exceed"):
        validate_synced_settings({"model": "x" * (padding + 1)})


def test_apply_ignores_non_portable_harness_fields_even_without_validation() -> None:
    merged = apply_synced_settings(
        {}, {"harness": {"codex-native": {"args": ["ok"], "command": "/evil/codex"}}}
    )
    assert merged == {"harness": {"codex-native": {"args": ["ok"]}}}
