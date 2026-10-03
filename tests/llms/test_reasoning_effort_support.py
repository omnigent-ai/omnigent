"""Test provider gating, rejection detection, and the learned cache."""

from __future__ import annotations

from typing import Any

import httpx
import openai
import pytest

from omnigent.llms.reasoning_effort_support import (
    accepts_reasoning_effort,
    clear_learned_rejections,
    gating_identity,
    is_reasoning_effort_rejection,
    record_reasoning_effort_rejection,
    strip_rejected_reasoning_effort,
)


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    """Isolate the learned-rejection cache across tests."""
    clear_learned_rejections()


def _http_error(status_code: int, body: str) -> httpx.HTTPStatusError:
    """Build an ``HTTPStatusError`` with the given status and body."""
    return httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=httpx.Request("POST", "http://test/v1/chat/completions"),
        response=httpx.Response(status_code, content=body.encode()),
    )


@pytest.mark.parametrize(
    "model",
    ["grok-4", "grok-code-fast-1", "grok-4-fast-reasoning"],
)
def test_seeded_xai_models_are_rejected(model: str) -> None:
    """Models with observed xAI rejections are skipped up front."""
    assert not accepts_reasoning_effort("xai", model)


def test_seed_match_is_case_insensitive() -> None:
    """Model-id casing must not defeat the seed set."""
    assert not accepts_reasoning_effort("xai", "Grok-4")


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("xai", "grok-3-mini"),  # supported Grok model
        ("xai", "grok-4-someday-new"),  # unknown ids are optimistic
        ("groq", "llama-3.3-70b-versatile"),  # other providers pass through
        ("databricks", "grok-4"),  # seed is per-provider, not per-name
    ],
)
def test_unlisted_pairs_are_optimistic(provider: str, model: str) -> None:
    """Anything outside the seed/learned sets keeps the parameter."""
    assert accepts_reasoning_effort(provider, model)


def test_recorded_rejection_is_learned() -> None:
    """A recorded live rejection flips the gate for that pair only."""
    record_reasoning_effort_rejection("xai", "grok-new")
    assert not accepts_reasoning_effort("xai", "grok-new")
    assert accepts_reasoning_effort("xai", "grok-other")
    assert accepts_reasoning_effort("groq", "grok-new")


@pytest.mark.parametrize(
    "body",
    [
        # xAI's observed snake_case form.
        '{"error": "Argument not supported on this model: reasoning_effort"}',
        # camelCase form seen from grok fast models.
        '{"error": "Model grok-4-1-fast does not support parameter reasoningEffort."}',
    ],
)
def test_param_rejection_bodies_detected(body: str) -> None:
    """Both observed provider spellings of the rejection are matched."""
    assert is_reasoning_effort_rejection(_http_error(400, body))


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        # Unrelated 400 — mentions neither support nor the param.
        (400, '{"error": "messages: field required"}'),
        # 400 that echoes the param without a support complaint.
        (400, '{"error": "invalid value for reasoning_effort: bogus"}'),
        # Invalid effort values must not disable a supported parameter.
        (400, '{"error": "reasoning_effort must be one of the supported values: low, high"}'),
        (400, '{"error": "Unsupported value \'xhigh\' for reasoning_effort."}'),
        # Mentioning support of something else is not a capability rejection.
        (400, '{"error": "reasoning_effort requires a supporting beta header"}'),
        # Right body, wrong status — not a capability rejection.
        (503, '{"error": "Argument not supported on this model: reasoning_effort"}'),
    ],
)
def test_non_rejection_errors_not_matched(status_code: int, body: str) -> None:
    """Only a 400 naming the param as unsupported triggers the fallback."""
    assert not is_reasoning_effort_rejection(_http_error(status_code, body))


def test_non_http_errors_not_matched() -> None:
    """Transport errors and other exceptions never trigger the fallback."""
    assert not is_reasoning_effort_rejection(ValueError("boom"))
    assert not is_reasoning_effort_rejection(
        httpx.ConnectError("no route", request=httpx.Request("POST", "http://test"))
    )


