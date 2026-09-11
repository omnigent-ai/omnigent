"""
Integration test for custom pricing feature.

Tests that provider config custom pricing is correctly parsed, resolved,
and used for cost computation.
"""

import pytest

from omnigent.llms.context_window import (
    compute_llm_cost,
    fetch_model_pricing_with_provider,
)
from omnigent.onboarding.provider_config import (
    ModelPricingConfig,
    _parse_family,
)


def test_parse_family_with_custom_pricing():
    """Test that _parse_family correctly parses custom pricing."""
    raw = {
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
        "pricing": {
            "input_per_million": 0.0,
            "output_per_million": 0.0,
        },
        "models": {"default": "llama3.2:latest"},
    }

    family = _parse_family("test-provider", "openai", raw)

    assert family.base_url == "http://localhost:11434/v1"
    assert family.api_key == "ollama"
    assert family.pricing is not None
    assert family.pricing.input_per_million == 0.0
    assert family.pricing.output_per_million == 0.0
    assert family.pricing.cache_read_per_million is None
    assert family.pricing.cache_write_per_million is None


def test_parse_family_with_cache_pricing():
    """Test parsing custom pricing with cache rates."""
    raw = {
        "base_url": "http://my-gateway/v1",
        "api_key": "test",
        "pricing": {
            "input_per_million": 0.25,
            "output_per_million": 1.0,
            "cache_read_per_million": 0.025,
            "cache_write_per_million": 0.3125,
        },
    }

    family = _parse_family("gateway", "openai", raw)

    assert family.pricing is not None
    assert family.pricing.input_per_million == 0.25
    assert family.pricing.output_per_million == 1.0
    assert family.pricing.cache_read_per_million == 0.025
    assert family.pricing.cache_write_per_million == 0.3125


def test_parse_family_pricing_validation():
    """Test that negative pricing values are rejected."""
    from omnigent.errors import OmnigentError

    raw = {
        "base_url": "http://localhost/v1",
        "api_key": "test",
        "pricing": {
            "input_per_million": -1.0,
            "output_per_million": 1.0,
        },
    }

    with pytest.raises(OmnigentError, match="input_per_million must be >= 0"):
        _parse_family("provider", "openai", raw)


def test_parse_family_pricing_missing_fields():
    """Test that pricing with missing required fields is rejected."""
    from omnigent.errors import OmnigentError

    raw = {
        "base_url": "http://localhost/v1",
        "api_key": "test",
        "pricing": {
            "input_per_million": 0.5,
            # Missing output_per_million
        },
    }

    with pytest.raises(
        OmnigentError, match="requires both 'input_per_million' and 'output_per_million'"
    ):
        _parse_family("provider", "openai", raw)


def test_fetch_pricing_with_custom_provider():
    """Test that custom provider pricing takes precedence over catalog."""
    # Create a mock provider config
    provider_config = {
        "providers": {
            "test-local": {
                "kind": "local",
                "default": True,
                "openai": {
                    "base_url": "http://localhost:11434/v1",
                    "api_key": "test",
                    "pricing": {
                        "input_per_million": 0.0,
                        "output_per_million": 0.0,
                    },
                },
            }
        }
    }

    # Fetch pricing for a codex harness (uses openai family)
    pricing = fetch_model_pricing_with_provider(
        model="llama3.2:latest", provider_config=provider_config, harness="codex"
    )

    # Should get the custom pricing
    assert pricing is not None
    assert pricing.input_per_token == 0.0
    assert pricing.output_per_token == 0.0


def test_fetch_pricing_fallback_to_catalog(monkeypatch: pytest.MonkeyPatch):
    """Test that pricing falls back to catalog when no custom pricing."""
    from omnigent.llms.context_window import ModelPricing

    catalog_pricing = ModelPricing(input_per_token=1.0, output_per_token=2.0)
    requested_models: list[str] = []

    def fetch_catalog_pricing(model: str) -> ModelPricing:
        requested_models.append(model)
        return catalog_pricing

    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        fetch_catalog_pricing,
    )
    # Provider with no custom pricing
    provider_config = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "test",
                    # No pricing block
                },
            }
        }
    }

    pricing = fetch_model_pricing_with_provider(
        model="test-model", provider_config=provider_config, harness="claude-sdk"
    )

    assert pricing is catalog_pricing
    assert requested_models == ["test-model"]


def test_compute_cost_with_custom_pricing():
    """Test cost computation using custom pricing."""
    # Create a mock provider config with $0.25/$1.00 pricing
    provider_config = {
        "providers": {
            "vllm": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "http://my-vllm/v1",
                    "api_key": "test",
                    "pricing": {
                        "input_per_million": 0.25,
                        "output_per_million": 1.0,
                    },
                },
            }
        }
    }

    pricing = fetch_model_pricing_with_provider(
        model="my-custom-model", provider_config=provider_config, harness="codex"
    )

    assert pricing is not None

    # Compute cost for 1500 input, 800 output tokens
    usage = {
        "input_tokens": 1500,
        "output_tokens": 800,
    }

    cost = compute_llm_cost(usage, pricing)

    # Expected: (1500 * 0.25/1M) + (800 * 1.0/1M)
    # = 0.000375 + 0.0008 = 0.001175
    expected = (1500 * 0.25 / 1_000_000) + (800 * 1.0 / 1_000_000)
    assert abs(cost - expected) < 0.0001


