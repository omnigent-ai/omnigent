"""Tests for opencode-native credential reporting (``opencode_auth.py``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import omnigent.onboarding.opencode_auth as oc


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point XDG_DATA_HOME at a tmp dir and clear provider env keys."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    for _provider_id, _label, var in oc._ENV_PROVIDER_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_OPENCODE_API_KEY", raising=False)
    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", lambda _name: None)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_ZHIPU_API_KEY", raising=False)


def _write_auth(tmp_path: Path, providers: dict[str, object]) -> None:
    path = oc.opencode_auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(providers), encoding="utf-8")


def test_auth_path_honors_xdg_data_home(tmp_path: Path) -> None:
    assert oc.opencode_auth_path() == tmp_path / "share" / "opencode" / "auth.json"


def test_stored_providers_reads_auth_json_keys(tmp_path: Path) -> None:
    _write_auth(tmp_path, {"anthropic": {"type": "api", "key": "x"}, "openai": {"type": "oauth"}})
    assert set(oc._stored_providers()) == {"anthropic", "openai"}


def test_stored_providers_ignores_empty_entries(tmp_path: Path) -> None:
    """An empty provider object is config shape, not a usable stored credential."""
    _write_auth(
        tmp_path,
        {
            "anthropic": {"type": "api", "key": "x"},
            "openai": {},
            "groq": "",
        },
    )
    assert oc._stored_providers() == ("anthropic",)


def test_stored_providers_empty_when_missing_or_invalid(tmp_path: Path) -> None:
    assert oc._stored_providers() == ()  # no file
    oc.opencode_auth_path().parent.mkdir(parents=True, exist_ok=True)
    oc.opencode_auth_path().write_text("not json", encoding="utf-8")
    assert oc._stored_providers() == ()


def test_env_providers_detects_present_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-y")
    labels = oc._env_providers()
    assert "OpenAI" in labels and "Anthropic" in labels


def test_summary_ready_requires_installed_and_a_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    # No provider yet → not ready.
    assert oc.opencode_auth_summary().ready is False
    # An env key flips it ready.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    summary = oc.opencode_auth_summary()
    assert summary.ready is True
    assert summary.has_provider is True
    assert "env: OpenAI" in summary.describe()


def test_summary_not_ready_when_cli_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    assert oc.opencode_auth_summary().ready is False  # provider present but no binary


def test_describe_lists_stored_and_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    _write_auth(tmp_path, {"anthropic": {"type": "api", "key": "x"}})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    text = oc.opencode_auth_summary().describe()
    assert "1 stored (anthropic)" in text
    assert "env: OpenAI" in text


def test_reachable_provider_ids_merges_stored_and_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_auth(tmp_path, {"anthropic": {"type": "api", "key": "x"}})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    ids = oc.reachable_provider_ids()
    assert "anthropic" in ids  # from auth.json
    assert "openai" in ids  # from env key
    assert "groq" not in ids


def test_summary_zen_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-zen")
    summary = oc.opencode_auth_summary()
    assert summary.zen_key_source == "env:OPENCODE_API_KEY"
    assert summary.has_provider
    assert summary.ready


def test_summary_zen_key_from_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret",
        lambda name: "sk-vault" if name == "opencode-zen" else None,
    )
    summary = oc.opencode_auth_summary()
    assert summary.zen_key_source == "keychain"
    assert summary.has_provider


def test_summary_no_zen_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    summary = oc.opencode_auth_summary()
    assert summary.zen_key_source is None
    assert not summary.has_provider


def test_describe_includes_zen_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret",
        lambda name: "sk-vault" if name == "opencode-zen" else None,
    )
    text = oc.opencode_auth_summary().describe()
    assert "env: OpenAI" in text
    assert "zen key: keychain" in text


def test_reachable_ids_include_opencode_for_zen_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret",
        lambda name: "sk-vault" if name == "opencode-zen" else None,
    )
    ids = oc.reachable_provider_ids()
    assert "opencode" in ids
    assert "opencode-go" in ids


def test_reachable_ids_include_opencode_for_env_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-zen")
    ids = oc.reachable_provider_ids()
    assert "opencode" in ids
    assert "opencode-go" in ids


def test_reachable_ids_omit_opencode_without_key() -> None:
    ids = oc.reachable_provider_ids()
    assert "opencode" not in ids
    assert "opencode-go" not in ids


def test_env_providers_detects_zhipu_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZHIPU_API_KEY", "zai-x")
    labels = oc._env_providers()
    assert "Z.AI Coding Plan" in labels


def test_summary_zai_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    monkeypatch.setenv("ZHIPU_API_KEY", "zai-env")
    summary = oc.opencode_auth_summary()
    assert summary.zai_key_source == "env:ZHIPU_API_KEY"
    assert summary.has_provider
    assert summary.ready


def test_summary_zai_key_from_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret",
        lambda name: "zai-vault" if name == "zai-coding-plan" else None,
    )
    summary = oc.opencode_auth_summary()
    assert summary.zai_key_source == "keychain"
    assert summary.has_provider


def test_summary_no_zai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    summary = oc.opencode_auth_summary()
    assert summary.zai_key_source is None


def test_describe_includes_zai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret",
        lambda name: "zai-vault" if name == "zai-coding-plan" else None,
    )
    text = oc.opencode_auth_summary().describe()
    assert "zai key: keychain" in text


def test_reachable_ids_include_zai_for_keychain_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret",
        lambda name: "zai-vault" if name == "zai-coding-plan" else None,
    )
    ids = oc.reachable_provider_ids()
    assert "zai-coding-plan" in ids


def test_reachable_ids_include_zai_for_env_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZHIPU_API_KEY", "zai-env")
    ids = oc.reachable_provider_ids()
    assert "zai-coding-plan" in ids


def test_reachable_ids_omit_zai_without_key() -> None:
    ids = oc.reachable_provider_ids()
    assert "zai-coding-plan" not in ids
