"""Unit tests for the goose-native spawn-env builder."""

from __future__ import annotations

from omnigent.goose_native_bridge import BRIDGE_DIR_ENV_VAR, build_goose_native_spawn_env


def test_spawn_env_sets_bridge_dir_and_ansi_theme() -> None:
    env = build_goose_native_spawn_env("sess-1")
    assert env[BRIDGE_DIR_ENV_VAR].endswith("goose-native") is False  # it's <root>/<hash>
    assert "goose-native/" in env[BRIDGE_DIR_ENV_VAR]
    assert env["GOOSE_CLI_THEME"] == "ansi"
    # No provider/model unless asked.
    assert "GOOSE_PROVIDER" not in env
    assert "GOOSE_MODEL" not in env


def test_spawn_env_pins_provider_and_model_when_given() -> None:
    env = build_goose_native_spawn_env("sess-2", provider="openrouter", model="openai/gpt-4o-mini")
    assert env["GOOSE_PROVIDER"] == "openrouter"
    assert env["GOOSE_MODEL"] == "openai/gpt-4o-mini"


def test_spawn_env_bridge_dir_is_deterministic_per_session() -> None:
    a = build_goose_native_spawn_env("same")[BRIDGE_DIR_ENV_VAR]
    b = build_goose_native_spawn_env("same")[BRIDGE_DIR_ENV_VAR]
    c = build_goose_native_spawn_env("other")[BRIDGE_DIR_ENV_VAR]
    assert a == b and a != c
