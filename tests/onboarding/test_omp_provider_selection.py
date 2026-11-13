"""OMP provider defaults must not adopt Pi's original-auth configuration."""

from __future__ import annotations

import pytest

from omnigent.errors import OmnigentError
from omnigent.onboarding.provider_config import (
    default_provider_for_harness,
    get_default_provider,
    load_providers,
    provider_families,
    set_default_provider,
)


def _gateway(default: object) -> dict:
    return {
        "kind": "gateway",
        "default": default,
        "openai": {"base_url": "http://localhost:1234/v1", "api_key": "test-only"},
    }


def test_omp_falls_back_to_family_without_using_pi_login() -> None:
    config = {
        "providers": {
            "pi-original": {"kind": "subscription", "cli": "pi", "default": "pi"},
            "models": _gateway(True),
        }
    }
    assert default_provider_for_harness(config, "pi-native").name == "pi-original"
    for harness in ("omp", "omp-native", "native-omp"):
        assert default_provider_for_harness(config, harness).name == "models"


def test_explicit_omp_default_is_independent_of_pi_and_family() -> None:
    config = {
        "providers": {
            "pi-models": _gateway("pi"),
            "omp-models": _gateway("omp"),
            "other-models": _gateway(True),
        }
    }
    assert default_provider_for_harness(config, "omp-native").name == "omp-models"
    assert default_provider_for_harness(config, "pi-native").name == "pi-models"
    assert get_default_provider(config, "openai").name == "other-models"


@pytest.mark.parametrize("default", [True, "omp"])
def test_omp_original_auth_only_serves_omp(default: object) -> None:
    config = {
        "providers": {
            "omp-original": {
                "kind": "subscription",
                "cli": "omp",
                "default": default,
            }
        }
    }
    selected = default_provider_for_harness(config, "omp-native")
    assert selected is not None and selected.cli == "omp"
    assert provider_families(selected) == {"omp"}
    assert default_provider_for_harness(config, "pi-native") is None
    assert get_default_provider(config, "anthropic") is None
    assert get_default_provider(config, "openai") is None


def test_pi_original_auth_cannot_be_selected_for_omp() -> None:
    with pytest.raises(OmnigentError, match="does not serve"):
        load_providers(
            {
                "providers": {
                    "wrong-login": {
                        "kind": "subscription",
                        "cli": "pi",
                        "default": "omp",
                    }
                }
            }
        )


def test_omp_default_roundtrip_does_not_reassign_other_scopes() -> None:
    providers = {"family": _gateway(True), "omp-models": _gateway(False)}
    rewritten = set_default_provider(providers, "omp-models", "omp")
    config = {"providers": rewritten}
    assert default_provider_for_harness(config, "omp-native").name == "omp-models"
    assert default_provider_for_harness(config, "pi-native").name == "family"
    assert get_default_provider(config, "openai").name == "family"


def test_gemini_only_gateway_cannot_claim_omp_managed_surface() -> None:
    with pytest.raises(OmnigentError, match="does not serve"):
        load_providers(
            {
                "providers": {
                    "wrong-wire": {
                        "kind": "key",
                        "default": "omp",
                        "gemini": {"base_url": "https://example.invalid", "api_key": "test-only"},
                    }
                }
            }
        )
