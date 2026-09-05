"""Regression test: gateway pi-native sessions emit bare model entries.

pi-native sessions with a generic OpenAI-compatible gateway provider produce a
bare ``{"id": "<model>"}`` entry in ``models.json``, with no ``contextWindow``,
``maxTokens``, ``reasoning``, or cost metadata.  Pi then falls back to its own
defaults (128k context / 16k output / reasoning disabled / zero cost).

The defect is in ``_inline_family_pi_provider`` (omnigent/pi_native_credentials.py):
it constructs a ``PiProviderConfig`` without ``extra_models``, so
``to_models_config`` appends only ``{"id": ...}`` instead of a full entry.

Expected: the provider config should carry the model's metadata so Pi sees the
real limits and capabilities.

Steps to reproduce (adapted from the bug report):
1. Configure a generic OpenAI-compatible gateway provider as the default,
   pointing at a model with a context window larger than 128k
   (e.g. glm-5.2 with 1M context).
2. Resolve the pi-native provider config via ``resolve_pi_native_provider``.
3. Call ``to_models_config()`` to render the ``models.json`` content.
4. Observe that the entry under the ``omnigent`` provider is ``{"id": "glm-5.2"}``
   only -- no ``contextWindow``, ``maxTokens``, ``reasoning``, or cost.
"""

from __future__ import annotations

from omnigent import pi_native_credentials as creds

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _gateway_config_chat_wire(
    model: str = "glm-5.2",
    base_url: str = "https://api.example.com",
) -> dict[str, object]:
    """A generic LiteLLM gateway using wire_api=chat (openai-completions).

    Includes ``context_window`` and ``max_output_tokens`` so pi-native sessions
    can advertise real limits instead of Pi's 128k/16k defaults.
    """
    return {
        "providers": {
            "litellm": {
                "kind": "gateway",
                "openai": {
                    "api_key": "test-litellm-key",
                    "base_url": base_url,
                    "models": {"default": model},
                    "wire_api": "chat",
                    "context_window": 1_000_000,
                    "max_output_tokens": 65_536,
                },
                "default": True,
            }
        }
    }


def _gateway_config_responses_wire(
    model: str = "gpt-4o-mini",
    base_url: str = "https://api.example.com",
) -> dict[str, object]:
    """A generic gateway using the default responses wire (openai-responses).

    Includes ``context_window`` and ``max_output_tokens`` so pi-native sessions
    can advertise real limits instead of Pi's 128k/16k defaults.
    """
    return {
        "providers": {
            "litellm": {
                "kind": "gateway",
                "openai": {
                    "api_key": "test-key",
                    "base_url": base_url,
                    "models": {"default": model},
                    "context_window": 128_000,
                    "max_output_tokens": 16_384,
                },
                "default": True,
            }
        }
    }


def _gateway_config_anthropic_proxy(
    model: str = "glm-5.2",
    base_url: str = "https://api.example.com/anthropic",
) -> dict[str, object]:
    """A LiteLLM proxy configured with an Anthropic-protocol surface.

    Includes ``context_window`` and ``max_output_tokens`` so pi-native sessions
    can advertise real limits instead of Pi's 128k/16k defaults.
    """
    return {
        "providers": {
            "litellm": {
                "kind": "gateway",
                "anthropic": {
                    "api_key": "test-litellm-key",
                    "base_url": base_url,
                    "models": {"default": model},
                    "context_window": 1_000_000,
                    "max_output_tokens": 131_072,
                },
                "default": True,
            }
        }
    }


# ---------------------------------------------------------------------------
# Facet 1: openai-completions (wire_api: chat) path emits bare {"id": model}
# ---------------------------------------------------------------------------


def test_generic_gateway_chat_wire_models_json_is_bare() -> None:
    """Core defect: a generic gateway (wire_api: chat) produces a bare entry.

    ``resolve_pi_native_provider`` returns a ``PiProviderConfig`` without any
    ``extra_models``, so ``to_models_config()`` emits ``{"id": "glm-5.2"}``
    with no ``contextWindow``, ``maxTokens``, or ``reasoning``.

    This test FAILS on the buggy build (proving the defect is live) and should
    PASS after the fix enriches the generic gateway path.
    """
    provider = creds.resolve_pi_native_provider(
        config_loader=lambda: _gateway_config_chat_wire("glm-5.2")
    )

    assert provider is not None
    assert provider.api == "openai-completions"
    assert provider.model == "glm-5.2"

    cfg = provider.to_models_config()
    models = cfg["providers"]["omnigent"]["models"]
    assert len(models) == 1
    entry = models[0]

    # Bug: all three fields are absent; the entry is a bare id-only dict.
    # After the fix, at least one of contextWindow/maxTokens/reasoning must be
    # populated from provider config or an external metadata source.
    assert "contextWindow" in entry, (
        "gateway bare entry reproduced: models.json entry has no contextWindow; "
        f"Pi will fall back to 128000.  Actual entry: {entry}"
    )
    assert "maxTokens" in entry, (
        "gateway bare entry reproduced: models.json entry has no maxTokens; "
        f"Pi will cap output at 16384.  Actual entry: {entry}"
    )


