"""Tests for the non-secret provider inventory."""

from __future__ import annotations

import pytest

from omnigent.onboarding.ambient import DetectedProvider
from omnigent.onboarding.provider_config import FamilyConfig, ProviderEntry
from omnigent.onboarding.provider_inventory import (
    ConnectionState,
    build_provider_inventory,
    provider_capabilities,
    provider_connection_state,
)


def test_inventory_combines_configured_and_detected_providers_without_secrets() -> None:
    config: dict[str, object] = {
        "providers": {
            "work": {
                "kind": "gateway",
                "default": "openai",
                "openai": {
                    "base_url": "https://gateway.example/v1",
                    "api_key": "top-secret-value",
                    "models": {"default": "company-model"},
                },
            }
        }
    }
    detected = [
        DetectedProvider(
            name="claude",
            kind="subscription",
            family="anthropic",
            source="claude CLI login",
        )
    ]

    rows = [entry.as_dict() for entry in build_provider_inventory(config, detected=detected)]

    assert rows == [
        {
            "id": "claude",
            "display_name": "claude",
            "kind": "subscription",
            "origin": "detected",
            "source": "claude CLI login",
            "configuration_state": "valid",
            "error": None,
            "families": ["anthropic"],
            "surfaces": ["anthropic"],
            "default_for": ["anthropic"],
            "default_models": {},
            "cli": "claude",
            "profile": None,
            "model_provider": None,
            "capabilities": {
                "model_discovery": "supported",
                "usage_status": "unsupported",
                "multiple_profiles": "unsupported",
                "interactive_cli": "supported",
            },
            "connection_state": "connected",
            "connection_detail": (
                "A usable claude credential is configured locally; the vendor was not contacted."
            ),
            "default_for_harnesses": ["claude-native", "claude-sdk"],
        },
        {
            "id": "work",
            "display_name": "work",
            "kind": "gateway",
            "origin": "configured",
            "source": "config",
            "configuration_state": "valid",
            "error": None,
            "families": ["openai"],
            "surfaces": ["openai", "pi"],
            "default_for": ["openai"],
            "default_models": {"openai": "company-model"},
            "cli": None,
            "profile": None,
            "model_provider": None,
            "capabilities": {
                "model_discovery": "supported",
                "usage_status": "unsupported",
                "multiple_profiles": "unknown",
                "interactive_cli": "unsupported",
            },
            "connection_state": "connected",
            "connection_detail": (
                "The openai credential is configured locally; the vendor was not contacted."
            ),
            "default_for_harnesses": ["codex-native", "codex", "openai-agents", "pi-native", "pi"],
        },
    ]
    serialized = repr(rows)
    assert "top-secret-value" not in serialized
    assert "gateway.example" not in serialized


def test_databricks_inventory_exposes_profile_name_and_profile_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.list_databricks_profiles",
        lambda: ["staging"],
    )
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.databricks_sdk_installed",
        lambda: True,
    )
    config: dict[str, object] = {
        "providers": {
            "analytics": {
                "kind": "databricks",
                "profile": "staging",
                "default": "openai",
            }
        }
    }

    [row] = [entry.as_dict() for entry in build_provider_inventory(config, detected=[])]

    assert row["profile"] == "staging"
    assert row["connection_state"] == "unknown"
    assert row["capabilities"] == {
        "model_discovery": "supported",
        "usage_status": "unsupported",
        "multiple_profiles": "supported",
        "interactive_cli": "unsupported",
    }


def test_bedrock_inventory_does_not_claim_unimplemented_discovery() -> None:
    provider = ProviderEntry(name="aws", kind="bedrock")

    assert provider_capabilities(provider).as_dict()["model_discovery"] == "unknown"


def test_inventory_keeps_malformed_entries_as_explicit_invalid_state() -> None:
    config: dict[str, object] = {
        "providers": {
            "broken": {"kind": "gateway", "api_key": "do-not-expose"},
            "codex": {"kind": "subscription", "cli": "codex"},
        }
    }

    rows = [entry.as_dict() for entry in build_provider_inventory(config, detected=[])]

    assert [row["id"] for row in rows] == ["broken", "codex"]
    assert rows[0]["configuration_state"] == "invalid"
    assert rows[0]["error"] == "Provider configuration is invalid. Reconfigure this provider."
    assert rows[0]["connection_state"] == "misconfigured"
    assert rows[1]["configuration_state"] == "valid"
    assert "do-not-expose" not in repr(rows)


def _subscription(cli: str = "codex") -> ProviderEntry:
    return ProviderEntry(name=cli, kind="subscription", cli=cli)


def _gateway(family: FamilyConfig, *, name: str = "work") -> ProviderEntry:
    return ProviderEntry(name=name, kind="gateway", families={"openai": family})


