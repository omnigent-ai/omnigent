"""Tests for integration journey-suite model selection."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests import _model_pools
from tests.integration.model_selection import resolve_default_model

_SPREAD_ENV = "OMNIGENT_TEST_MODEL_SPREAD"
_GPT_POOL_ENV = "OMNIGENT_TEST_MODEL_POOL_GPT"


@pytest.fixture(autouse=True)
def _restore_model_pool_context() -> Iterator[None]:
    """Clear synthetic model-pool context after each test."""
    yield
    _model_pools.set_current_test(None)


def _key_that_rebalances(model: str) -> str:
    """Find a deterministic context key where hash-spread changes *model*.

    :param model: Pooled model to resolve, e.g. ``"databricks-gpt-5-5"``.
    :returns: A pytest-like nodeid key that resolves to a different pool member.
    :raises AssertionError: If the default pool no longer has a second member.
    """
    for index in range(64):
        key = f"tests/integration/test_model_selection.py::case_{index}"
        if _model_pools.resolve_model(model, key=key) != model:
            return key
    raise AssertionError(f"could not find a spreading key for {model}")


def test_codex_default_model_stays_workflow_pinned_under_spread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex keeps the workflow's GPT pin even when integration spreading is on."""
    model = "databricks-gpt-5-5"
    monkeypatch.setenv(_SPREAD_ENV, "1")
    monkeypatch.delenv(_GPT_POOL_ENV, raising=False)

    key = _key_that_rebalances(model)
    _model_pools.set_current_test(key)

    assert resolve_default_model(model, "openai-agents") != model
    assert resolve_default_model(model, "codex") == model
