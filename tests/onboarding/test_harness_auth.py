"""Tests for the non-interactive credential-write core (``harness_auth.py``).

Covers the security-critical invariants of the UI-driven auth path: the raw
secret never lands in ``config.yaml`` (only a ``keychain:`` reference), the
provider entry is written correctly for key / gateway / adopt, the first
provider on a family becomes the default, unsupported families / kinds are
rejected, and readiness flips to configured after a write.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import omnigent.onboarding.harness_auth as ha
from omnigent.onboarding.provider_config import (
    default_provider_for_harness,
    load_config,
)


@pytest.fixture(autouse=True)
def _isolate_config_and_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point config home + the secret store at a tmp dir, force the file backend.

    Keeps every write off the developer's real ``~/.omnigent`` and OS keychain,
    and makes the file backend deterministic so a written secret is inspectable
    (to prove it never leaks into ``config.yaml``).
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")


def _config_text(tmp_path: Path) -> str:
    cfg = Path(tmp_path) / "config.yaml"
    return cfg.read_text() if cfg.exists() else ""


def test_store_key_writes_reference_never_raw_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stored API key lands in the secret store; config holds only a ref."""
    result = ha.store_harness_credential(
        family="anthropic",
        kind="key",
        secret="sk-ant-SECRET-VALUE",
        default_model="claude-sonnet-4-6",
    )
    assert result.stored is True
    assert result.provider_name == "anthropic"

    cfg = load_config()
    entry = cfg["providers"]["anthropic"]  # type: ignore[index]
    assert entry["kind"] == "key"
    assert entry["anthropic"]["api_key_ref"] == "keychain:anthropic"
    assert entry["anthropic"]["models"]["default"] == "claude-sonnet-4-6"
    # The raw secret must NOT appear anywhere in the on-disk config.
    assert "sk-ant-SECRET-VALUE" not in _config_text(tmp_path)
    assert "sk-ant-SECRET-VALUE" not in json.dumps(cfg)
    # First provider on the family becomes the default.
    assert entry["default"] is True


def test_store_key_makes_default_only_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second key on a family does not steal an existing default."""
    ha.store_harness_credential(family="openai", kind="key", secret="sk-one")
    # A gateway on the same family added after must not become the new default.
    ha.store_harness_credential(
        family="openai", kind="gateway", secret="gw-two", base_url="https://gw.example/v1"
    )
    cfg = load_config()
    providers = cfg["providers"]  # type: ignore[index]
    # The first (key) keeps the default; the gateway does not claim it.
    assert providers["openai"].get("default") is True
    assert providers["openai-gateway"].get("default") in (None, False)


def test_store_gateway_writes_base_url_and_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gateway entry carries base_url + a keychain ref, never the raw token."""
    result = ha.store_harness_credential(
        family="openai",
        kind="gateway",
        secret="or-GATEWAY-TOKEN",
        base_url="https://openrouter.ai/api/v1",
        wire_api="chat",
        default_model="qwen/qwen3",
    )
    assert result.stored is True
    cfg = load_config()
    entry = cfg["providers"]["openai-gateway"]  # type: ignore[index]
    assert entry["kind"] == "gateway"
    assert entry["openai"]["base_url"] == "https://openrouter.ai/api/v1"
    assert entry["openai"]["api_key_ref"] == "keychain:openai-gateway"
    assert entry["openai"]["wire_api"] == "chat"
    assert "or-GATEWAY-TOKEN" not in _config_text(tmp_path)


def test_store_rejects_unsupported_family() -> None:
    """A family outside the UI-auth set is refused (no write)."""
    result = ha.store_harness_credential(family="gemini", kind="key", secret="x")
    assert result.stored is False
    assert result.provider_name is None
    assert result.reason is not None and "family" in result.reason


def test_store_rejects_empty_secret() -> None:
    """A blank secret is refused before touching the store."""
    result = ha.store_harness_credential(family="anthropic", kind="key", secret="   ")
    assert result.stored is False
    assert result.reason == "no credential provided"


def test_store_gateway_requires_base_url() -> None:
    """A gateway without a base_url is refused."""
    result = ha.store_harness_credential(family="openai", kind="gateway", secret="tok")
    assert result.stored is False
    assert result.reason is not None and "base_url" in result.reason


def test_store_gateway_rejects_non_http_base_url() -> None:
    """A gateway base_url that isn't http(s):// is refused (no malformed entry)."""
    result = ha.store_harness_credential(
        family="openai", kind="gateway", secret="tok", base_url="ftp://gw.example/v1"
    )
    assert result.stored is False
    assert result.reason is not None and "http" in result.reason


def test_store_flips_readiness_to_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a successful write, the provider resolves for the harness family."""
    assert default_provider_for_harness(load_config(), "claude-native") is None
    ha.store_harness_credential(family="anthropic", kind="key", secret="sk-ant-x")
    assert default_provider_for_harness(load_config(), "claude-native") is not None


def test_adopt_env_credential_writes_env_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Adopt writes an ``env:<VAR>`` reference — never reads the value."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-present")  # var must be set to adopt
    result = ha.adopt_env_credential(family="anthropic", env_var="ANTHROPIC_API_KEY")
    assert result.stored is True
    cfg = load_config()
    entry = cfg["providers"]["anthropic"]  # type: ignore[index]
    assert entry["anthropic"]["api_key_ref"] == "env:ANTHROPIC_API_KEY"
    # The reference is stored, not the value.
    assert "sk-ant-present" not in _config_text(tmp_path)


def test_adopt_rejects_unset_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adopting a var that isn't set on the host is refused (no dangling ref)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = ha.adopt_env_credential(family="openai", env_var="OPENAI_API_KEY")
    assert result.stored is False
    assert result.reason is not None and "not set" in result.reason


def test_adopt_rejects_unsupported_family() -> None:
    """Adopt refuses a family outside the UI-auth set."""
    result = ha.adopt_env_credential(family="gemini", env_var="GEMINI_API_KEY")
    assert result.stored is False


def test_detect_adoptable_credentials_only_env_key_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detection surfaces only env-var key creds for the UI-auth families."""

    class _P:
        def __init__(self, family: str, kind: str, source: str) -> None:
            self.family = family
            self.kind = kind
            self.source = source

    monkeypatch.setattr(
        "omnigent.onboarding.ambient.detect_providers",
        lambda: [
            _P("anthropic", "key", "$ANTHROPIC_API_KEY"),  # kept
            _P("openai", "key", "claude CLI login"),  # dropped (not an env var)
            _P("gemini", "key", "$GEMINI_API_KEY"),  # dropped (unsupported family)
            _P("anthropic", "subscription", "$X"),  # dropped (not key kind)
        ],
    )
    detected = ha.detect_adoptable_credentials()
    assert [(d.family, d.env_var) for d in detected] == [("anthropic", "ANTHROPIC_API_KEY")]


def test_detect_adoptable_credentials_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A detection failure yields an empty list, not an exception."""
    monkeypatch.setattr(
        "omnigent.onboarding.ambient.detect_providers",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert ha.detect_adoptable_credentials() == []