@pytest.mark.parametrize(
    ("availability", "expected"),
    [
        ("needs-auth", ConnectionState.AUTHENTICATION_REQUIRED),
        ("binary-missing", ConnectionState.UNAVAILABLE),
        (False, ConnectionState.UNAVAILABLE),
        ("version-too-low", ConnectionState.UNAVAILABLE),
    ],
)
def test_cli_provider_uses_cached_negative_readiness(
    availability: object, expected: ConnectionState
) -> None:
    connection = provider_connection_state(
        _subscription(),
        harness_readiness={"codex-native": availability},  # type: ignore[dict-item]
    )

    assert connection.state is expected
    assert connection.detail


def test_aggregate_cli_readiness_is_not_provider_specific_proof() -> None:
    connection = provider_connection_state(
        _subscription(),
        harness_readiness={"codex-native": True},
    )

    assert connection.state is ConnectionState.UNKNOWN
    assert "another provider" in connection.detail


def test_safe_provider_detection_is_provider_specific_proof() -> None:
    connection = provider_connection_state(
        _subscription(),
        harness_readiness={"codex-native": True},
        provider_detected=True,
    )

    assert connection.state is ConnectionState.CONNECTED
    assert "vendor was not contacted" in connection.detail


def test_keychain_reference_is_unknown_without_resolving_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.onboarding.provider_inventory as provider_inventory_module

    def _forbidden_secret_resolution(ref: str) -> str:
        raise AssertionError(f"status read resolved secret {ref!r}")

    assert not hasattr(provider_inventory_module, "resolve_secret")
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.resolve_secret",
        _forbidden_secret_resolution,
    )
    provider = _gateway(
        FamilyConfig(base_url="https://gateway.example/v1", api_key_ref="keychain:openai")
    )

    connection = provider_connection_state(provider)

    assert connection.state is ConnectionState.UNKNOWN
    assert "protected secret store" in connection.detail


def test_env_reference_uses_presence_without_leaking_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_TEST_PROVIDER_KEY", "sk-do-not-expose")
    provider = _gateway(
        FamilyConfig(
            base_url="https://gateway.example/v1",
            api_key_ref="env:OMNIGENT_TEST_PROVIDER_KEY",
        )
    )

    connection = provider_connection_state(provider)

    assert connection.state is ConnectionState.CONNECTED
    assert "sk-do-not-expose" not in connection.detail


def test_missing_env_reference_needs_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_TEST_PROVIDER_KEY", raising=False)
    provider = _gateway(
        FamilyConfig(
            base_url="https://gateway.example/v1",
            api_key_ref="env:OMNIGENT_TEST_PROVIDER_KEY",
        )
    )

    assert provider_connection_state(provider).state is ConnectionState.AUTHENTICATION_REQUIRED


def test_auth_command_is_unknown_because_status_reads_never_run_it() -> None:
    provider = _gateway(
        FamilyConfig(base_url="https://gateway.example/v1", auth_command="print-token")
    )

    assert provider_connection_state(provider).state is ConnectionState.UNKNOWN


def test_databricks_profile_presence_does_not_claim_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.list_databricks_profiles",
        lambda: ["staging"],
    )
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.databricks_sdk_installed",
        lambda: True,
    )
    provider = ProviderEntry(name="analytics", kind="databricks", profile="staging")

    connection = provider_connection_state(provider)

    assert connection.state is ConnectionState.UNKNOWN
    assert "do not authenticate" in connection.detail


def test_rows_name_the_harnesses_they_would_serve() -> None:
    # The picker must not re-derive harness→provider from families itself.
    config: dict[str, object] = {
        "providers": {
            "claude": {"kind": "subscription", "cli": "claude", "default": "anthropic"},
            "work": {
                "kind": "gateway",
                "default": "openai",
                "openai": {"base_url": "https://gateway.example/v1", "api_key": "k"},
            },
        }
    }

    rows = {row.provider_id: row for row in build_provider_inventory(config, detected=[])}

    assert rows["claude"].default_for_harnesses == ("claude-native", "claude-sdk")
    assert "codex-native" in rows["work"].default_for_harnesses
    assert "claude-native" not in rows["work"].default_for_harnesses


def test_a_provider_that_is_nobodys_default_names_no_harness() -> None:
    config: dict[str, object] = {
        "providers": {
            "spare": {
                "kind": "gateway",
                "openai": {"base_url": "https://spare.example/v1", "api_key": "k"},
            }
        }
    }

    [row] = build_provider_inventory(config, detected=[])

    assert row.default_for_harnesses == ()


def test_malformed_unrelated_provider_does_not_erase_valid_harness_mapping() -> None:
    config: dict[str, object] = {
        "providers": {
            "broken": {
                "kind": "gateway",
                "default": "anthropic",
                "anthropic": "not a provider family configuration",
            },
            "work": {
                "kind": "gateway",
                "default": "openai",
                "openai": {"base_url": "https://gateway.example/v1", "api_key": "k"},
            },
        }
    }

    rows = {row.provider_id: row for row in build_provider_inventory(config, detected=[])}

    assert rows["broken"].configuration_state == "invalid"
    assert "codex-native" in rows["work"].default_for_harnesses
    assert "openai-agents" in rows["work"].default_for_harnesses
