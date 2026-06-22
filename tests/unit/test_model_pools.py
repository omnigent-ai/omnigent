"""Unit tests for :mod:`tests._model_pools` (test-suite model load-balancing)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests import _model_pools

_SPREAD_ENV = "OMNIGENT_TEST_MODEL_SPREAD"
_GPT_POOL = ("databricks-gpt-5-4", "databricks-gpt-5-5")
_CLAUDE_POOL = ("databricks-claude-sonnet-4-6", "databricks-claude-opus-4-6")


@pytest.fixture(autouse=True)
def _restore_context() -> Iterator[None]:
    """Clear the synthetic contexts these tests set; the conftest hook
    re-stamps a real one at each test's setup."""
    yield
    _model_pools.set_current_test(None)


def test_unpooled_model_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SPREAD_ENV, "1")
    # Unpooled names come back verbatim even with spread + retry.
    assert (
        _model_pools.resolve_model("databricks-gpt-bogus-xyz", key="k", attempt=2)
        == "databricks-gpt-bogus-xyz"
    )
    assert _model_pools.resolve_model("gpt-4o-mini", key="k") == "gpt-4o-mini"


def test_spread_off_returns_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_SPREAD_ENV, raising=False)
    # Env var unset: no-op, local runs see the literal model.
    assert _model_pools.resolve_model("databricks-gpt-5-4", key="anything") == "databricks-gpt-5-4"


def test_spread_is_deterministic_and_in_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SPREAD_ENV, "1")
    first = _model_pools.resolve_model("databricks-gpt-5-4", key="tests/foo.py::test_bar")
    second = _model_pools.resolve_model("databricks-gpt-5-4", key="tests/foo.py::test_bar")
    # Same key -> same member; CI failures reproduce locally.
    assert first == second
    assert first in _GPT_POOL


def test_spread_distributes_across_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SPREAD_ENV, "1")
    resolved = {
        _model_pools.resolve_model("databricks-claude-sonnet-4-6", key=f"tests/t{i}.py::test")
        for i in range(64)
    }
    # 64 keys must hit BOTH members, else the hash is degenerate.
    assert resolved == set(_CLAUDE_POOL)


def test_env_override_replaces_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SPREAD_ENV, "1")
    monkeypatch.setenv("OMNIGENT_TEST_MODEL_POOL_GPT", "databricks-gpt-9-test")
    # Single-member env override captures every gpt-family resolution.
    assert _model_pools.resolve_model("databricks-gpt-5-4", key="any") == "databricks-gpt-9-test"


def test_spread_false_skips_spread_but_rotates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SPREAD_ENV, "1")
    # spread=False (explicit pin): attempt 0 honors it exactly.
    assert (
        _model_pools.resolve_model("databricks-gpt-5-5", key="any", attempt=0, spread=False)
        == "databricks-gpt-5-5"
    )
    # ...but a rerun must still move to a DIFFERENT model.
    rotated = _model_pools.resolve_model("databricks-gpt-5-5", key="any", attempt=1, spread=False)
    assert rotated != "databricks-gpt-5-5"


def test_rotation_changes_model_each_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_SPREAD_ENV, raising=False)
    base = _model_pools.resolve_model("databricks-claude-sonnet-4-6", key="k", attempt=0)
    second = _model_pools.resolve_model("databricks-claude-sonnet-4-6", key="k", attempt=1)
    # 2-member anthropic chain: attempts 0 and 1 cover both members.
    assert base == "databricks-claude-sonnet-4-6"
    assert second == "databricks-claude-opus-4-6"
    # Chain wraps: attempt 2 is back at the base.
    assert _model_pools.resolve_model("databricks-claude-sonnet-4-6", key="k", attempt=2) == base


def test_rotation_walks_full_provider_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_SPREAD_ENV, raising=False)
    seen = {_model_pools.resolve_model("databricks-gpt-5-4", key="k", attempt=a) for a in range(4)}
    # 4 attempts visit all 4 openai-chain models before repeating.
    assert seen == {
        "databricks-gpt-5-4",
        "databricks-gpt-5-5",
        "databricks-gpt-5-4-mini",
        "databricks-gpt-5-mini",
    }


def test_pinned_context_disables_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SPREAD_ENV, "1")
    _model_pools.set_current_test("tests/foo.py::test_pinned", attempt=1, pinned=True)
    # model_pinned wins over spread AND retry rotation.
    assert _model_pools.resolve_model("databricks-gpt-5-4") == "databricks-gpt-5-4"


def test_context_supplies_default_key_and_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SPREAD_ENV, "1")
    nodeid = "tests/foo.py::test_ctx"
    _model_pools.set_current_test(nodeid, attempt=0, pinned=False)
    via_context = _model_pools.resolve_model("databricks-gpt-5-4")
    # Implicit context key == passing the nodeid explicitly.
    assert via_context == _model_pools.resolve_model("databricks-gpt-5-4", key=nodeid)

    _model_pools.set_current_test(nodeid, attempt=1, pinned=False)
    rotated = _model_pools.resolve_model("databricks-gpt-5-4")
    # Context attempt feeds rotation.
    assert rotated != via_context
    assert _model_pools.current_attempt() == 1


def test_no_context_no_key_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SPREAD_ENV, "1")
    _model_pools.set_current_test(None)
    # No context and no key: spreading must not fire.
    assert _model_pools.resolve_model("databricks-gpt-5-4") == "databricks-gpt-5-4"


def test_env_pool_member_outside_static_chain_still_rotates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_SPREAD_ENV, "1")
    monkeypatch.setenv(
        "OMNIGENT_TEST_MODEL_POOL_CLAUDE",
        "databricks-claude-sonnet-4-6,databricks-claude-haiku-4-5",
    )
    resolved = {
        _model_pools.resolve_model("databricks-claude-sonnet-4-6", key="k", attempt=a)
        for a in range(3)
    }
    # Rotation must reach env-added pool members too.
    assert "databricks-claude-haiku-4-5" in resolved


def test_drained_model_excluded_from_spread_and_retry_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_SPREAD_ENV, "1")
    # gpt-5-4 removed from the pool (e.g. rate-limited endpoint).
    monkeypatch.setenv(
        "OMNIGENT_TEST_MODEL_POOL_GPT",
        "databricks-gpt-5-5,databricks-gpt-5-4-mini",
    )
    resolved = {
        _model_pools.resolve_model("databricks-gpt-5-4", key=f"k{i}", attempt=a)
        for i in range(8)
        for a in range(6)
    }
    # Neither spreading nor retry rotation may route to the drained model.
    assert "databricks-gpt-5-4" not in resolved
    assert "databricks-gpt-5-5" in resolved


def test_drained_model_kept_when_it_is_the_pinned_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_SPREAD_ENV, "1")
    monkeypatch.setenv("OMNIGENT_TEST_MODEL_POOL_GPT", "databricks-gpt-5-5")
    # spread=False (explicit pin): attempt 0 still honors the drained
    # model exactly, and reruns rotate away from it without crashing.
    assert (
        _model_pools.resolve_model("databricks-gpt-5-4", key="k", attempt=0, spread=False)
        == "databricks-gpt-5-4"
    )
    rotated = _model_pools.resolve_model("databricks-gpt-5-4", key="k", attempt=1, spread=False)
    assert rotated != "databricks-gpt-5-4"
