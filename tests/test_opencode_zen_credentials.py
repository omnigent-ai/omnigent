"""Tests for OpenCode Zen API key resolution."""

from __future__ import annotations

import pytest

import omnigent.opencode_zen_credentials as zen


@pytest.fixture(autouse=True)
def _no_ambient(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the Zen env vars and stub the keychain to empty."""
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_OPENCODE_API_KEY", raising=False)
    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", lambda _name: None)


def test_resolves_canonical_env_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-canonical")
    monkeypatch.setenv("OMNIGENT_OPENCODE_API_KEY", "sk-prefixed")
    assert zen.resolve_opencode_zen_key() == ("env:OPENCODE_API_KEY", "sk-canonical")


def test_falls_back_to_omnigent_prefixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_OPENCODE_API_KEY", "sk-prefixed")
    assert zen.resolve_opencode_zen_key() == (
        "env:OMNIGENT_OPENCODE_API_KEY",
        "sk-prefixed",
    )


def test_blank_env_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "   ")
    assert zen.resolve_opencode_zen_key() is None


def test_env_value_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", " sk-pad \n")
    assert zen.resolve_opencode_zen_key() == ("env:OPENCODE_API_KEY", "sk-pad")


def test_falls_back_to_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret",
        lambda name: "sk-vault" if name == "opencode-zen" else None,
    )
    assert zen.resolve_opencode_zen_key() == ("keychain", "sk-vault")


def test_keychain_error_means_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_name: str) -> str | None:
        raise RuntimeError("keyring exploded")

    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", _boom)
    assert zen.resolve_opencode_zen_key() is None


def test_no_key_anywhere_is_none() -> None:
    assert zen.resolve_opencode_zen_key() is None


def test_explicit_environ_mapping_is_used() -> None:
    env = {"OPENCODE_API_KEY": "sk-explicit"}
    assert zen.resolve_opencode_zen_key(env) == ("env:OPENCODE_API_KEY", "sk-explicit")


def test_zen_spawn_env_stamps_resolved_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret",
        lambda name: "sk-vault" if name == "opencode-zen" else None,
    )
    assert zen.zen_spawn_env() == {"OPENCODE_API_KEY": "sk-vault"}


def test_zen_spawn_env_empty_when_unresolved() -> None:
    assert zen.zen_spawn_env() == {}
