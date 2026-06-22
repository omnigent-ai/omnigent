"""End-to-end test of the ``llm_flaky`` multi-model retry mechanism.

Exercises the real conftest wiring (marker translation, per-attempt
context stamping, model rotation) by deliberately failing twice and
asserting each attempt resolved a different model. No LLM is called.
"""

from __future__ import annotations

import pytest

from tests import _model_pools

# Module-global so state survives across rerunfailures attempts.
_SEEN_MODELS: list[str] = []


@pytest.mark.llm_flaky(reruns=2)
def test_llm_flaky_rotates_model_per_attempt() -> None:
    _SEEN_MODELS.append(_model_pools.resolve_model("databricks-claude-sonnet-4-6", spread=False))
    if len(_SEEN_MODELS) < 3:
        # Force a rerun; fails loudly if llm_flaky -> flaky is broken.
        raise AssertionError(f"forcing rerun, attempt {len(_SEEN_MODELS)} of 3")
    # 2-member anthropic chain: 3 attempts rotate sonnet -> opus ->
    # sonnet. A constant list means rotation never happened.
    assert _SEEN_MODELS == [
        "databricks-claude-sonnet-4-6",
        "databricks-claude-opus-4-6",
        "databricks-claude-sonnet-4-6",
    ]
