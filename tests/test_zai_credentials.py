"""Tests for Z.AI (Zhipu) API key resolution."""

from __future__ import annotations

import pytest

import omnigent.zai_credentials as zai


@pytest.fixture(autouse=True)
def _no_ambient(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the Z.AI env vars and stub the keychain to empty."""
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_ZHIPU_API_KEY", raising=False)
    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", lambda _name: None)


def test_resolves_canonical_env_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZHIPU_API_KEY", "zai-canonical")
    monkeypatch.setenv("OMNIGENT_ZHIPU_API_KEY", "zai-prefixed")
    assert zai.resolve_zai_key() == ("env:ZHIPU_API_KEY", "zai-canonical")


def test_falls_back_to_omnigent_prefixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_ZHIPU_API_KEY", "zai-prefixed")
    assert zai.resolve_zai_key() == (
        "env:OMNIGENT_ZHIPU_API_KEY",
        "zai-prefixed",
    )


def test_blank_env_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZHIPU_API_KEY", "   ")
    assert zai.resolve_zai_key() is None


def test_env_value_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZHIPU_API_KEY", " zai-pad \n")
    assert zai.resolve_zai_key() == ("env:ZHIPU_API_KEY", "zai-pad")


def test_falls_back_to_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret",
        lambda name: "zai-vault" if name == "zai-coding-plan" else None,
    )
    assert zai.resolve_zai_key() == ("keychain", "zai-vault")


def test_keychain_error_means_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_name: str) -> str | None:
        raise RuntimeError("keyring exploded")

    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", _boom)
    assert zai.resolve_zai_key() is None


def test_no_key_anywhere_is_none() -> None:
    assert zai.resolve_zai_key() is None


def test_explicit_environ_mapping_is_used() -> None:
    env = {"ZHIPU_API_KEY": "zai-explicit"}
    assert zai.resolve_zai_key(env) == ("env:ZHIPU_API_KEY", "zai-explicit")


def test_zai_spawn_env_stamps_resolved_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret",
        lambda name: "zai-vault" if name == "zai-coding-plan" else None,
    )
    assert zai.zai_spawn_env() == {"ZHIPU_API_KEY": "zai-vault"}


def test_zai_spawn_env_empty_when_unresolved() -> None:
    assert zai.zai_spawn_env() == {}
