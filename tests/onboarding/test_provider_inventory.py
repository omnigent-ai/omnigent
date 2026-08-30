"""Tests for the non-secret provider inventory."""

from __future__ import annotations

from omnigent.onboarding.ambient import DetectedProvider
from omnigent.onboarding.provider_config import ProviderEntry
from omnigent.onboarding.provider_inventory import build_provider_inventory, provider_capabilities


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
        },
    ]
    serialized = repr(rows)
    assert "top-secret-value" not in serialized
    assert "gateway.example" not in serialized


def test_databricks_inventory_exposes_profile_name_and_profile_capability() -> None:
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
    assert rows[1]["configuration_state"] == "valid"
    assert "do-not-expose" not in repr(rows)
