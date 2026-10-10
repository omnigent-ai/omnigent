"""Tests for opencode-native credential reporting (``opencode_auth.py``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import omnigent.onboarding.opencode_auth as oc

# The AWS credential-chain variables OpenCode's Bedrock loader autoloads on.
_AWS_BEDROCK_VARS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point XDG_DATA_HOME at a tmp dir and clear provider env keys."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for _provider_id, _label, var in oc._ENV_PROVIDER_VARS:
        monkeypatch.delenv(var, raising=False)


def _write_auth(tmp_path: Path, providers: dict[str, object]) -> None:
    path = oc.opencode_auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(providers), encoding="utf-8")


def _write_config(tmp_path: Path, raw: str, filename: str = "opencode.json") -> None:
    path = tmp_path / "config" / "opencode" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")


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


@pytest.mark.parametrize("var", _AWS_BEDROCK_VARS)
def test_aws_credential_env_marks_bedrock_reachable(
    monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    """Any AWS variable OpenCode's Bedrock loader keys on counts as a provider."""
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    monkeypatch.setenv(var, "placeholder")

    summary = oc.opencode_auth_summary()
    assert summary.ready is True
    assert summary.stored_providers == ()
    assert summary.env_providers == ("Amazon Bedrock",)
    assert "env: Amazon Bedrock" in summary.describe()
    assert oc.reachable_provider_ids() == frozenset({"amazon-bedrock"})


@pytest.mark.parametrize(
    "var", ["AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION", "AWS_SHARED_CREDENTIALS_FILE"]
)
def test_other_aws_env_does_not_mark_bedrock_reachable(
    monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    """AWS variables OpenCode's loader ignores must not read as a provider."""
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    monkeypatch.setenv(var, "placeholder")

    assert oc.opencode_auth_summary().ready is False
    assert oc.reachable_provider_ids() == frozenset()


@pytest.mark.parametrize(
    ("filename", "raw"),
    [
        ("opencode.json", '{"provider": {"amazon-bedrock": {"options": {"profile": "work"}}}}'),
        (
            "opencode.json",
            '{"provider": {"amazon-bedrock": {"options": {"region": "us-west-2"}}}}',
        ),
        ("opencode.json", '{"provider": {"amazon-bedrock": {}}}'),
        (
            "opencode.jsonc",
            "// AWS credentials come from the profile\n"
            '{"provider": {"amazon-bedrock": {"options": {"profile": "work",}}}}',
        ),
    ],
)
def test_bedrock_provider_block_is_ready_without_stored_auth_or_api_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, filename: str, raw: str
) -> None:
    """OpenCode loads Bedrock from any ``provider.amazon-bedrock`` block."""
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    _write_config(tmp_path, raw, filename)

    summary = oc.opencode_auth_summary()
    assert summary.ready is True
    assert summary.stored_providers == ()
    assert summary.env_providers == ()
    assert summary.configured_providers == ("amazon-bedrock",)
    assert "config: amazon-bedrock" in summary.describe()
    assert oc.reachable_provider_ids() == frozenset({"amazon-bedrock"})


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"provider": []}',
        '{"provider": {"amazon-bedrock": "work"}}',
        '{"enabled_providers": ["amazon-bedrock"]}',
    ],
)
def test_config_without_bedrock_block_does_not_report_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str
) -> None:
    """``enabled_providers`` only filters; a malformed file credits nothing."""
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    _write_config(tmp_path, raw)

    assert oc.opencode_auth_summary().ready is False
    assert oc.reachable_provider_ids() == frozenset()