def test_generic_gateway_chat_wire_reasoning_disabled() -> None:
    """Symptom 3: reasoning flag is absent for a gateway reasoning model.

    For a model that supports thinking (e.g. a reasoning-capable GLM), Pi
    receives no ``reasoning: true`` flag, so thinking is silently disabled even
    when the project sets ``defaultThinkingLevel: high``.
    """
    # Use a reasoning-capable model id (deepseek is the recognised fragment)
    provider = creds.resolve_pi_native_provider(
        config_loader=lambda: _gateway_config_chat_wire("deepseek-r2")
    )

    assert provider is not None
    assert provider.api == "openai-completions"

    cfg = provider.to_models_config()
    entry = cfg["providers"]["omnigent"]["models"][0]

    # Bug: reasoning is absent.  After the fix, the entry must carry
    # ``reasoning: true`` for a reasoning-model id.
    assert entry.get("reasoning") is True, (
        "gateway bare entry reproduced: models.json entry missing reasoning:true for a "
        f"reasoning model; Pi runs with thinking disabled.  Actual entry: {entry}"
    )


# ---------------------------------------------------------------------------
# Facet 2: openai-responses (default wire) path — same defect
# ---------------------------------------------------------------------------


def test_generic_gateway_responses_wire_models_json_is_bare() -> None:
    """Defect also affects the default openai-responses wire.

    Any generic gateway that does not set ``wire_api`` (defaults to responses)
    is equally affected: ``extra_models`` is empty, so the entry is bare.
    """
    provider = creds.resolve_pi_native_provider(
        config_loader=lambda: _gateway_config_responses_wire("gpt-4o-mini")
    )

    assert provider is not None
    assert provider.api == "openai-responses"
    assert provider.model == "gpt-4o-mini"

    cfg = provider.to_models_config()
    entry = cfg["providers"]["omnigent"]["models"][0]

    assert "contextWindow" in entry, (
        "gateway bare entry reproduced (responses wire): models.json entry has no "
        f"contextWindow.  Actual entry: {entry}"
    )
    assert "maxTokens" in entry, (
        "gateway bare entry reproduced (responses wire): models.json entry has no "
        f"maxTokens.  Actual entry: {entry}"
    )


# ---------------------------------------------------------------------------
# Facet 3: anthropic-messages proxy path — contextWindow/maxTokens also absent
# ---------------------------------------------------------------------------


def test_generic_gateway_anthropic_proxy_lacks_context_limits() -> None:
    """Defect applies to the anthropic-messages path too.

    A LiteLLM proxy speaking the Anthropic Messages protocol for a non-Claude
    model (e.g. GLM via a passthrough) receives ``reasoning: true`` (because
    ``api == 'anthropic-messages'``), but still no ``contextWindow`` or
    ``maxTokens``.
    """
    provider = creds.resolve_pi_native_provider(
        config_loader=lambda: _gateway_config_anthropic_proxy("glm-5.2")
    )

    assert provider is not None
    assert provider.api == "anthropic-messages"
    assert provider.model == "glm-5.2"

    cfg = provider.to_models_config()
    entry = cfg["providers"]["omnigent"]["models"][0]

    # reasoning is already set (because api==anthropic-messages), but
    # contextWindow and maxTokens are absent — Pi truncates at 128k/16k.
    assert "contextWindow" in entry, (
        "gateway bare entry reproduced (anthropic proxy): models.json entry has no "
        f"contextWindow.  Actual entry: {entry}"
    )
    assert "maxTokens" in entry, (
        "gateway bare entry reproduced (anthropic proxy): models.json entry has no "
        f"maxTokens.  Actual entry: {entry}"
    )


# ---------------------------------------------------------------------------
# Contrast: the fix contract (what the fix must produce)
# ---------------------------------------------------------------------------


def test_fix_contract_extra_models_propagate_to_models_json() -> None:
    """Documents the fix contract: extra_models must flow into models.json.

    When the generic gateway path populates ``extra_models`` on the returned
    ``PiProviderConfig`` (either from provider config or a metadata lookup),
    ``to_models_config()`` already handles it correctly — it walks ``extra_models``
    first and skips the bare-id append.  This test confirms that mechanic works.

    A passing test here proves the models.json rendering path is correct;
    the regression tests above prove the *population* of extra_models is missing.
    """
    full_entry: creds._PiModelEntry = {
        "id": "glm-5.2",
        "input": ["text", "image"],
        "contextWindow": 1_048_575,
        "maxTokens": 131_072,
        "reasoning": True,
    }
    provider = creds.PiProviderConfig(
        provider_id="omnigent",
        base_url="https://api.example.com",
        api="openai-completions",
        model="glm-5.2",
        api_key="test-key",
        auth_header=False,
        extra_models=[full_entry],
    )

    cfg = provider.to_models_config()
    entry = cfg["providers"]["omnigent"]["models"][0]

    assert entry["contextWindow"] == 1_048_575
    assert entry["maxTokens"] == 131_072
    assert entry["reasoning"] is True
