"""Unit tests for the workflow-level ``_apply_request_model_override`` helper.

Mirrors the shape of :mod:`tests.unit.runtime.test_reasoning_effort_validation`
in scope (workflow-internal helper, no DBOS / no FastAPI). The helper
backs the ``/model`` slash command's server-side substitution step:
the request's optional ``model_override`` becomes the effective
``LLMConfig.model`` for that single execution while also being
stashed in ``extra["model_override"]`` so the harness path can
forward it.
"""

from __future__ import annotations

from omnigent.runtime.workflow import _apply_request_model_override
from omnigent.spec.types import LLMConfig


def test_apply_request_model_override_none_returns_input_unchanged() -> None:
    """Passing ``None`` is a no-op — the spec's model and extra survive.

    Critical to the design contract: when no per-request override
    is set, the agent spec's configured model wins. Any leak here
    would silently swap models on every request.
    """
    original = LLMConfig(
        model="databricks-gpt-5-4",
        extra={"temperature": 0.5},
    )
    result = _apply_request_model_override(original, None)
    # The returned value is functionally identical to the input.
    assert result.model == "databricks-gpt-5-4"
    assert result.extra == {"temperature": 0.5}
    # ``extra["model_override"]`` is absent — downstream harness
    # propagation must distinguish "no override" from "override
    # set to the spec's own model".
    assert "model_override" not in result.extra


def test_apply_request_model_override_substitutes_model_field() -> None:
    """A non-None override becomes the effective ``llm_config.model``.

    The harness subprocess reads ``llm_config.model`` when calling
    the LLM; substituting it here is what makes the override
    actually take effect on the wire.
    """
    original = LLMConfig(
        model="databricks-gpt-5-4",
        extra={"temperature": 0.5},
    )
    result = _apply_request_model_override(original, "openai/gpt-5.4-mini")
    assert result.model == "openai/gpt-5.4-mini"


def test_apply_request_model_override_stashes_override_in_extra() -> None:
    """The override is also recorded in ``extra["model_override"]``.

    The harness path can't distinguish "spec default model" from
    "user-set override" by looking at ``llm_config.model`` alone —
    both produce the same string after substitution. ``extra`` is
    the unambiguous signal that ``the harness HTTP client`` reads to
    decide whether to emit ``body["model_override"]`` on the wire.
    """
    original = LLMConfig(model="databricks-gpt-5-4", extra={})
    result = _apply_request_model_override(original, "openai/gpt-5.4-mini")
    assert result.extra["model_override"] == "openai/gpt-5.4-mini"


def test_apply_request_model_override_preserves_other_extra_keys() -> None:
    """Existing ``extra`` keys (temperature, reasoning_effort) survive.

    ``_apply_request_model_override`` runs AFTER
    ``_apply_request_reasoning``, so any reasoning_effort the
    request layered in must still be present after the model
    substitution. If this regresses, ``/effort`` and ``/model``
    silently fight when used together.
    """
    original = LLMConfig(
        model="databricks-gpt-5-4",
        extra={"temperature": 0.5, "reasoning_effort": "high"},
    )
    result = _apply_request_model_override(original, "openai/gpt-5.4-mini")
    # Pre-existing keys still there.
    assert result.extra["temperature"] == 0.5
    assert result.extra["reasoning_effort"] == "high"
    # New key added on top.
    assert result.extra["model_override"] == "openai/gpt-5.4-mini"


def test_apply_request_model_override_does_not_mutate_input() -> None:
    """The helper returns a new LLMConfig — the input is untouched.

    Mirrors ``_apply_request_reasoning``'s contract. Mutation of
    the agent's cached ``spec.llm`` would leak the override into
    every subsequent request hitting the same spec, which is
    exactly the bug ``LLMConfig``'s frozen-by-convention contract
    exists to prevent.
    """
    original = LLMConfig(
        model="databricks-gpt-5-4",
        extra={"temperature": 0.5},
    )
    _apply_request_model_override(original, "openai/gpt-5.4-mini")
    # Original is unchanged after the call.
    assert original.model == "databricks-gpt-5-4"
    assert original.extra == {"temperature": 0.5}