def test_model_pricing_config_validation():
    """Test ModelPricingConfig validation."""
    # Valid config
    config = ModelPricingConfig(input_per_million=0.25, output_per_million=1.0)
    assert config.input_per_million == 0.25

    # Negative input price should fail
    with pytest.raises(ValueError):
        ModelPricingConfig(input_per_million=-0.1, output_per_million=1.0)

    # Negative output price should fail
    with pytest.raises(ValueError):
        ModelPricingConfig(input_per_million=0.25, output_per_million=-1.0)

    # Negative cache prices should fail
    with pytest.raises(ValueError):
        ModelPricingConfig(
            input_per_million=0.25, output_per_million=1.0, cache_read_per_million=-0.025
        )


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v"])


def test_fetch_pricing_with_explicit_provider_entry_beats_default():
    """An explicit provider_entry uses that entry's rates, not the default's.

    This is the unit-level guard for the bug where _accumulate_session_usage
    always resolves the default provider, so sessions on a non-default named
    provider were charged the wrong rate.
    """
    from omnigent.onboarding.provider_config import _parse_provider

    expensive_entry = _parse_provider(
        "expensive-named",
        {
            "kind": "key",
            "anthropic": {
                "base_url": "https://expensive.example/v1",
                "api_key": "expensive-key",
                "pricing": {"input_per_million": 10.0, "output_per_million": 20.0},
            },
        },
    )
    provider_config = {
        "providers": {
            "cheap-default": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://cheap.example/v1",
                    "api_key": "cheap-key",
                    "pricing": {"input_per_million": 1.0, "output_per_million": 2.0},
                },
            }
        }
    }

    # Without provider_entry: returns the default (cheap) provider's rate.
    default_pricing = fetch_model_pricing_with_provider(
        model="claude-sonnet-4-6", provider_config=provider_config, harness="claude-sdk"
    )
    assert default_pricing is not None
    assert default_pricing.input_per_token == pytest.approx(1.0 / 1_000_000)

    # With provider_entry: returns the named (expensive) provider's rate.
    named_pricing = fetch_model_pricing_with_provider(
        model="claude-sonnet-4-6",
        provider_config=provider_config,
        harness="claude-sdk",
        provider_entry=expensive_entry,
    )
    assert named_pricing is not None
    assert named_pricing.input_per_token == pytest.approx(10.0 / 1_000_000)
    assert named_pricing.output_per_token == pytest.approx(20.0 / 1_000_000)


def test_fetch_pricing_provider_entry_without_pricing_falls_through_to_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the explicit provider_entry has no custom pricing, catalog is used."""
    from omnigent.llms.context_window import ModelPricing
    from omnigent.onboarding.provider_config import _parse_provider

    # Entry with no pricing block.
    entry_no_pricing = _parse_provider(
        "bare-provider",
        {
            "kind": "key",
            "anthropic": {
                "base_url": "https://bare.example/v1",
                "api_key": "bare-key",
                # No pricing block.
            },
        },
    )
    catalog_pricing = ModelPricing(input_per_token=5.0, output_per_token=15.0)
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda _model: catalog_pricing,
    )

    pricing = fetch_model_pricing_with_provider(
        model="claude-sonnet-4-6",
        harness="claude-sdk",
        provider_entry=entry_no_pricing,
    )
    assert pricing is catalog_pricing


def test_fetch_pricing_provider_entry_takes_precedence_over_config_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider_entry beats the config default even when provider_config is also given."""
    from omnigent.onboarding.provider_config import _parse_provider

    explicit_entry = _parse_provider(
        "explicit",
        {
            "kind": "key",
            "anthropic": {
                "base_url": "https://explicit.example/v1",
                "api_key": "explicit-key",
                "pricing": {"input_per_million": 42.0, "output_per_million": 84.0},
            },
        },
    )
    provider_config = {
        "providers": {
            "some-default": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://default.example/v1",
                    "api_key": "default-key",
                    "pricing": {"input_per_million": 1.0, "output_per_million": 2.0},
                },
            }
        }
    }
    # catalog must not be consulted
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda _model: (_ for _ in ()).throw(AssertionError("catalog must not be called")),
    )

    pricing = fetch_model_pricing_with_provider(
        model="any-model",
        provider_config=provider_config,
        harness="claude-sdk",
        provider_entry=explicit_entry,
    )
    assert pricing is not None
    assert pricing.input_per_token == pytest.approx(42.0 / 1_000_000)
    assert pricing.output_per_token == pytest.approx(84.0 / 1_000_000)