def _openai_error(status_code: int, body: str) -> openai.APIStatusError:
    """Build the OpenAI client exception shape the executor path raises."""
    response = httpx.Response(
        status_code,
        content=body.encode(),
        request=httpx.Request("POST", "https://api.x.ai/v1/chat/completions"),
    )
    return openai.APIStatusError(f"Error code: {status_code}", response=response, body=None)


def test_openai_client_400_shape_detected() -> None:
    """``openai.APIStatusError`` (the agents-SDK path) matches too."""
    exc = _openai_error(400, "Argument not supported on this model: reasoning_effort")
    assert is_reasoning_effort_rejection(exc)


def test_openai_client_unrelated_400_not_matched() -> None:
    """An openai-shaped 400 about another parameter stays untouched."""
    assert not is_reasoning_effort_rejection(_openai_error(400, "max_tokens is too large"))
    assert not is_reasoning_effort_rejection(
        _openai_error(429, "Argument not supported on this model: reasoning_effort")
    )


def test_learned_rejection_is_scoped_to_endpoint() -> None:
    """A 400 learned via one endpoint must not suppress the param elsewhere."""
    record_reasoning_effort_rejection("openai", "proxy-model", "https://proxy.example.com/v1")

    assert not accepts_reasoning_effort("openai", "proxy-model", "https://proxy.example.com/v1")
    # Same host, different path — still the same endpoint.
    assert not accepts_reasoning_effort("openai", "proxy-model", "https://proxy.example.com/v2")
    # A different endpoint (or default routing) keeps the param.
    assert accepts_reasoning_effort("openai", "proxy-model", "https://api.openai.com/v1")
    assert accepts_reasoning_effort("openai", "proxy-model")


def test_seed_applies_at_any_endpoint() -> None:
    """Seeds encode the vendor's API contract, wherever the model is reached."""
    assert not accepts_reasoning_effort("xai", "grok-4", "https://api.x.ai/v1")
    assert not accepts_reasoning_effort("xai", "grok-4", "http://127.0.0.1:9999/v1")
    assert not accepts_reasoning_effort("xai", "grok-4")


def test_gating_identity_splits_provider_prefix() -> None:
    """``provider/model`` strings gate on the split pair, case-normalized."""
    assert gating_identity("xai/grok-4") == ("xai", "grok-4")
    assert gating_identity("XAI/grok-4") == ("xai", "grok-4")


def test_gating_identity_infers_provider_from_base_url() -> None:
    """A bare model id falls back to the client base URL's endpoint."""
    assert gating_identity("grok-4", "https://api.x.ai/v1") == ("xai", "grok-4")


def test_gating_identity_defaults_to_openai() -> None:
    """An unknown or empty base URL defaults the provider to openai."""
    assert gating_identity("grok-4", "") == ("openai", "grok-4")
    assert gating_identity("grok-4", "http://127.0.0.1:8123/v1") == ("openai", "grok-4")


def test_strip_returns_copy_without_param() -> None:
    """A matching rejection strips the param without mutating the input."""
    extra: dict[str, Any] = {"reasoning_effort": "low", "temperature": 0.5}
    exc = _http_error(400, "Argument not supported on this model: reasoning_effort")

    stripped = strip_rejected_reasoning_effort(extra, exc)

    assert stripped == {"temperature": 0.5}
    assert extra["reasoning_effort"] == "low", "the original dict must not be mutated"


def test_strip_does_not_learn() -> None:
    """Stripping alone learns nothing — the caller records only after a
    confirmed retry, so a false-positive match self-corrects."""
    extra: dict[str, Any] = {"reasoning_effort": "low"}
    exc = _http_error(400, "Argument not supported on this model: reasoning_effort")

    assert strip_rejected_reasoning_effort(extra, exc) is not None
    assert accepts_reasoning_effort("xai", "grok-new")


def test_strip_declines_when_param_absent() -> None:
    """A 400 on a request that never carried the param is not ours."""
    exc = _http_error(400, "Argument not supported on this model: reasoning_effort")
    assert strip_rejected_reasoning_effort({"temperature": 0.5}, exc) is None


def test_strip_declines_on_unrelated_error() -> None:
    """An unrelated failure re-raises — no retry, nothing stripped."""
    exc = _http_error(500, "internal error")
    extra = {"reasoning_effort": "low"}
    assert strip_rejected_reasoning_effort(extra, exc) is None
